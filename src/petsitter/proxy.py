"""OpenAI API proxy handling for petsitter."""

import asyncio
import json
import logging
from pathlib import Path
import re
import time
from typing import Any

import httpx

from petsitter.context import append_to_system_prompt
from petsitter.observability import (
    get_logger,
    new_request_id,
    request_tag,
    start_trace,
    reset_trace,
    get_trace,
    trace_event,
    reset_current_trickset,
    request_meta,
    reset_request_id,
    reset_request_meta,
    set_current_trickset,
    set_request_id,
    start_request_meta,
)
from petsitter.trick import (
    Trick,
    build_upstream_headers,
    build_upstream_payload,
    callmodel,
    configure,
    get_model_config,
)
from petsitter.trickset import Trickset

logger = logging.getLogger("petsitter")

CONFIG_MAGIC = "__petsitter_config__"

# A 502 from a broker upstream (dyva, litellm) means it raced its pool of
# backends and every one of them was down or missing the model -- a condition
# that usually clears on its own within a second or two as a host comes back.
# Retrying costs one more round trip; not retrying drops a request that would
# have succeeded. Other 5xx codes are left alone: a 500 is the upstream itself
# breaking, and replaying the prompt at it just breaks it again.
UPSTREAM_RETRY_STATUSES = frozenset({502})
UPSTREAM_RETRY_ATTEMPTS = 3
UPSTREAM_RETRY_BACKOFF = 0.5


ANTHROPIC_UPSTREAM = "https://api.anthropic.com"

# Headers Anthropic needs, and the client's own credentials. Petsitter never
# supplies a key here: whatever the caller authenticated with goes upstream.
_ANTHROPIC_PASSTHROUGH = ("x-api-key", "authorization", "anthropic-version",
                          "anthropic-beta", "user-agent")


def _anthropic_headers(incoming: dict) -> dict[str, str]:
    lowered = {k.lower(): v for k, v in (incoming or {}).items()}
    headers = {"content-type": "application/json"}
    for name in _ANTHROPIC_PASSTHROUGH:
        if lowered.get(name):
            headers[name] = lowered[name]
    headers.setdefault("anthropic-version", "2023-06-01")
    return headers


class ProxyHandler:

    def __init__(
        self,
        model_url: str,
        model_name: str | None,
        api_key: str = "",
        tricksets: dict[str, Trickset] | None = None,
        tricks: list[Trick] | None = None,
    ):
        self.model_url = model_url.rstrip("/") if model_url else ""
        self.model_name = model_name or ""
        self.api_key = api_key
        self.tricksets = tricksets or {}
        if tricks is not None and not self.tricksets:
            ts = Trickset("_default", "0.3.0", {"X-Title": "*", "Model": "*"}, [], file_path=str(Path.home() / ".config" / "petsitter" / "tricksets" / "_default.json"))
            ts.tricks = list(tricks)
            ts.trick_enabled = [True] * len(tricks)
            self.tricksets["_default"] = ts
        self._run_counts: dict[str, int] = {}
        # A live client's ANTHROPIC_BASE_URL is baked into its process
        # environment at launch and never re-read, so unregistering (editing
        # settings.json) cannot stop an already-running Claude Code session
        # from continuing to send requests here. This flag is the actual
        # kill switch for that case: while set, every request is forwarded
        # untouched -- no prompt-keyword parsing, no trick pipeline -- so
        # petsitter is functionally off for already-connected clients without
        # needing to kill the shared process (which would drop everyone) or
        # wait for each client to restart.
        self.paused = False
        configure(self.model_url, self.model_name or "", self.api_key)

    @property
    def tricks(self) -> list[Trick]:
        result: list[Trick] = []
        for ts in self.tricksets.values():
            result.extend(ts.tricks)
        return result

    def _matching_tricks(self, x_title: str, model: str) -> tuple[list[Trick], Trickset | None]:
        tricks: list[Trick] = []
        matched: Trickset | None = None
        default_ts = self.tricksets.get("_default")
        for name, ts in self.tricksets.items():
            if name == "_default":
                continue
            if ts.matches(x_title, model):
                enabled = [t for i, t in enumerate(ts.tricks) if i < len(ts.trick_enabled) and ts.trick_enabled[i]]
                ts.get_logger().info(
                    "%strickset '%s' matched (X-Title=%r, Model=%r) -> %d enabled tricks",
                    request_tag(), ts.name, x_title, model, len(enabled),
                )
                if matched is None:
                    matched = ts
                tricks.extend(enabled)
        if not tricks and default_ts:
            enabled = [t for i, t in enumerate(default_ts.tricks) if i < len(default_ts.trick_enabled) and default_ts.trick_enabled[i]]
            default_ts.get_logger().info(
                "%strickset '_default' used as fallback (X-Title=%r, Model=%r) -> %d enabled tricks",
                request_tag(), x_title, model, len(enabled),
            )
            tricks.extend(enabled)
            matched = default_ts
        return tricks, matched

    def _build_headers(self, model_cfg: dict | None = None) -> dict[str, str]:
        if model_cfg is not None:
            return build_upstream_headers(model_cfg)
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    @staticmethod
    def _message_text(msg: dict) -> str | None:
        """The human-readable text of a message, whatever shape its content is.

        Message content arrives in two shapes.  The OpenAI-style one is a plain
        string; the Anthropic-style one (what Claude Code and most block-based
        clients send) is a list of typed blocks.  Anything that wants to read
        what the user actually typed has to cope with both, so it reads it
        here rather than testing ``isinstance(content, str)`` for itself.

        Returns None when the message carries no text at all -- a tool_result
        or image-only turn -- which is different from an empty string.
        """
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                b.get("text") or ""
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            if not parts:
                return None
            return "".join(parts)
        return None

    @staticmethod
    def _set_message_text(msg: dict, text: str) -> None:
        """Write text back into a message, preserving its original shape.

        For block content the text lands in the first text block and any
        further text blocks are dropped, so that a rewrite of the joined text
        does not get duplicated across blocks.  Non-text blocks are untouched.
        """
        content = msg.get("content")
        if isinstance(content, str):
            msg["content"] = text
            return
        if isinstance(content, list):
            new_blocks = []
            seen_text = False
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text":
                    if seen_text:
                        continue
                    seen_text = True
                    new_blocks.append({**b, "text": text})
                else:
                    new_blocks.append(b)
            msg["content"] = new_blocks

    @classmethod
    def _is_config_request(cls, messages: list) -> bool:
        """True when the last message is exactly the config diagnostic magic string."""
        if not messages:
            return False
        last = messages[-1]
        if not isinstance(last, dict) or last.get("role") != "user":
            return False
        text = cls._message_text(last)
        return text is not None and text.strip() == CONFIG_MAGIC

    @staticmethod
    def _trick_diag_entry(trick: Trick) -> dict:
        cfg = {}
        for field in getattr(trick, "config_fields", []) or []:
            key = field.get("key")
            if key and hasattr(trick, key):
                cfg[key] = getattr(trick, key)
        return {
            "class": type(trick).__name__,
            "display_name": getattr(trick, "__display_name__", "") or type(trick).__name__,
            "brief": getattr(trick, "__brief__", ""),
            "keywords": list(getattr(trick, "keywords", []) or []),
            "config": cfg,
            "config_fields": list(getattr(trick, "config_fields", []) or []),
        }

    def _config_diag_result(
        self,
        payload: dict,
        x_title: str,
        original_messages: list,
        messages: list,
        tricks: list[Trick],
        matched_ts_name: str | None,
        target: str,
        upstream_payload: dict,
        upstream_headers: dict,
    ) -> dict:
        """Snapshot the config/tricks/pipeline as a synthetic chat completion."""
        default_cfg = get_model_config("default")
        diag = {
            "service": "petsitter",
            "petsitter_config_diag": True,
            "model": {
                "url": default_cfg.get("url", ""),
                "name": default_cfg.get("model", ""),
                "api_key": "set" if (default_cfg.get("key") or self.api_key) else "",
                "target": target,
            },
            "request": {
                "x_title": x_title,
                "model": payload.get("model"),
                "stream": payload.get("stream", False),
                "original_messages": original_messages,
                "transformed_messages": messages,
                "upstream": {
                    "url": target,
                    "payload": upstream_payload,
                    "auth": "bearer" if any(k.lower() == "authorization" for k in upstream_headers) else "none",
                },
            },
            "trickset": {
                "name": matched_ts_name,
                "tricks": [self._trick_diag_entry(t) for t in tricks],
            },
            "tricksets": {name: ts.to_dict() for name, ts in self.tricksets.items()},
            "capabilities": self._merge_capabilities(tricks),
        }
        return {
            "id": "chatcmpl-petsitter-config",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "petsitter.config",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "\n".join([
                        "```json",
                        json.dumps(diag, indent=2),
                        "```"
                    ])
                },
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    def _apply_system_prompt_tricks(self, system_prompt: str, tricks: list[Trick] | None = None) -> str:
        if tricks is None:
            tricks = self.tricks
        result = system_prompt
        for trick in tricks:
            before = len(result)
            addition = trick.system_prompt(result)
            if not addition:
                get_logger().debug(
                    "%ssystem_prompt hook: %s (no change)", request_tag(), type(trick).__name__,
                )
                trace_event("system_prompt", trick, changed=False)
                continue
            if getattr(trick, "replace_system_prompt", False):
                result = addition
            elif addition not in result:
                result = result + "\n" + addition if result else addition
            get_logger().debug(
                "%ssystem_prompt hook: %s (%d -> %d chars)",
                request_tag(), type(trick).__name__, before, len(result),
            )
            trace_event("system_prompt", trick, changed=True, before=before, after=len(result))
        return result

    def _apply_pre_hooks(self, context: list, params: dict, tricks: list[Trick] | None = None) -> list:
        if tricks is None:
            tricks = self.tricks
        result = context
        for trick in tricks:
            before = len(result)
            result = trick.pre_hook(result, params)
            get_logger().debug(
                "%spre_hook: %s (messages %d -> %d)",
                request_tag(), type(trick).__name__, before, len(result),
            )
            trace_event("pre_hook", trick, changed=result is not context or before != len(result),
                        before=before, after=len(result))
        return result

    def _apply_post_hooks(self, context: list, tricks: list[Trick] | None = None) -> list:
        if tricks is None:
            tricks = self.tricks
        result = context
        for trick in tricks:
            before = len(result)
            result = trick.post_hook(result)
            get_logger().debug(
                "%spost_hook: %s (messages %d -> %d)",
                request_tag(), type(trick).__name__, before, len(result),
            )
            trace_event("post_hook", trick, changed=before != len(result), before=before, after=len(result))
        return result

    def _merge_capabilities(self, tricks: list[Trick] | None = None) -> dict:
        if tricks is None:
            tricks = self.tricks
        capabilities = {}
        for trick in tricks:
            capabilities = trick.info(capabilities)
            get_logger().debug(
                "%sinfo: %s -> capabilities %s",
                request_tag(), type(trick).__name__, {k: type(v).__name__ for k, v in capabilities.items()},
            )
        return capabilities

    def _find_prompt_keyword_patterns(self, text: str) -> list[dict]:
        results: list[dict] = []
        i = 0
        while i < len(text):
            if text[i] != '(':
                i += 1
                continue
            j = i + 1
            if j >= len(text) or not (text[j].isalnum() or text[j] == '_'):
                i += 1
                continue
            while j < len(text) and (text[j].isalnum() or text[j] in ('_', '/')):
                j += 1
            kw_end = j
            if j >= len(text):
                i += 1
                continue
            if text[j] == ':':
                j += 1
                while j < len(text) and text[j].isspace():
                    j += 1
                depth = 1
                k = j
                while k < len(text) and depth > 0:
                    if text[k] == '(':
                        depth += 1
                    elif text[k] == ')':
                        depth -= 1
                    k += 1
                if depth != 0:
                    i += 1
                    continue
                request = text[j:k - 1].strip()
            elif text[j] == ')':
                k = j + 1
                request = ""
            else:
                i += 1
                continue
            keyword = text[i + 1:kw_end].strip()
            results.append({
                "start": i,
                "end": k,
                "keyword": keyword,
                "request": request,
            })
            i = k
        return results

    def _filter_prompt_keywords(self, messages: list, payload: dict | None = None) -> tuple[list, dict | None]:
        registry: dict[str, tuple[Trick, Trickset]] = {}
        for ts_name, ts in self.tricksets.items():
            for i, t in enumerate(ts.tricks):
                kw = (ts.trick_keywords[i] if i < len(ts.trick_keywords) and ts.trick_keywords[i] else None) or t.prompt_keyword
                if kw:
                    registry[kw.lower()] = (t, ts)

        modified = list(messages)
        for msg in reversed(modified):
            if msg.get("role") != "user":
                continue
            content = self._message_text(msg)
            if content is None:
                continue

            patterns = self._find_prompt_keyword_patterns(content)
            if not patterns:
                bare = content.strip().rstrip(".,!?")
                if bare and bare.lower() in registry:
                    patterns = [{
                        "keyword": bare,
                        "request": "",
                        "start": 0,
                        "end": len(content),
                    }]
                else:
                    break

            recognized: list[dict] = []
            unrecognized: list[str] = []
            for p in patterns:
                keyword = p["keyword"].lower()
                entry = registry.get(keyword)
                if entry:
                    recognized.append(p | {"trick": entry[0], "trickset": entry[1]})
                    entry[1].get_logger().info(
                        "%sprompt keyword %r recognized -> %s",
                        request_tag(), keyword, type(entry[0]).__name__,
                    )
                else:
                    unrecognized.append(keyword)
                    get_logger().info("%sprompt keyword %r unrecognized; ignored", request_tag(), keyword)

            # Strip all keyword patterns from content
            for p in reversed(patterns):
                content = content[:p["start"]] + content[p["end"]:]
            content = content.strip()
            content = re.sub(r' +', " ", content).strip()
            self._set_message_text(msg, content)

            # Inject notices for unrecognized keywords
            if unrecognized:
                notes = "Note: unrecognized prompt keyword" + \
                        ("s" if len(unrecognized) > 1 else "") + \
                        " " + ", ".join(f'"{k}"' for k in unrecognized) + \
                        " " + ("were" if len(unrecognized) > 1 else "was") + " ignored."
                if modified and modified[0].get("role") == "system":
                    modified[0]["content"] += "\n\n" + notes
                else:
                    modified.insert(0, {"role": "system", "content": notes})

            # Process recognized keywords in text order
            for p in recognized:
                request_text = p["request"]
                keyword = p["keyword"].lower()
                trick = p["trick"]
                log = p["trickset"].get_logger()
                try:
                    response = trick.handle_prompt_keyword(request_text, modified, payload)
                except Exception as e:
                    log.exception("%sprompt_keyword handler for %r failed: %s", request_tag(), keyword, e)
                    response = {
                        "role": "assistant",
                        "content": f"Error handling prompt keyword '{keyword}': {e}",
                    }
                if isinstance(response, dict):
                    log.info(
                        "%sprompt keyword %r handled by %s -> response injected",
                        request_tag(), keyword, type(trick).__name__,
                    )
                    trace_event("prompt_keyword", trick, keyword=keyword, short_circuit=True)
                    return modified, response

            break

        return modified, None

    def _filter_tricks_by_keywords(self, tricks: list[Trick], messages: list) -> tuple[list[Trick], list]:
        active: list[Trick] = []
        modified = list(messages)
        kw_tricks = [t for t in tricks if t.keywords]
        non_kw_tricks = [t for t in tricks if not t.keywords]

        for msg in reversed(modified):
            if msg.get("role") != "user":
                continue
            content = self._message_text(msg)
            if content is None:
                continue
            for trick in kw_tricks:
                for kw in trick.keywords:
                    pattern = re.compile(r'\b' + re.escape(kw) + r'\b', re.IGNORECASE)
                    if pattern.search(content):
                        content = pattern.sub("", content)
                        if trick not in active:
                            active.append(trick)
                            get_logger().info(
                                "%skeyword %r activated %s",
                                request_tag(), kw, type(trick).__name__,
                            )
                            trace_event("keyword", trick, keyword=kw)
            content = re.sub(r' +', ' ', content).strip()
            self._set_message_text(msg, content)
            break

        result = non_kw_tricks + active
        get_logger().info(
            "%sactive tricks (%d): %s",
            request_tag(), len(result), ", ".join(type(t).__name__ for t in result) or "(none)",
        )
        for trick in result:
            trace_event("active", trick)
        for trick in kw_tricks:
            if trick not in active:
                trace_event("dormant", trick, needs=list(trick.keywords))
        return result, modified

    def get_default_trickset(self) -> Trickset | None:
        for ts in self.tricksets.values():
            return ts
        return None

    def _persist_ts(self, ts: Trickset) -> None:
        """Persist a trickset so dashboard edits survive restarts."""
        if not ts.file_path:
            ts.file_path = str(Path.home() / ".config" / "petsitter" / "tricksets" / f"{ts.name}.json")
        ts.save()

    def add_trick(self, path: str, ts_name: str | None = None) -> Trick:
        if ts_name:
            ts = self.tricksets.get(ts_name)
            if not ts:
                raise KeyError(f"Trickset '{ts_name}' not found")
        else:
            ts = self.get_default_trickset()
            if not ts:
                ts = Trickset("_default", "0.3.0", {"X-Title": "*", "Model": "*"}, [], file_path=str(Path.home() / ".config" / "petsitter" / "tricksets" / "_default.json"))
                self.tricksets["_default"] = ts
        trick = ts.add_trick(path)
        try:
            trick.install()
        except Exception:
            ts.get_logger().exception("trick %s install failed", type(trick).__name__)
        else:
            ts.get_logger().info(
                "trickset '%s': installed %s (%s)", ts.name, type(trick).__name__, path,
            )
        self._persist_ts(ts)
        return trick

    def remove_trick(self, trick_id: str, ts_name: str | None = None) -> bool:
        def _find_trick_by_id(ts: Trickset) -> Trick | None:
            for i, tid in enumerate(ts.trick_ids):
                if tid == trick_id and i < len(ts.tricks):
                    return ts.tricks[i]
            return None

        if ts_name:
            ts = self.tricksets.get(ts_name)
            if not ts:
                return False
            trick = _find_trick_by_id(ts)
            if trick is not None:
                try:
                    trick.uninstall()
                except Exception:
                    logger.exception("Trick %s uninstall failed", trick_id)
            removed = ts.remove_trick(trick_id)
            if removed:
                ts.get_logger().info("trickset '%s': uninstalled %s", ts.name, trick_id)
                self._persist_ts(ts)
            return removed

        for ts in self.tricksets.values():
            trick = _find_trick_by_id(ts)
            if trick is not None:
                try:
                    trick.uninstall()
                except Exception:
                    logger.exception("Trick %s uninstall failed", trick_id)
            if ts.remove_trick(trick_id):
                ts.get_logger().info("trickset '%s': uninstalled %s", ts.name, trick_id)
                self._persist_ts(ts)
                return True
        return False

    def reorder_trick(self, class_name: str, new_index: int, ts_name: str | None = None) -> bool:
        if ts_name:
            ts = self.tricksets.get(ts_name)
            if not ts:
                return False
            tid = ts.find_trick_id_by_class(class_name)
            if tid:
                changed = ts.reorder_trick(tid, new_index)
                if changed:
                    self._persist_ts(ts)
                return changed
            return False
        for ts in self.tricksets.values():
            tid = ts.find_trick_id_by_class(class_name)
            if tid and ts.reorder_trick(tid, new_index):
                self._persist_ts(ts)
                return True
        return False

    def get_tricks_info(self) -> list[dict]:
        result = []
        for ts_name, ts in self.tricksets.items():
            for i, t in enumerate(ts.tricks):
                name = type(t).__name__
                path = ts.trick_paths[i] if i < len(ts.trick_paths) else ""
                tid = ts.trick_ids[i] if i < len(ts.trick_ids) else ""
                result.append({
                    "name": name,
                    "id": tid,
                    "display_name": getattr(t, "__display_name__", None) or name,
                    "brief": getattr(t, "__brief__", ""),
                    "module": type(t).__module__,
                    "trickset": ts_name,
                    "path": path,
                    "enabled": i < len(ts.trick_enabled) and ts.trick_enabled[i],
                    "keywords": list(t.keywords),
                    "prompt_keyword": (ts.trick_keywords[i] if i < len(ts.trick_keywords) and ts.trick_keywords[i] else None) or getattr(t, "prompt_keyword", "") or "",
                    "required_models": list(t.required_models),
                    "config_fields": list(getattr(type(t), "config_fields", []) or []),
                    "config": ts.trick_configs.get(tid, {}),
                })
        return result

    def toggle_trick(self, name: str, enabled: bool | None = None, ts_name: str | None = None) -> bool:
        for ts in self.tricksets.values():
            if ts_name is not None and ts.name != ts_name:
                continue
            for i, t in enumerate(ts.tricks):
                if type(t).__name__ == name:
                    if enabled is None:
                        enabled = not (ts.trick_enabled[i] if i < len(ts.trick_enabled) else True)
                    while len(ts.trick_enabled) <= i:
                        ts.trick_enabled.append(True)
                    ts.trick_enabled[i] = enabled
                    if not ts.file_path:
                        ts.file_path = str(Path.home() / ".config" / "petsitter" / "tricksets" / f"{ts.name}.json")
                    ts.save()
                    return True
        return False

    # --- lifecycle management ---

    def _start_tricks(self, tricks: list[Trick]) -> None:
        for t in tricks:
            name = type(t).__name__
            cnt = self._run_counts.get(name, 0)
            if cnt == 0:
                try:
                    t.startup()
                except Exception:
                    get_logger().exception("%strick %s startup failed", request_tag(), name)
                get_logger().info("%sstarted %s (run 0 -> 1)", request_tag(), name)
            self._run_counts[name] = cnt + 1

    def _stop_tricks(self, tricks: list[Trick]) -> None:
        for t in tricks:
            name = type(t).__name__
            cnt = self._run_counts.get(name, 0)
            if cnt <= 0:
                continue
            if cnt == 1:
                try:
                    t.shutdown()
                except Exception:
                    get_logger().exception("%strick %s shutdown failed", request_tag(), name)
                get_logger().info("%sstopped %s (run 1 -> 0)", request_tag(), name)
            self._run_counts[name] = cnt - 1

    def shutdown_all(self) -> None:
        for name, cnt in list(self._run_counts.items()):
            if cnt > 0:
                for t in self.tricks:
                    if type(t).__name__ == name:
                        try:
                            t.shutdown()
                        except Exception:
                            logger.exception("Trick %s shutdown failed", name)
                        break
                self._run_counts[name] = 0

    async def chat_completions(self, payload: dict, x_title: str = "", upstream_request_url: str = "", forward_headers: dict | None = None) -> dict:
        rid = new_request_id()
        rid_token = set_request_id(rid)
        # Everything about the request that the hooks are not handed directly.
        # post_hook in particular only sees messages, so anything it needs to
        # know about the request that produced them travels here.
        meta_token = start_request_meta(
            request_id=rid,
            payload=payload,
            x_title=x_title,
            tools=payload.get("tools") or [],
            model=payload.get("model", ""),
            stream=bool(payload.get("stream", False)),
        )
        ts_token = None
        tricks: list[Trick] = []
        matched_ts_name: str | None = None
        log = get_logger()
        try:
            default_cfg = get_model_config("default")
            upstream_url = default_cfg["url"]
            if not upstream_request_url and not upstream_url:
                raise ValueError("No upstream model configured. Set a model URL via the dashboard.")
            messages = payload.get("messages", [])
            original_messages = list(messages)
            is_config_request = self._is_config_request(messages)

            log.info("%srequest: model=%r x_title=%r", request_tag(), payload.get("model", ""), x_title)

            if self.paused:
                log.info("%spetsitter paused; forwarding untouched, no tricks applied", request_tag())
            else:
                messages, pk_response = self._filter_prompt_keywords(messages, payload)
                if pk_response:
                    return {
                        "id": "chatcmpl-pk-" + str(int(time.time())),
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": "petsitter",
                        "choices": [{
                            "index": 0,
                            "message": pk_response,
                            "finish_reason": "stop",
                        }],
                        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                    }

                model = payload.get("model", "")
                if model.startswith("trickset/"):
                    ts_name = model.split("/", 1)[1]
                    ts = self.tricksets.get(ts_name)
                    if ts:
                        tricks = list(ts.tricks)
                        matched_ts_name = ts.name
                        ts_token = set_current_trickset(ts)
                        log = get_logger()
                        log.info(
                            "%strickset '%s' selected via model %r -> %d tricks",
                            request_tag(), ts.name, model, len(tricks),
                        )
                        trace_event("trickset", trickset=ts.name, via="model")
                    else:
                        log.warning(
                            "%smodel %r requested but trickset '%s' not loaded",
                            request_tag(), model, ts_name,
                        )
                else:
                    tricks, matched_ts = self._matching_tricks(x_title, model)
                    if matched_ts is not None:
                        matched_ts_name = matched_ts.name
                        ts_token = set_current_trickset(matched_ts)
                        log = get_logger()
                        trace_event("trickset", trickset=matched_ts.name, via="filters")
                    elif not tricks:
                        log.info("%sno trickset matched; no tricks active", request_tag())

                tricks, messages = self._filter_tricks_by_keywords(tricks, messages)
                self._start_tricks(tricks)

                system_prompt = ""
                if messages and messages[0].get("role") == "system":
                    system_prompt = messages[0].get("content", "")
                    messages = messages[1:]

                new_system_prompt = self._apply_system_prompt_tricks(system_prompt, tricks)
                if new_system_prompt:
                    messages = [{"role": "system", "content": new_system_prompt}] + messages

                messages = self._apply_pre_hooks(messages, payload, tricks)

            if upstream_request_url:
                upstream_payload = build_upstream_payload(
                    {"url": upstream_request_url, "model": payload.get("model", "default"), "key": False},
                    messages, payload,
                )
                upstream_headers = build_upstream_headers({"key": False}, extra_headers=forward_headers or {})
                target = upstream_request_url
            else:
                upstream_payload = build_upstream_payload(default_cfg, messages, payload)
                upstream_headers = self._build_headers(default_cfg)
                target = f"{upstream_url}/v1/chat/completions"

            if is_config_request:
                log.info("%sconfig diagnostic requested via magic string", request_tag())
                return self._config_diag_result(
                    payload, x_title, original_messages, messages, tricks,
                    matched_ts_name, target, upstream_payload, upstream_headers,
                )

            log.info("%scalling upstream: %s", request_tag(), target)
            trace_event("upstream", url=target, model=upstream_payload.get("model", ""))
            log.debug("%supstream payload: %s", request_tag(), json.dumps(upstream_payload, indent=2))

            attempts = 0
            for attempt in range(1, UPSTREAM_RETRY_ATTEMPTS + 1):
                attempts = attempt
                try:
                    async with httpx.AsyncClient() as client:
                        response = await client.post(
                            target,
                            json=upstream_payload,
                            headers=upstream_headers,
                            timeout=120.0,
                        )
                except httpx.TransportError as e:
                    raise ValueError(f"Error: {target} can't be reached: {e}") from e

                if (response.status_code not in UPSTREAM_RETRY_STATUSES
                        or attempt == UPSTREAM_RETRY_ATTEMPTS):
                    break

                delay = UPSTREAM_RETRY_BACKOFF * (2 ** (attempt - 1))
                log.warning(
                    "%supstream %s from %s (attempt %d/%d), retrying in %.1fs: %s",
                    request_tag(), response.status_code, target, attempt,
                    UPSTREAM_RETRY_ATTEMPTS, delay,
                    (response.text or "").strip()[:200] or "(empty body)",
                )
                trace_event("upstream_retry", url=target,
                            status=response.status_code, attempt=attempt)
                await asyncio.sleep(delay)

            log.info("%supstream response status: %s", request_tag(), response.status_code)
            log.debug("%supstream response headers: %s", request_tag(), dict(response.headers))
            log.debug("%supstream response body: %s", request_tag(), response.text[:500] if response.text else "(empty)")

            # An upstream that is itself a gateway (dyva, litellm, openrouter)
            # puts the only useful part of the failure in the body -- which host
            # it tried, which model was missing, what the box said back. The
            # status line alone just says "502 Bad Gateway", which is true of
            # every one of those causes and tells you nothing about which.
            if response.status_code >= 400:
                detail = (response.text or "").strip()
                log.error("%supstream %s from %s: %s", request_tag(),
                          response.status_code, target, detail[:2000] or "(empty body)")
                tried = f" after {attempts} attempts" if attempts > 1 else ""
                raise ValueError(
                    f"Upstream {target} returned {response.status_code}{tried}: "
                    f"{detail[:2000] or '(empty body)'}"
                )

            if not response.content:
                log.error("%supstream returned empty response (status %s)", request_tag(), response.status_code)
                log.error("%supstream response headers: %s", request_tag(), dict(response.headers))
                raise ValueError(f"Upstream returned empty response (status {response.status_code})")

            result = response.json()

            log.debug("%supstream response: %s", request_tag(), json.dumps(result, indent=2))

            assistant_message = result["choices"][0]["message"]
            context = messages + [assistant_message]

            log.debug("%scontext before post-hooks: %s", request_tag(), json.dumps(context, indent=2))

            context = self._apply_post_hooks(context, tricks)
            log.debug("%scontext after post-hooks: %s", request_tag(), json.dumps(context, indent=2))

            result["choices"][0]["message"] = context[-1]
            # A trick may have turned the answer into a tool call (see
            # ToolCallTrick, ReferenceCheckTrick). Harnesses that key off
            # finish_reason rather than the message body need it to agree.
            if context[-1].get("tool_calls"):
                result["choices"][0]["finish_reason"] = "tool_calls"

            capabilities = self._merge_capabilities(tricks)
            if capabilities:
                result["capabilities"] = capabilities

            return result
        finally:
            self._stop_tricks(tricks)
            if ts_token is not None:
                reset_current_trickset(ts_token)
            reset_request_meta(meta_token)
            reset_request_id(rid_token)

    async def messages(self, payload: dict, x_title: str = "",
                       forward_headers: dict | None = None) -> dict:
        """Serve Anthropic's /v1/messages through the ordinary trick pipeline.

        The request is translated into the OpenAI shape the tricks are written
        against, run through the same trickset matching and hooks as any other
        request, then translated back and sent to Anthropic. The caller's own
        credentials are forwarded, so petsitter never needs a key of its own.
        """
        from petsitter import anthropic_compat as ac

        rid = new_request_id()
        rid_token = set_request_id(rid)
        messages, tools = ac.to_openai_messages(payload)

        # Same (keyword:request) short-circuit chat_completions() gives the
        # OpenAI path. This was missing here, so a prompt keyword like
        # (exportit:) typed in Claude Code -- which talks to /v1/messages,
        # not /v1/chat/completions -- arrived as literal text and never
        # dispatched to any trick. Skipped entirely while paused, same as
        # every other trick mechanism below.
        if not self.paused:
            messages, pk_response = self._filter_prompt_keywords(messages, payload)
            if pk_response:
                text = pk_response.get("content") or ""
                return {
                    "id": "msg_pk-" + str(int(time.time())),
                    "type": "message",
                    "role": "assistant",
                    "model": payload.get("model", ""),
                    "content": [{"type": "text", "text": text}],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                }

        # The pipeline reads and writes this dict, so tricks that gate tools see
        # and edit exactly the list that will be sent.
        shadow: dict[str, Any] = {
            "model": payload.get("model", ""),
            "messages": messages,
            "tools": tools,
            "stream": bool(payload.get("stream", False)),
            "temperature": payload.get("temperature"),
        }
        meta_token = start_request_meta(
            request_id=rid,
            payload=shadow,
            x_title=x_title,
            tools=list(tools),
            model=payload.get("model", ""),
            stream=bool(payload.get("stream", False)),
            api="anthropic",
        )
        ts_token = None
        tricks: list[Trick] = []
        log = get_logger()
        try:
            if self.paused:
                log.info("%s/v1/messages: petsitter paused; forwarding untouched, no tricks applied", request_tag())
            else:
                model = payload.get("model", "")
                tricks, matched_ts = self._matching_tricks(x_title, model)
                if matched_ts is not None:
                    ts_token = set_current_trickset(matched_ts)
                    log = get_logger()
                    log.info("%s/v1/messages -> trickset '%s' (%d tricks)",
                             request_tag(), matched_ts.name, len(tricks))
                    trace_event("trickset", trickset=matched_ts.name, via="filters")
                elif not tricks:
                    log.info("%s/v1/messages: no trickset matched", request_tag())

                tricks, messages = self._filter_tricks_by_keywords(tricks, messages)
                self._start_tricks(tricks)

                system_prompt = ""
                if messages and messages[0].get("role") == "system":
                    system_prompt = messages[0].get("content", "")
                    messages = messages[1:]
                new_system_prompt = self._apply_system_prompt_tricks(system_prompt, tricks)
                if new_system_prompt:
                    messages = [{"role": "system", "content": new_system_prompt}] + messages

                shadow["messages"] = messages
                messages = self._apply_pre_hooks(messages, shadow, tricks)

            upstream = ANTHROPIC_UPSTREAM.rstrip("/")
            body = ac.to_anthropic_payload(messages, shadow.get("tools") or [], payload)
            headers = _anthropic_headers(forward_headers or {})
            target = f"{upstream}/v1/messages"

            log.info("%scalling upstream: %s", request_tag(), target)
            trace_event("upstream", url=target, model=body.get("model", ""))
            async with httpx.AsyncClient() as client:
                response = await client.post(target, json=body, headers=headers, timeout=600.0)

            if response.status_code >= 400:
                detail = (response.text or "").strip()[:500]
                log.error("%supstream %s: %s", request_tag(), response.status_code, detail)
                raise ValueError(f"Anthropic returned {response.status_code}: {detail}")

            result = response.json()
            assistant = ac.response_to_assistant_message(result)
            context = self._apply_post_hooks(messages + [assistant], tricks)
            if context:
                result = ac.apply_assistant_message(result, context[-1])
            return result
        finally:
            self._stop_tricks(tricks)
            if ts_token is not None:
                reset_current_trickset(ts_token)
            reset_request_meta(meta_token)
            reset_request_id(rid_token)

    async def models(self, upstream_url: str = "", forward_headers: dict | None = None) -> dict:
        if upstream_url:
            target = upstream_url
            headers = build_upstream_headers({"key": False}, extra_headers=forward_headers or {})
        else:
            default_cfg = get_model_config("default")
            base = default_cfg["url"]
            if not base:
                raise ValueError("No upstream model configured. Set a model URL via the dashboard.")
            target = f"{base}/v1/models"
            headers = self._build_headers(default_cfg)
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(target, headers=headers, timeout=30.0)
                response.raise_for_status()
                result = response.json()
        except httpx.TransportError as e:
            raise ValueError(f"Error: {target} can't be reached: {e}") from e
        for name in self.tricksets:
            result.setdefault("data", []).append({
                "id": f"trickset/{name}",
                "object": "model",
                "created": 0,
                "owned_by": "petsitter",
            })
        return result

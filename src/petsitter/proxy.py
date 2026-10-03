"""OpenAI API proxy handling for petsitter."""

import asyncio
import inspect
import json
import logging
from pathlib import Path
import re
import sys
import time
from typing import Any

import httpx

from petsitter.context import append_to_system_prompt
from petsitter.discovered import DiscoveredPrograms
from petsitter.observability import (
    current_request_id,
    current_request_headers,
    current_user_agent,
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
from petsitter.reply_window import ReplyWindow, channel_window
from petsitter.trick import (
    _preview,
    api_root_candidates,
    chat_completions_url,
    learn_api_root,
    find_prompt_keyword_patterns,
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


# How long to wait for the model. Generating can take minutes (a big
# compaction, a slow local model), so reading gets 15 minutes; connecting gets
# 15 seconds so a host that's down still fails fast.
UPSTREAM_TIMEOUT = httpx.Timeout(900.0, connect=15.0)
# While holding a reply, a comment line is sent this often so the client's own
# read timeout doesn't fire. SSE clients ignore comment lines.
HEARTBEAT_SECONDS = 5.0


def sse_from_result(result: dict):
    """A finished chat completion as server-sent events, for streaming clients."""
    message = result["choices"][0]["message"]
    base = {
        "id": result.get("id", "chatcmpl-petsitter"),
        "object": "chat.completion.chunk",
        "created": result.get("created", int(time.time())),
        "model": result.get("model", "unknown"),
    }

    def emit(delta: dict, finish_reason: str | None = None) -> str:
        chunk = dict(base)
        chunk["choices"] = [{"index": 0, "delta": delta, "finish_reason": finish_reason}]
        return f"data: {json.dumps(chunk)}\n\n"

    yield emit({"role": "assistant", "content": ""})
    reasoning = message.get("reasoning_content")
    if reasoning:
        for i in range(0, len(reasoning), 64):
            yield emit({"reasoning_content": reasoning[i:i + 64]})
    content = message.get("content")
    if content:
        for i in range(0, len(content), 64):
            yield emit({"content": content[i:i + 64]})
    if message.get("tool_calls"):
        # Streamed tool calls must each carry "index": clients use it to tell
        # one call's pieces from another's, and strict ones (Goose, the OpenAI
        # SDK) mangle or reject calls without it.
        yield emit({"tool_calls": [
            {**tc, "index": i} if isinstance(tc, dict) else tc
            for i, tc in enumerate(message["tool_calls"])
        ]})
    yield emit({}, finish_reason=result["choices"][0].get("finish_reason", "stop"))
    yield "data: [DONE]\n\n"


def _problems_of(trick) -> list[str]:
    try:
        return [str(p) for p in (trick.problems() or [])]
    except Exception as e:
        return [f"Couldn't check its setup: {e}"]


def _window_of(trick) -> int:
    try:
        return int(trick.needs_window)
    except (TypeError, ValueError):
        return -1


def _wants_all(trick) -> bool:
    """Is this the trick (or one of them) making the channel hold the whole reply?"""
    try:
        return int(trick.needs_window) < 0
    except (TypeError, ValueError):
        return True


def add_tool_call_pieces(calls: list, pieces) -> None:
    """Add streamed tool-call pieces to calls, the list being built.

    A piece that carries a name starts a new call; the name is set, never
    appended. A piece without one continues the call before it, adding its
    arguments. An id is kept from whichever piece has one. ``index`` is
    ignored: upstreams fill it with anything (the same number for every call,
    or a timestamp), and going by it glued parallel calls into one
    ("search_websearch_web..." with their arguments run together).
    """
    for tc in pieces or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        if fn.get("name") or not calls:
            calls.append({"id": tc.get("id") or "", "type": "function",
                          "function": {"name": fn.get("name") or "", "arguments": ""}})
        call = calls[-1]
        if tc.get("id") and not call["id"]:
            call["id"] = tc["id"]
        piece = fn.get("arguments")
        if piece is not None:
            call["function"]["arguments"] += piece if isinstance(piece, str) else json.dumps(piece)


def merge_tool_call_fragments(tool_calls):
    """Stitch streamed tool-call fragments back into whole calls.

    Some upstreams build a non-streamed reply by concatenating stream chunks
    without merging them, so one call arrives as several entries: the first
    with the name and empty arguments, the rest with no name and a piece of
    the arguments each. A named entry starts a call and a nameless one
    continues it (see add_tool_call_pieces); whole calls pass through untouched.
    """
    if not isinstance(tool_calls, list) or len(tool_calls) < 2:
        return tool_calls
    if all(isinstance(tc, dict) and (tc.get("function") or {}).get("name") for tc in tool_calls):
        return tool_calls   # whole calls, each with its name: nothing to stitch
    merged: list[dict] = []
    add_tool_call_pieces(merged, tool_calls)
    return merged


def _tricksets_dir() -> Path:
    """Where tricksets are saved: the directory the server resolved at startup
    from -c / $PET_CONFIG_DIR. Never a hardcoded ~/.config, or a second
    instance run with its own config writes into the first one's."""
    from petsitter import server
    return server.TRICKSETS_DIR


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
            ts = Trickset("_default", "0.3.0", {"X-Title": "*", "Model": "*"}, [], file_path=str(_tricksets_dir() / "_default.json"))
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
        # What traffic has actually come through, in memory only: which apps
        # (X-Title) and models asked, and which tricksets each request landed
        # in. The dashboard uses it to say a rule is live ("12 requests") and
        # to suggest new rules from apps it has really seen.
        self.traffic: dict[str, dict[str, dict]] = {"apps": {}, "models": {}, "tricksets": {}}
        # Every program seen (by X-Title), kept across restarts once the server
        # gives it a file (create_app); in memory until then.
        self.discovered = DiscoveredPrograms(None)
        # How the last call to the provider (the final hop) went, for the
        # dashboard: {"ok", "target", "status", "error", "at"}. None until the
        # first request.
        self.upstream_status: dict | None = None
        configure(self.model_url, self.model_name or "", self.api_key)

    TRAFFIC_KEEP = 50

    @staticmethod
    def _describe_transport_error(e: Exception, timeout: float | None = None) -> str:
        name = type(e).__name__
        detail = str(e).strip()
        if isinstance(e, httpx.TimeoutException):
            what = {"ConnectTimeout": "Couldn't connect", "ReadTimeout": "No response",
                    "WriteTimeout": "Couldn't send the request", "PoolTimeout": "No free connection"}.get(name, "Timed out")
            within = f" within {timeout:g}s" if timeout else ""
            return f"{what}{within} ({name})" + (f": {detail}" if detail else "")
        return f"{name}: {detail}" if detail else name

    def _note_upstream(self, target: str, ok: bool, status: int | None = None, error: str = "") -> None:
        self.upstream_status = {"ok": ok, "target": target, "status": status,
                                "error": (error or "")[:2000], "at": time.time()}

    def _note_traffic(self, x_title: str, model: str, tricksets: list[str], user_agent: str = "") -> None:
        try:
            self.discovered.note(x_title, model, tricksets, user_agent,
                                 headers=current_request_headers())
        except Exception:
            get_logger().exception("couldn't record a discovered program")
        now = time.time()
        for kind, keys in (("apps", [x_title]), ("models", [model]), ("tricksets", tricksets)):
            bucket = self.traffic[kind]
            for key in keys:
                if not key:
                    continue
                entry = bucket.setdefault(key, {"count": 0, "last": 0.0})
                entry["count"] += 1
                entry["last"] = now
            if len(bucket) > self.TRAFFIC_KEEP:
                for stale in sorted(bucket, key=lambda k: bucket[k]["last"])[:len(bucket) - self.TRAFFIC_KEEP]:
                    del bucket[stale]

    @property
    def tricks(self) -> list[Trick]:
        result: list[Trick] = []
        for ts in self.tricksets.values():
            result.extend(ts.tricks)
        return result

    def _matching_tricks(self, x_title: str, model: str, user_agent: str | None = None,
                         record: bool = True) -> tuple[list[Trick], Trickset | None]:
        if user_agent is None:
            user_agent = current_user_agent()
        tricks: list[Trick] = []
        matched: Trickset | None = None
        default_ts = self.tricksets.get("_default")
        hit: list[str] = []
        for name, ts in self.tricksets.items():
            if name == "_default":
                continue
            if ts.matches(x_title, model, user_agent):
                enabled = [t for i, t in enumerate(ts.tricks) if i < len(ts.trick_enabled) and ts.trick_enabled[i]]
                ts.get_logger().info(
                    "%strickset '%s' matched (X-Title=%r, Model=%r) -> %d enabled tricks",
                    request_tag(), ts.name, x_title, model, len(enabled),
                )
                if matched is None:
                    matched = ts
                hit.append(name)
                tricks.extend(enabled)
        # "Everything else" means no rule matched, not "no extensions ran": a
        # matching rule claims the request even while it's empty or all its
        # extensions are off, instead of quietly falling through to _default.
        if not hit and default_ts:
            enabled = [t for i, t in enumerate(default_ts.tricks) if i < len(default_ts.trick_enabled) and default_ts.trick_enabled[i]]
            default_ts.get_logger().info(
                "%strickset '_default' used as fallback (X-Title=%r, Model=%r) -> %d enabled tricks",
                request_tag(), x_title, model, len(enabled),
            )
            tricks.extend(enabled)
            matched = default_ts
            hit.append("_default")
        if record:
            self._note_traffic(x_title, model, hit, user_agent)
        return tricks, matched

    def _preview_transform(self, messages: list, payload: dict | None) -> list:
        """The conversation as the matching channel's extensions would send it,
        on a copy: system prompts and pre_hooks, no model call, and none of the
        extensions that only watch, so nothing is logged or counted."""
        import copy
        msgs = copy.deepcopy(messages)
        params = copy.deepcopy({k: v for k, v in (payload or {}).items() if k != "messages"})
        model = params.get("model", "") or ""
        if model.startswith("trickset/") and model.split("/", 1)[1] in self.tricksets:
            ts = self.tricksets[model.split("/", 1)[1]]
            tricks = [t for i, t in enumerate(ts.tricks) if i < len(ts.trick_enabled) and ts.trick_enabled[i]]
        else:
            x_title = request_meta().get("x_title") or next(
                (v for k, v in current_request_headers() if k.lower() == "x-title"), "")
            tricks, _ = self._matching_tricks(x_title, model, record=False)
        tricks, msgs = self._filter_tricks_by_keywords(tricks, msgs)
        tricks = [t for t in tricks if _window_of(t) != 0]
        system_prompt = ""
        if msgs and msgs[0].get("role") == "system":
            system_prompt = msgs[0].get("content", "")
            msgs = msgs[1:]
        new_system_prompt = self._apply_system_prompt_tricks(system_prompt, tricks)
        if new_system_prompt:
            msgs = [{"role": "system", "content": new_system_prompt}] + msgs
        params["messages"] = msgs
        return self._apply_pre_hooks(msgs, params, tricks)

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
            "category": getattr(trick, "__category__", "") or "",
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
        return find_prompt_keyword_patterns(text)

    def _filter_prompt_keywords(self, messages: list, payload: dict | None = None) -> tuple[list, dict | None]:
        registry: dict[str, tuple[Trick, Trickset]] = {}
        for ts_name, ts in self.tricksets.items():
            for i, t in enumerate(ts.tricks):
                kw = (ts.trick_keywords[i] if i < len(ts.trick_keywords) and ts.trick_keywords[i] else None) or t.prompt_keyword
                if kw:
                    registry[kw.lower()] = (t, ts)

        modified = list(messages)
        # (keyword: request) is petsitter's own control syntax -- a layer above
        # the inference provider, the same way a lower OSI layer's framing never
        # shows up to the layer above it. It must never reach the model, on
        # this turn or any later one. Only the most recent user message with
        # text is "live": it can actually fire a trick's handler and
        # short-circuit the request. Every older user message still gets
        # scanned on every single request (the client resends full history
        # every time), but only to strip petsitter's own syntax back out --
        # never to re-run a handler, since that turn already ran once for
        # real when it was live.
        is_live = True
        for msg in reversed(modified):
            if msg.get("role") != "user":
                continue
            content = self._message_text(msg)
            if content is None:
                continue

            this_is_live = is_live
            is_live = False

            patterns = [
                p for p in self._find_prompt_keyword_patterns(content)
                # "(x = 'a')" is far likelier to be code than a mistyped
                # keyword, so the delimited form only counts once it names
                # a keyword something actually registered.
                if not p["delimited"] or p["keyword"].lower() in registry
            ]
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
                    continue

            recognized: list[dict] = []
            unrecognized: list[str] = []
            kept: list[dict] = []
            for p in patterns:
                keyword = p["keyword"].lower()
                entry = registry.get(keyword)
                if entry and not entry[0].strip_prompt_keyword and keyword == entry[0].prompt_keyword.lower():
                    # The trick rewrites this pattern itself in its pre_hook,
                    # and needs it still sitting where the user typed it.
                    kept.append(p)
                elif entry:
                    recognized.append(p | {"trick": entry[0], "trickset": entry[1]})
                    if this_is_live:
                        entry[1].get_logger().info(
                            "%sprompt keyword %r recognized -> %s",
                            request_tag(), keyword, type(entry[0]).__name__,
                        )
                elif this_is_live:
                    unrecognized.append(keyword)
                    get_logger().info("%sprompt keyword %r unrecognized; ignored", request_tag(), keyword)
                # else: an unrecognized paren in a buried turn is just a paren --
                # only petsitter's own known keywords get scrubbed back there.

            if not this_is_live and not recognized:
                continue

            # Strip patterns from content: all of them on the live turn
            # (including unrecognized ones, which get a system-prompt note
            # instead), only the recognized ones on an older turn.
            strip = [p for p in patterns if p not in kept] if this_is_live else recognized
            if not strip:
                continue
            for p in reversed(strip):
                content = content[:p["start"]] + content[p["end"]:]
            content = re.sub(r' +', " ", content).strip()
            self._set_message_text(msg, content)

            if not this_is_live:
                continue

            # Inject notices for unrecognized keywords -- only meaningful on
            # the live turn, since it's the only one about to get a real reply.
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
                # The framework knows a keyword fired even when the trick says
                # nothing, so its Live tab always shows it -- unless the trick
                # reported something itself during the call.
                seen = trick.live_feed.since(0)
                before = seen[-1][0] if seen else 0
                shown = f"({keyword}: {request_text[:80]}{'...' if len(request_text) > 80 else ''})" if request_text else f"({keyword})"
                preview_token = _preview.set(lambda m=list(modified): self._preview_transform(m, payload))
                try:
                    response = trick.handle_prompt_keyword(request_text, modified, payload)
                except Exception as e:
                    log.exception("%sprompt_keyword handler for %r failed: %s", request_tag(), keyword, e)
                    trick.report(f"{shown} failed: {e}")
                    response = {
                        "role": "assistant",
                        "content": f"Error handling prompt keyword '{keyword}': {e}",
                    }
                else:
                    if not trick.live_feed.since(before):
                        trick.report(f"Ran {shown}" + (" and answered it directly" if isinstance(response, dict) else ""))
                finally:
                    _preview.reset(preview_token)
                if isinstance(response, dict):
                    log.info(
                        "%sprompt keyword %r handled by %s -> response injected",
                        request_tag(), keyword, type(trick).__name__,
                    )
                    trace_event("prompt_keyword", trick, keyword=keyword, short_circuit=True)
                    return modified, response

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
            ts.file_path = str(_tricksets_dir() / f"{ts.name}.json")
        ts.save()

    def add_trick(self, path: str, ts_name: str | None = None) -> Trick:
        if ts_name:
            ts = self.tricksets.get(ts_name)
            if not ts:
                raise KeyError(f"Trickset '{ts_name}' not found")
        else:
            ts = self.get_default_trickset()
            if not ts:
                ts = Trickset("_default", "0.3.0", {"X-Title": "*", "Model": "*"}, [], file_path=str(_tricksets_dir() / "_default.json"))
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
                    "category": getattr(t, "__category__", "") or "",
                    "module": type(t).__module__,
                    "trickset": ts_name,
                    "path": path,
                    "enabled": i < len(ts.trick_enabled) and ts.trick_enabled[i],
                    "keywords": list(t.keywords),
                    "prompt_keyword": (ts.trick_keywords[i] if i < len(ts.trick_keywords) and ts.trick_keywords[i] else None) or getattr(t, "prompt_keyword", "") or "",
                    "required_models": list(t.required_models),
                    "optional_models": list(getattr(t, "optional_models", []) or []),
                    "config_fields": list(getattr(type(t), "config_fields", []) or []),
                    "config": ts.trick_configs.get(tid, {}),
                    "changed_by": ts.trick_changed_by.get(tid, {}),
                    "has_ui": type(t).has_ui(),
                    "settings": t.current_settings(),
                    "problems": _problems_of(t),
                    "readme": inspect.cleandoc(getattr(sys.modules.get(type(t).__module__), "__doc__", None) or ""),
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
                    ts.set_trick_enabled(ts.trick_ids[i], enabled)   # you did it: clears a model's mark
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

    # A chat request runs in three parts. _begin_chat/_end_chat bracket it
    # (request id, metadata, channel); _prepare_chat does everything before the
    # model (prompt keywords, channel matching, system prompt, pre_hooks); then
    # either _finish_buffered (whole reply; post_hooks may rewrite it) or
    # _stream_upstream (pieces forwarded as they arrive; post_hooks only look).

    def _begin_chat(self, payload: dict, x_title: str):
        from types import SimpleNamespace
        rid = current_request_id() or new_request_id()
        return SimpleNamespace(
            rid=rid, rid_token=set_request_id(rid), ts_token=None, tricks=[],
            meta_token=start_request_meta(
                request_id=rid,
                payload=payload,
                x_title=x_title,
                tools=payload.get("tools") or [],
                model=payload.get("model", ""),
                stream=bool(payload.get("stream", False)),
            ),
        )

    def _end_chat(self, req) -> None:
        self._stop_tricks(req.tricks)
        if req.ts_token is not None:
            reset_current_trickset(req.ts_token)
        reset_request_meta(req.meta_token)
        reset_request_id(req.rid_token)

    def _prepare_chat(self, req, payload: dict, x_title: str, upstream_request_url: str,
                      forward_headers: dict | None) -> dict | None:
        """Everything before the model. Returns a finished result when the
        request is answered without one (prompt keyword, config diagnostic)."""
        tricks: list[Trick] = []
        matched_ts_name: str | None = None
        log = get_logger()
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
                    # the channel's extensions that are on, as for any request
                    tricks = [t for i, t in enumerate(ts.tricks) if i < len(ts.trick_enabled) and ts.trick_enabled[i]]
                    matched_ts_name = ts.name
                    req.ts_token = set_current_trickset(ts)
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
                    req.ts_token = set_current_trickset(matched_ts)
                    log = get_logger()
                    trace_event("trickset", trickset=matched_ts.name, via="filters")
                elif not tricks:
                    log.info("%sno trickset matched; no tricks active", request_tag())

            tricks, messages = self._filter_tricks_by_keywords(tricks, messages)
            self._start_tricks(tricks)
            req.tricks = tricks

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
            target = chat_completions_url(upstream_url)
            req.base = upstream_url

        if is_config_request:
            log.info("%sconfig diagnostic requested via magic string", request_tag())
            return self._config_diag_result(
                payload, x_title, original_messages, messages, tricks,
                matched_ts_name, target, upstream_payload, upstream_headers,
            )

        req.messages = messages
        req.target = target
        req.upstream_payload = upstream_payload
        req.upstream_headers = upstream_headers
        req.log = log
        request_meta()["resend"] = self._resend_chat(req)
        return None

    @staticmethod
    def _resend_chat(req):
        """For call_upstream_sync: this request again, with other messages and
        tools, to the same upstream; the reply as an assistant message."""
        def resend(messages: list, tools: list | None = None) -> dict:
            body = {k: v for k, v in req.upstream_payload.items() if k not in ("stream", "stream_options")}
            body["messages"] = messages
            if tools:
                body["tools"] = tools
            else:
                body.pop("tools", None)
                body.pop("tool_choice", None)
            with httpx.Client() as client:
                r = client.post(req.target, json=body, headers=req.upstream_headers, timeout=UPSTREAM_TIMEOUT)
            r.raise_for_status()
            msg = r.json()["choices"][0]["message"]
            if msg.get("tool_calls"):
                msg["tool_calls"] = merge_tool_call_fragments(msg["tool_calls"])
            return msg
        return resend

    def _other_chat_target(self, req):
        """After a 404: the base's other candidate endpoint (with or without
        /v1), as (url, root), or None. See api_root_candidates."""
        base = getattr(req, "base", None)
        if not base or base.rstrip("/").endswith("/chat/completions"):
            return None
        for root in api_root_candidates(base):
            if root + "/chat/completions" != req.target:
                return root + "/chat/completions", root
        return None

    async def _finish_buffered(self, req) -> dict:
        """Call the model for the whole reply, then run every post_hook."""
        messages, tricks, target = req.messages, req.tricks, req.target
        upstream_payload, upstream_headers, log = req.upstream_payload, req.upstream_headers, req.log
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
                        timeout=UPSTREAM_TIMEOUT,
                    )
            except httpx.TransportError as e:
                self._note_upstream(target, False, error=self._describe_transport_error(e, UPSTREAM_TIMEOUT.read))
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

        other = self._other_chat_target(req) if response.status_code == 404 else None
        if other:
            try:
                async with httpx.AsyncClient() as client:
                    retry = await client.post(other[0], json=upstream_payload, headers=upstream_headers,
                                              timeout=UPSTREAM_TIMEOUT)
            except httpx.TransportError:
                retry = None
            if retry is not None and retry.status_code != 404:
                log.info("%s%s was a 404; %s answers, using it from now on", request_tag(), target, other[0])
                learn_api_root(req.base, other[1])
                response, target = retry, other[0]
                req.target = target

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
            self._note_upstream(target, False, response.status_code, detail or "(empty body)")
            raise ValueError(
                f"Upstream {target} returned {response.status_code}{tried}: "
                f"{detail[:2000] or '(empty body)'}"
            )

        if not response.content:
            log.error("%supstream returned empty response (status %s)", request_tag(), response.status_code)
            log.error("%supstream response headers: %s", request_tag(), dict(response.headers))
            raise ValueError(f"Upstream returned empty response (status {response.status_code})")

        result = response.json()
        self._note_upstream(target, True, response.status_code)

        log.debug("%supstream response: %s", request_tag(), json.dumps(result, indent=2))

        assistant_message = result["choices"][0]["message"]
        if assistant_message.get("tool_calls"):
            assistant_message["tool_calls"] = merge_tool_call_fragments(assistant_message["tool_calls"])
        context = messages + [assistant_message]

        log.debug("%scontext before post-hooks: %s", request_tag(), json.dumps(context, indent=2))

        # In a thread: a post_hook may call the model again (call_upstream_sync),
        # and that mustn't stop every other request while it waits.
        context = await asyncio.to_thread(self._apply_post_hooks, context, tricks)
        log.debug("%scontext after post-hooks: %s", request_tag(), json.dumps(context, indent=2))

        result["choices"][0]["message"] = context[-1]
        # A trick may have turned the answer into a tool call (see
        # ToolCallTrick, ReferenceCheckTrick), or answered the model's tool
        # call itself. Harnesses that key off finish_reason rather than the
        # message body need it to agree.
        if context[-1].get("tool_calls"):
            result["choices"][0]["finish_reason"] = "tool_calls"
        elif result["choices"][0].get("finish_reason") == "tool_calls":
            result["choices"][0]["finish_reason"] = "stop"

        capabilities = self._merge_capabilities(tricks)
        if capabilities:
            result["capabilities"] = capabilities

        return result

    async def chat_completions(self, payload: dict, x_title: str = "", upstream_request_url: str = "", forward_headers: dict | None = None) -> dict:
        req = self._begin_chat(payload, x_title)
        try:
            early = self._prepare_chat(req, payload, x_title, upstream_request_url, forward_headers)
            if early is not None:
                return early
            return await self._finish_buffered(req)
        finally:
            self._end_chat(req)

    async def chat_completions_stream(self, payload: dict, x_title: str = "", upstream_request_url: str = "",
                                      forward_headers: dict | None = None):
        """The same request, as server-sent events for a streaming client.

        How much of the reply is held back is the channel's window (see
        reply_window): the whole reply if any trick needs all of it, with SSE
        comment heartbeats so the client's own timeout doesn't give up on a
        long generation; otherwise the model's stream is forwarded as it
        arrives, minus the last few characters the rewriting tricks still
        need to see.
        """
        req = self._begin_chat(payload, x_title)
        try:
            early = self._prepare_chat(req, payload, x_title, upstream_request_url, forward_headers)
            if early is not None:
                for line in sse_from_result(early):
                    yield line
                return
            window, rewriters, observers = channel_window(req.tricks)
            if window < 0:
                req.log.info("%sholding the reply for %s", request_tag(),
                             ", ".join(type(t).__name__ for t in rewriters if _wants_all(t)))
                task = asyncio.create_task(self._finish_buffered(req))
                # One right away, so the response has started before the
                # client's time-to-first-byte limit.
                yield ": petsitter is waiting for the full reply\n\n"
                try:
                    while True:
                        done, _ = await asyncio.wait({task}, timeout=HEARTBEAT_SECONDS)
                        if done:
                            break
                        yield ": petsitter is waiting for the full reply\n\n"
                finally:
                    if not task.done():
                        task.cancel()   # the client went away
                for line in sse_from_result(task.result()):
                    yield line
            else:
                async for line in self._stream_upstream(req, window, rewriters, observers):
                    yield line
        finally:
            self._end_chat(req)

    def _window_rewrite(self, req, rewriters):
        """The rewriting post_hooks as one function on an assistant message."""
        def rewrite(msg: dict) -> dict:
            context = list(req.messages) + [msg]
            for trick in rewriters:
                context = trick.post_hook(context)
            return context[-1] if context else msg
        return rewrite

    def _run_observers(self, req, observers, reply: dict) -> None:
        # The reply has already gone out: these post_hooks can look, not change.
        if observers:
            try:
                self._apply_post_hooks(req.messages + [reply], observers)
            except Exception:
                req.log.exception("%sa post_hook failed after streaming", request_tag())

    async def _stream_upstream(self, req, window: int = 0, rewriters=(), observers=()):
        """Forward the model's own stream. With a window, text passes through
        the rewriting post_hooks on the way and tool calls are held to the
        end; either way the reply as sent is reassembled so the observing
        post_hooks see it whole at the end."""
        log, target = req.log, req.target
        body = dict(req.upstream_payload, stream=True)
        if window:
            log.info("%sstreaming from upstream with a %d-character window: %s", request_tag(), window, target)
        else:
            log.info("%sstreaming from upstream: %s", request_tag(), target)
        trace_event("upstream", url=target, model=body.get("model", ""), stream=True, window=window)
        win = ReplyWindow(self._window_rewrite(req, rewriters), window) if window else None

        # what was sent, for the observers
        reply: dict[str, Any] = {"role": "assistant", "content": ""}
        reasoning = ""
        calls: list[dict] = []
        finish = None

        def sent(obj) -> str:
            nonlocal reasoning, finish
            try:
                choice = (obj.get("choices") or [{}])[0]
            except (AttributeError, IndexError):
                choice = {}
            delta = choice.get("delta") or {}
            finish = choice.get("finish_reason") or finish
            if isinstance(delta.get("content"), str):
                reply["content"] += delta["content"]
            if isinstance(delta.get("reasoning_content"), str):
                reasoning += delta["reasoning_content"]
            add_tool_call_pieces(calls, delta.get("tool_calls"))
            return f"data: {json.dumps(obj)}\n\n"

        # windowed only: the chunk shape to copy, tool calls and the finish
        # chunk (plus anything after it, like usage) held for the end
        template: dict = {}
        held_calls: list[dict] = []
        held_end: list[dict] = []

        def chunk(delta: dict, finish_reason=None) -> dict:
            return {"id": template.get("id", "chatcmpl-petsitter"), "object": "chat.completion.chunk",
                    "created": template.get("created", int(time.time())), "model": template.get("model", ""),
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}

        async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT) as client:
            for attempt in range(1, UPSTREAM_RETRY_ATTEMPTS + 1):
                resp_cm = client.stream("POST", target, json=body, headers=req.upstream_headers)
                try:
                    resp = await resp_cm.__aenter__()
                except httpx.TransportError as e:
                    self._note_upstream(target, False, error=self._describe_transport_error(e, UPSTREAM_TIMEOUT.read))
                    raise ValueError(f"Error: {target} can't be reached: {e}") from e
                if resp.status_code in UPSTREAM_RETRY_STATUSES and attempt < UPSTREAM_RETRY_ATTEMPTS:
                    await resp_cm.__aexit__(None, None, None)
                    await asyncio.sleep(UPSTREAM_RETRY_BACKOFF * (2 ** (attempt - 1)))
                    continue
                break
            other = self._other_chat_target(req) if resp.status_code == 404 else None
            if other:
                other_cm = client.stream("POST", other[0], json=body, headers=req.upstream_headers)
                try:
                    other_resp = await other_cm.__aenter__()
                except httpx.TransportError:
                    other_resp = None
                if other_resp is not None and other_resp.status_code != 404:
                    await resp_cm.__aexit__(None, None, None)
                    log.info("%s%s was a 404; %s answers, using it from now on", request_tag(), target, other[0])
                    learn_api_root(req.base, other[1])
                    resp_cm, resp, target = other_cm, other_resp, other[0]
                    req.target = target
                elif other_resp is not None:
                    await other_cm.__aexit__(None, None, None)
            try:
                if resp.status_code >= 400:
                    detail = (await resp.aread()).decode("utf-8", "replace").strip()
                    self._note_upstream(target, False, resp.status_code, detail or "(empty body)")
                    raise ValueError(f"Upstream {target} returned {resp.status_code}: {detail[:2000] or '(empty body)'}")
                try:
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            obj = json.loads(data)
                        except ValueError:
                            yield f"data: {data}\n\n"
                            continue
                        if win is None:
                            yield f"data: {data}\n\n"
                            sent(obj)
                            continue
                        if held_end:
                            held_end.append(obj)
                            continue
                        choices = obj.get("choices") if isinstance(obj, dict) else None
                        if not choices:
                            yield sent(obj)
                            continue
                        template = template or obj
                        choice = choices[0]
                        delta = choice.get("delta") or {}
                        text = delta.pop("content", None)
                        add_tool_call_pieces(held_calls, delta.pop("tool_calls", None))
                        out = win.feed(text) if isinstance(text, str) else ""
                        if choice.get("finish_reason"):
                            if out or delta:
                                yield sent(chunk(dict(delta, **({"content": out} if out else {}))))
                            choice["delta"] = {}
                            held_end.append(obj)
                            continue
                        if out:
                            delta["content"] = out
                        if delta:
                            choice["delta"] = delta
                            yield sent(obj)
                except httpx.TransportError as e:
                    self._note_upstream(target, False, error=self._describe_transport_error(e, UPSTREAM_TIMEOUT.read))
                    raise
                self._note_upstream(target, True, resp.status_code)
            finally:
                await resp_cm.__aexit__(None, None, None)
        if win is not None:
            # In a thread: a rewriter may answer a tool call by asking the
            # model again (call_upstream_sync), and that mustn't block.
            tail, final_calls = await asyncio.to_thread(win.finish, held_calls or None)
            for t in rewriters:
                trace_event("post_hook", t, windowed=True)
            if tail:
                yield sent(chunk({"content": tail}))
            if final_calls:
                # each with its "index": clients use it to tell calls apart
                yield sent(chunk({"tool_calls": [dict(tc, index=i) for i, tc in enumerate(final_calls)]}))
            if not held_end:
                held_end.append(chunk({}, "tool_calls" if final_calls else "stop"))
            for obj in held_end:
                for choice in (obj.get("choices") or []) if isinstance(obj, dict) else []:
                    if choice.get("finish_reason") == "tool_calls" and not final_calls:
                        choice["finish_reason"] = "stop"   # a rewriter answered them
                yield sent(obj)
        if calls:
            reply["tool_calls"] = calls
        if reasoning:
            reply["reasoning_content"] = reasoning
        self._run_observers(req, observers, reply)
        log.info("%sstreamed reply done (finish=%s)", request_tag(), finish)
        yield "data: [DONE]\n\n"

    # Anthropic's /v1/messages (what Claude Code talks to), split the same way
    # as chat completions: _prepare_messages does everything before the model,
    # then the reply is either held whole (_finish_messages_buffered, with ping
    # events as heartbeats when streaming) or, when no trick needs it whole,
    # Anthropic's own event stream is forwarded untouched (_stream_messages).
    #
    # Never called while paused: the server routes a paused request straight
    # to Anthropic before it reaches here.

    def _begin_messages(self):
        from types import SimpleNamespace
        rid = current_request_id() or new_request_id()
        return SimpleNamespace(rid=rid, rid_token=set_request_id(rid), meta_token=None,
                               ts_token=None, tricks=[])

    def _end_messages(self, req) -> None:
        self._stop_tricks(req.tricks)
        if req.ts_token is not None:
            reset_current_trickset(req.ts_token)
        if req.meta_token is not None:
            reset_request_meta(req.meta_token)
        reset_request_id(req.rid_token)

    def _prepare_messages(self, req, payload: dict, x_title: str, forward_headers: dict | None,
                          upstream_request_url: str = "") -> dict | None:
        """Translate to the OpenAI shape tricks are written against, run the
        pipeline up to the model, and translate back. Returns a finished
        response when a prompt keyword answered it."""
        from petsitter import anthropic_compat as ac

        messages, tools = ac.to_openai_messages(payload)

        # Same (keyword:request) short-circuit chat_completions() gives the
        # OpenAI path, so a prompt keyword typed in Claude Code dispatches too.
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
        req.meta_token = start_request_meta(
            request_id=req.rid,
            payload=shadow,
            x_title=x_title,
            tools=list(tools),
            model=payload.get("model", ""),
            stream=bool(payload.get("stream", False)),
            api="anthropic",
            # The caller's own parameters (max_tokens, thinking...): the shadow
            # above keeps only what the pipeline works with.
            request_params={k: v for k, v in payload.items() if k not in ("messages", "system", "tools")},
        )
        log = get_logger()
        model = payload.get("model", "")
        tricks, matched_ts = self._matching_tricks(x_title, model)
        if matched_ts is not None:
            req.ts_token = set_current_trickset(matched_ts)
            log = get_logger()
            log.info("%s/v1/messages -> trickset '%s' (%d tricks)",
                     request_tag(), matched_ts.name, len(tricks))
            trace_event("trickset", trickset=matched_ts.name, via="filters")
        elif not tricks:
            log.info("%s/v1/messages: no trickset matched", request_tag())

        tricks, messages = self._filter_tricks_by_keywords(tricks, messages)
        self._start_tricks(tricks)
        req.tricks = tricks

        system_prompt = ""
        if messages and messages[0].get("role") == "system":
            system_prompt = messages[0].get("content", "")
            messages = messages[1:]
        new_system_prompt = self._apply_system_prompt_tricks(system_prompt, tricks)
        if new_system_prompt:
            messages = [{"role": "system", "content": new_system_prompt}] + messages

        shadow["messages"] = messages
        messages = self._apply_pre_hooks(messages, shadow, tricks)

        req.messages = messages
        req.shadow = shadow
        req.body = ac.to_anthropic_payload(messages, shadow.get("tools") or [], payload)
        req.headers = _anthropic_headers(forward_headers or {})
        req.target = upstream_request_url or f"{ANTHROPIC_UPSTREAM.rstrip('/')}/v1/messages"
        req.log = log
        request_meta()["resend"] = self._resend_messages(req, payload)
        return None

    @staticmethod
    def _resend_messages(req, payload: dict):
        """call_upstream_sync on the Anthropic path: translated there and back."""
        from petsitter import anthropic_compat as ac

        def resend(messages: list, tools: list | None = None) -> dict:
            body = ac.to_anthropic_payload(messages, tools or [], payload)
            body.pop("stream", None)
            with httpx.Client() as client:
                r = client.post(req.target, json=body, headers=req.headers, timeout=UPSTREAM_TIMEOUT)
            r.raise_for_status()
            return ac.response_to_assistant_message(r.json())
        return resend

    async def _finish_messages_buffered(self, req) -> dict:
        from petsitter import anthropic_compat as ac
        log, target = req.log, req.target
        log.info("%scalling upstream: %s", request_tag(), target)
        trace_event("upstream", url=target, model=req.body.get("model", ""))
        body = {k: v for k, v in req.body.items() if k != "stream"}
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(target, json=body, headers=req.headers, timeout=UPSTREAM_TIMEOUT)
        except httpx.TransportError as e:
            self._note_upstream(target, False, error=self._describe_transport_error(e, UPSTREAM_TIMEOUT.read))
            raise

        if response.status_code >= 400:
            detail = (response.text or "").strip()[:500]
            log.error("%supstream %s: %s", request_tag(), response.status_code, detail)
            self._note_upstream(target, False, response.status_code, detail or "(empty body)")
            raise ValueError(f"Anthropic returned {response.status_code}: {detail}")
        self._note_upstream(target, True, response.status_code)

        result = response.json()
        assistant = ac.response_to_assistant_message(result)
        context = await asyncio.to_thread(self._apply_post_hooks, req.messages + [assistant], req.tricks)
        if context:
            result = ac.apply_assistant_message(result, context[-1])
        return result

    async def messages(self, payload: dict, x_title: str = "",
                       forward_headers: dict | None = None, upstream_request_url: str = "") -> dict:
        """Serve Anthropic's /v1/messages through the ordinary trick pipeline,
        as one whole response."""
        req = self._begin_messages()
        try:
            early = self._prepare_messages(req, payload, x_title, forward_headers, upstream_request_url)
            if early is not None:
                return early
            return await self._finish_messages_buffered(req)
        finally:
            self._end_messages(req)

    async def messages_stream(self, payload: dict, x_title: str = "",
                              forward_headers: dict | None = None, upstream_request_url: str = ""):
        """The same request as Anthropic server-sent events.

        Held whole (with ping events every few seconds, which Anthropic
        clients expect and ignore) when a trick needs the whole reply;
        otherwise Anthropic's own stream, with text passing through the
        channel's window (see reply_window) when a trick rewrites it.
        """
        from petsitter import anthropic_compat as ac
        req = self._begin_messages()
        try:
            early = self._prepare_messages(req, payload, x_title, forward_headers, upstream_request_url)
            if early is not None:
                for chunk in ac.stream_events(early):
                    yield chunk
                return
            window, rewriters, observers = channel_window(req.tricks)
            if window < 0:
                req.log.info("%sholding the reply for %s", request_tag(),
                             ", ".join(type(t).__name__ for t in rewriters if _wants_all(t)))
                ping = ac._sse("ping", {"type": "ping"})
                task = asyncio.create_task(self._finish_messages_buffered(req))
                yield ping   # right away, so the response has started
                try:
                    while True:
                        done, _ = await asyncio.wait({task}, timeout=HEARTBEAT_SECONDS)
                        if done:
                            break
                        yield ping
                finally:
                    if not task.done():
                        task.cancel()   # the client went away
                for chunk in ac.stream_events(task.result()):
                    yield chunk
            else:
                async for chunk in self._stream_messages(req, window, rewriters, observers):
                    yield chunk
        finally:
            self._end_messages(req)

    async def _stream_messages(self, req, window: int = 0, rewriters=(), observers=()):
        """Forward Anthropic's event stream. Without a window, byte for byte.
        With one, each text block passes through the rewriting post_hooks
        (its own window per block), and the tool_use blocks are held to the
        end and rewritten together, as on the OpenAI path: a rewriter may
        change them, or answer them itself and add text instead. Thinking and
        everything else is untouched. Either way the reply as sent is rebuilt
        for the observing post_hooks."""
        from petsitter import anthropic_compat as ac
        log, target = req.log, req.target
        body = dict(req.body, stream=True)
        if window:
            log.info("%sstreaming from upstream with a %d-character window: %s", request_tag(), window, target)
        else:
            log.info("%sstreaming from upstream: %s", request_tag(), target)
        trace_event("upstream", url=target, model=body.get("model", ""), stream=True, window=window)
        rewrite = self._window_rewrite(req, rewriters) if window else None
        texts: dict[int, ReplyWindow] = {}     # open text blocks
        tools: dict[int, dict] = {}            # tool_use blocks, held to the end
        # Held blocks leave gaps, so blocks are renumbered as they go out.
        out_index: dict[int, int] = {}
        next_out = [0]

        result: dict[str, Any] = {"type": "message", "role": "assistant", "content": []}
        partial: dict[int, str] = {}

        def sent(raw: str, ev) -> str:
            """Note an event going to the client, for the observers."""
            if not isinstance(ev, dict):
                return raw
            kind = ev.get("type")
            if kind == "message_start":
                result.update({k: v for k, v in (ev.get("message") or {}).items() if k != "content"})
            elif kind == "content_block_start":
                idx = ev.get("index", len(result["content"]))
                while len(result["content"]) <= idx:
                    result["content"].append({})
                result["content"][idx] = dict(ev.get("content_block") or {})
            elif kind == "content_block_delta":
                idx, d = ev.get("index", 0), ev.get("delta") or {}
                if idx < len(result["content"]):
                    block = result["content"][idx]
                    if d.get("type") == "text_delta":
                        block["text"] = block.get("text", "") + d.get("text", "")
                    elif d.get("type") == "thinking_delta":
                        block["thinking"] = block.get("thinking", "") + d.get("thinking", "")
                    elif d.get("type") == "input_json_delta":
                        partial[idx] = partial.get(idx, "") + d.get("partial_json", "")
            elif kind == "message_delta":
                result.update(ev.get("delta") or {})
            return raw

        def text_delta(idx: int, text: str):
            ev = {"type": "content_block_delta", "index": idx, "delta": {"type": "text_delta", "text": text}}
            return ac._sse("content_block_delta", ev), ev

        def renumbered(raw: str, ev: dict, idx: int):
            out = out_index.get(idx, idx)
            if out == idx:
                return raw, ev
            ev = dict(ev, index=out)
            return ac._sse(ev.get("type", ""), ev), ev

        def release_tools(stop_ev: dict | None) -> list:
            """The held tool calls, through the rewriters once, as events; then
            the message_delta, its stop_reason matching what was sent."""
            calls = [{"id": (h["start"][1].get("content_block") or {}).get("id", ""), "type": "function",
                      "function": {"name": (h["start"][1].get("content_block") or {}).get("name", ""),
                                   "arguments": h["json"]}} for h in tools.values()]
            tools.clear()
            text, calls = ReplyWindow(rewrite, window).finish(calls)
            events = []

            def block(start: dict, delta: dict | None):
                i = next_out[0]
                next_out[0] += 1
                for kind, ev in (("content_block_start", dict(start, index=i)),
                                 ("content_block_delta", dict(delta, index=i)) if delta else (None, None),
                                 ("content_block_stop", {"type": "content_block_stop", "index": i})):
                    if kind:
                        events.append((ac._sse(kind, ev), ev))

            if text:
                block({"type": "content_block_start", "content_block": {"type": "text", "text": ""}},
                      {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}})
            for call in calls or []:
                fn = call.get("function") or {}
                block({"type": "content_block_start", "content_block": {
                          "type": "tool_use", "id": call.get("id", ""), "name": fn.get("name", ""), "input": {}}},
                      {"type": "content_block_delta", "delta": {
                          "type": "input_json_delta", "partial_json": fn.get("arguments") or "{}"}})
            if stop_ev is not None:
                d = dict(stop_ev.get("delta") or {})
                if d.get("stop_reason") == "tool_use" and not calls:
                    d["stop_reason"] = "end_turn"   # a rewriter answered them
                stop_ev = dict(stop_ev, delta=d)
                events.append((ac._sse("message_delta", stop_ev), stop_ev))
            return events

        def through_window(raw: str, ev) -> list:
            """The events to send for one event from Anthropic."""
            if not isinstance(ev, dict):
                return [(raw, ev)]
            kind, idx = ev.get("type"), ev.get("index", 0)
            if kind == "content_block_start":
                block = ev.get("content_block") or {}
                if block.get("type") == "tool_use":
                    tools[idx] = {"start": (raw, ev), "json": ""}
                    return []
                out_index[idx] = next_out[0]
                next_out[0] += 1
                if block.get("type") == "text":
                    texts[idx] = ReplyWindow(rewrite, window)
                    if block.get("text"):
                        out = texts[idx].feed(block["text"])
                        ev = dict(ev, content_block=dict(block, text=out))
                        raw = ac._sse("content_block_start", ev)
            elif kind == "content_block_delta":
                d = ev.get("delta") or {}
                if idx in tools:
                    if d.get("type") == "input_json_delta":
                        tools[idx]["json"] += d.get("partial_json", "")
                    return []
                if idx in texts and d.get("type") == "text_delta":
                    out = texts[idx].feed(d.get("text", ""))
                    return [text_delta(out_index.get(idx, idx), out)] if out else []
            elif kind == "content_block_stop":
                if idx in tools:
                    return []
                if idx in texts:
                    tail, _ = texts.pop(idx).finish()
                    return ([text_delta(out_index.get(idx, idx), tail)] if tail else []) + [renumbered(raw, ev, idx)]
            if kind in ("content_block_start", "content_block_delta", "content_block_stop"):
                return [renumbered(raw, ev, idx)]
            return [(raw, ev)]

        def event(lines: list):
            raw = "\n".join(lines) + "\n\n"
            ev = None
            for line in lines:
                if line.startswith("data:"):
                    try:
                        ev = json.loads(line[5:].strip())
                    except ValueError:
                        pass
            return raw, ev

        async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT) as client:
            try:
                resp_cm = client.stream("POST", target, json=body, headers=req.headers)
                resp = await resp_cm.__aenter__()
            except httpx.TransportError as e:
                self._note_upstream(target, False, error=self._describe_transport_error(e, UPSTREAM_TIMEOUT.read))
                raise
            try:
                if resp.status_code >= 400:
                    detail = (await resp.aread()).decode("utf-8", "replace").strip()[:500]
                    self._note_upstream(target, False, resp.status_code, detail or "(empty body)")
                    raise ValueError(f"Anthropic returned {resp.status_code}: {detail}")
                try:
                    lines: list[str] = []
                    async for line in resp.aiter_lines():
                        if line:
                            lines.append(line)
                            continue
                        if not lines:
                            continue
                        raw, ev = event(lines)
                        lines = []
                        if rewrite and tools and isinstance(ev, dict) and ev.get("type") == "message_delta":
                            # the content is over: the held tool calls, rewritten (in a
                            # thread, as a rewriter may ask the model again)
                            for raw_out, ev_out in await asyncio.to_thread(release_tools, ev):
                                yield sent(raw_out, ev_out)
                            continue
                        for raw_out, ev_out in (through_window(raw, ev) if rewrite else [(raw, ev)]):
                            yield sent(raw_out, ev_out)
                    if lines:
                        raw, ev = event(lines)
                        for raw_out, ev_out in (through_window(raw, ev) if rewrite else [(raw, ev)]):
                            yield sent(raw_out, ev_out)
                    if rewrite and tools:   # the stream ended without a message_delta
                        for raw_out, ev_out in await asyncio.to_thread(release_tools, None):
                            yield sent(raw_out, ev_out)
                except httpx.TransportError as e:
                    self._note_upstream(target, False, error=self._describe_transport_error(e, UPSTREAM_TIMEOUT.read))
                    raise
                self._note_upstream(target, True, resp.status_code)
            finally:
                await resp_cm.__aexit__(None, None, None)
        if rewrite:
            for t in rewriters:
                trace_event("post_hook", t, windowed=True)
        for idx, raw in partial.items():
            try:
                result["content"][idx]["input"] = json.loads(raw) if raw.strip() else {}
            except (ValueError, IndexError):
                pass
        self._run_observers(req, observers, ac.response_to_assistant_message(result))
        log.info("%sstreamed reply done (stop=%s)", request_tag(), result.get("stop_reason"))

    async def models(self, upstream_url: str = "", forward_headers: dict | None = None) -> dict:
        if upstream_url:
            target = upstream_url
            headers = build_upstream_headers({"key": False}, extra_headers=forward_headers or {})
        else:
            default_cfg = get_model_config("default")
            base = default_cfg["url"]
            if not base:
                raise ValueError("No upstream model configured. Set a model URL via the dashboard.")
            # Same base-URL forms as chat (with or without /v1).
            target = None
            headers = self._build_headers(default_cfg)
        try:
            async with httpx.AsyncClient() as client:
                if target is None:
                    for root in api_root_candidates(base):
                        target = root + "/models"
                        response = await client.get(target, headers=headers, timeout=30.0)
                        if response.status_code != 404:
                            learn_api_root(base, root)
                            break
                else:
                    response = await client.get(target, headers=headers, timeout=30.0)
                if response.status_code >= 400:
                    self._note_upstream(target, False, response.status_code,
                                        (response.text or "").strip() or "(empty body)")
                response.raise_for_status()
                result = response.json()
        except httpx.TransportError as e:
            self._note_upstream(target, False, error=self._describe_transport_error(e, 30.0))
            raise ValueError(f"Error: {target} can't be reached: {e}") from e
        self._note_upstream(target, True, response.status_code)
        for name in self.tricksets:
            result.setdefault("data", []).append({
                "id": f"trickset/{name}",
                "object": "model",
                "created": 0,
                "owned_by": "petsitter",
            })
        return result

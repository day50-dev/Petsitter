"""OpenAI API proxy handling for petsitter."""

import json
import logging
import re
import time
from typing import Any

import httpx

from src.context import append_to_system_prompt
from src.trick import Trick, callmodel, configure, get_model_config
from src.trickset import Trickset

logger = logging.getLogger("petsitter")


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
            ts = Trickset("_default", "0.3.0", {"X-Title": "*", "Model": "*"}, [])
            ts.tricks = list(tricks)
            self.tricksets["_default"] = ts
        self._enabled: dict[str, bool] = {}
        configure(self.model_url, self.model_name or "", self.api_key)

    @property
    def tricks(self) -> list[Trick]:
        result: list[Trick] = []
        for ts in self.tricksets.values():
            result.extend(ts.tricks)
        return result

    def _matching_tricks(self, x_title: str, model: str) -> list[Trick]:
        tricks: list[Trick] = []
        seen: set[str] = set()
        default_ts = self.tricksets.get("_default")
        for name, ts in self.tricksets.items():
            if name == "_default":
                continue
            if ts.matches(x_title, model):
                for t in ts.tricks:
                    cls_name = type(t).__name__
                    if cls_name not in seen and self._enabled.get(cls_name, True):
                        tricks.append(t)
                        seen.add(cls_name)
        if not tricks and default_ts:
            for t in default_ts.tricks:
                cls_name = type(t).__name__
                if self._enabled.get(cls_name, True):
                    tricks.append(t)
        return tricks

    def _build_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _apply_system_prompt_tricks(self, system_prompt: str, tricks: list[Trick] | None = None) -> str:
        if tricks is None:
            tricks = self.tricks
        result = system_prompt
        for trick in tricks:
            addition = trick.system_prompt(result)
            if addition:
                result = result + "\n" + addition if result else addition
        return result

    def _apply_pre_hooks(self, context: list, params: dict, tricks: list[Trick] | None = None) -> list:
        if tricks is None:
            tricks = self.tricks
        result = context
        for trick in tricks:
            result = trick.pre_hook(result, params)
        return result

    def _apply_post_hooks(self, context: list, tricks: list[Trick] | None = None) -> list:
        if tricks is None:
            tricks = self.tricks
        result = context
        for trick in tricks:
            result = trick.post_hook(result)
        return result

    def _merge_capabilities(self, tricks: list[Trick] | None = None) -> dict:
        if tricks is None:
            tricks = self.tricks
        capabilities = {}
        for trick in tricks:
            capabilities = trick.info(capabilities)
        return capabilities

    def _filter_prompt_keywords(self, messages: list) -> tuple[list, dict | None]:
        registry: dict[str, Trick] = {}
        for t in self.tricks:
            kw = t.prompt_keyword
            if kw:
                registry[kw.lower()] = t

        if not registry:
            return messages, None

        modified = list(messages)
        for msg in reversed(modified):
            if msg.get("role") == "user" and isinstance(msg.get("content"), str):
                content = msg["content"]
                combined = "|".join(re.escape(k) for k in registry)
                pattern = re.compile(
                    r'\(' + r'(?:' + combined + r')' + r':\s*(.*?)\)',
                    re.IGNORECASE | re.DOTALL,
                )
                matches = list(pattern.finditer(content))
                if not matches:
                    break
                content = pattern.sub("", content).strip()
                content = re.sub(r' +', " ", content).strip()
                msg["content"] = content
                for m in matches:
                    keyword = m.group(0).split(":", 1)[0].lstrip("(").strip().lower()
                    request_text = m.group(1).strip()
                    trick = registry.get(keyword)
                    if not trick:
                        continue
                    try:
                        response = trick.handle_prompt_keyword(request_text)
                    except Exception as e:
                        logger.exception(f"prompt_keyword handler failed: {e}")
                        response = {
                            "role": "assistant",
                            "content": f"Error handling prompt keyword: {e}",
                        }
                    if isinstance(response, dict):
                        return modified, response
                break

        return messages, None

    def _filter_tricks_by_keywords(self, tricks: list[Trick], messages: list) -> tuple[list[Trick], list]:
        active: list[Trick] = []
        modified = list(messages)
        kw_tricks = [t for t in tricks if t.keywords]
        non_kw_tricks = [t for t in tricks if not t.keywords]

        for msg in reversed(modified):
            if msg.get("role") == "user" and isinstance(msg.get("content"), str):
                content = msg["content"]
                for trick in kw_tricks:
                    for kw in trick.keywords:
                        pattern = re.compile(r'\b' + re.escape(kw) + r'\b', re.IGNORECASE)
                        if pattern.search(content):
                            content = pattern.sub("", content)
                            if trick not in active:
                                active.append(trick)
                content = re.sub(r' +', ' ', content).strip()
                msg["content"] = content
                break

        return non_kw_tricks + active, modified

    def get_default_trickset(self) -> Trickset | None:
        for ts in self.tricksets.values():
            return ts
        return None

    def add_trick(self, path: str, ts_name: str | None = None) -> Trick:
        if ts_name:
            ts = self.tricksets.get(ts_name)
            if not ts:
                raise KeyError(f"Trickset '{ts_name}' not found")
        else:
            ts = self.get_default_trickset()
            if not ts:
                ts = Trickset("_default", "0.3.0", {"X-Title": "*", "Model": "*"}, [])
                self.tricksets["_default"] = ts
        trick = ts.add_trick(path)
        self._enabled[type(trick).__name__] = True
        return trick

    def remove_trick(self, class_name: str, ts_name: str | None = None) -> bool:
        if ts_name:
            ts = self.tricksets.get(ts_name)
            if not ts:
                return False
            return ts.remove_trick(class_name)
        for ts in self.tricksets.values():
            if ts.remove_trick(class_name):
                return True
        return False

    def reorder_trick(self, name: str, new_index: int, ts_name: str | None = None) -> bool:
        if ts_name:
            ts = self.tricksets.get(ts_name)
            if not ts:
                return False
            return ts.reorder_trick(name, new_index)
        for ts in self.tricksets.values():
            if ts.reorder_trick(name, new_index):
                return True
        return False

    def get_tricks_info(self) -> list[dict]:
        result = []
        for ts_name, ts in self.tricksets.items():
            for i, t in enumerate(ts.tricks):
                name = type(t).__name__
                path = ts.trick_paths[i] if i < len(ts.trick_paths) else ""
                result.append({
                    "name": name,
                    "display_name": getattr(t, "__display_name__", None) or name,
                    "brief": getattr(t, "__brief__", ""),
                    "module": type(t).__module__,
                    "trickset": ts_name,
                    "path": path,
                    "enabled": self._enabled.get(name, True),
                    "keywords": list(t.keywords),
                    "required_models": list(t.required_models),
                })
        return result

    def toggle_trick(self, name: str, enabled: bool | None = None) -> bool:
        for t in self.tricks:
            if type(t).__name__ == name:
                if enabled is None:
                    enabled = not self._enabled.get(name, True)
                self._enabled[name] = enabled
                return True
        return False

    async def chat_completions(self, payload: dict, x_title: str = "") -> dict:
        default_cfg = get_model_config("default")
        upstream_url = default_cfg["model_url"]
        if not upstream_url:
            raise ValueError("No upstream model configured. Set a model URL via the dashboard.")
        messages = payload.get("messages", [])

        messages, pk_response = self._filter_prompt_keywords(messages)
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
            else:
                tricks = []
        else:
            tricks = self._matching_tricks(x_title, model)

        tricks, messages = self._filter_tricks_by_keywords(tricks, messages)

        system_prompt = ""
        if messages and messages[0].get("role") == "system":
            system_prompt = messages[0].get("content", "")
            messages = messages[1:]

        new_system_prompt = self._apply_system_prompt_tricks(system_prompt, tricks)
        if new_system_prompt:
            messages = [{"role": "system", "content": new_system_prompt}] + messages

        messages = self._apply_pre_hooks(messages, payload, tricks)

        upstream_model = default_cfg["model_name"] or "default"
        upstream_payload = {
            "model": upstream_model,
            "messages": messages,
        }
        for key in ["temperature", "max_tokens"]:
            if key in payload:
                upstream_payload[key] = payload[key]

        logger.info(f"Calling upstream model: {upstream_url}/v1/chat/completions")
        logger.debug(f"Upstream payload: {json.dumps(upstream_payload, indent=2)}")

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{upstream_url}/v1/chat/completions",
                json=upstream_payload,
                headers=self._build_headers(),
                timeout=120.0,
            )

            logger.info(f"Upstream response status: {response.status_code}")
            logger.debug(f"Upstream response headers: {dict(response.headers)}")
            logger.debug(f"Upstream response body: {response.text[:500] if response.text else '(empty)'}")

            response.raise_for_status()

            if not response.content:
                logger.error(f"Empty response from upstream. Status: {response.status_code}")
                logger.error(f"Response headers: {dict(response.headers)}")
                raise ValueError(f"Upstream returned empty response (status {response.status_code})")

            result = response.json()

        logger.debug(f"Upstream response: {json.dumps(result, indent=2)}")

        assistant_message = result["choices"][0]["message"]
        context = messages + [assistant_message]

        logger.debug(f"Context before post-hooks: {json.dumps(context, indent=2)}")

        context = self._apply_post_hooks(context, tricks)
        logger.debug(f"Context after post-hooks: {json.dumps(context, indent=2)}")

        result["choices"][0]["message"] = context[-1]

        capabilities = self._merge_capabilities(tricks)
        if capabilities:
            result["capabilities"] = capabilities

        return result

    async def models(self) -> dict:
        default_cfg = get_model_config("default")
        upstream_url = default_cfg["model_url"]
        if not upstream_url:
            raise ValueError("No upstream model configured. Set a model URL via the dashboard.")
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{upstream_url}/v1/models",
                headers=self._build_headers(),
                timeout=30.0,
            )
            response.raise_for_status()
            result = response.json()
        for name in self.tricksets:
            result.setdefault("data", []).append({
                "id": f"trickset/{name}",
                "object": "model",
                "created": 0,
                "owned_by": "petsitter",
            })
        return result

"""OpenAI API proxy handling for petsitter."""

import json
import logging
from typing import Any

import httpx

from src.context import append_to_system_prompt
from src.trick import Trick, callmodel, configure
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
        self.model_url = model_url.rstrip("/")
        self.model_name = model_name
        self.api_key = api_key
        self.tricksets = tricksets or {}
        if tricks is not None and not self.tricksets:
            ts = Trickset("_default", "0.3.0", {"X-Title": "*", "Model": "*"}, [])
            ts.tricks = list(tricks)
            self.tricksets["_default"] = ts
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
        for name, ts in self.tricksets.items():
            if ts.matches(x_title, model):
                for t in ts.tricks:
                    cls_name = type(t).__name__
                    if cls_name not in seen:
                        tricks.append(t)
                        seen.add(cls_name)
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
        return ts.add_trick(path)

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
            for t in ts.tricks:
                result.append({
                    "name": type(t).__name__,
                    "module": type(t).__module__,
                    "trickset": ts_name,
                })
        return result

    async def chat_completions(self, payload: dict, x_title: str = "") -> dict:
        messages = payload.get("messages", [])
        model = payload.get("model", "")
        tricks = self._matching_tricks(x_title, model)

        system_prompt = ""
        if messages and messages[0].get("role") == "system":
            system_prompt = messages[0].get("content", "")
            messages = messages[1:]

        new_system_prompt = self._apply_system_prompt_tricks(system_prompt, tricks)
        if new_system_prompt:
            messages = [{"role": "system", "content": new_system_prompt}] + messages

        messages = self._apply_pre_hooks(messages, payload, tricks)

        upstream_payload = {
            "model": self.model_name or model or "default",
            "messages": messages,
        }
        for key in ["temperature", "max_tokens"]:
            if key in payload:
                upstream_payload[key] = payload[key]

        logger.info(f"Calling upstream model: {self.model_url}/v1/chat/completions")
        logger.debug(f"Upstream payload: {json.dumps(upstream_payload, indent=2)}")

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.model_url}/v1/chat/completions",
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
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.model_url}/v1/models",
                headers=self._build_headers(),
                timeout=30.0,
            )
            response.raise_for_status()
            return response.json()

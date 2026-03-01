"""OpenAI API proxy handling for petsitter."""

import json
import logging
from typing import Any

import httpx

from src.context import append_to_system_prompt
from src.trick import Trick, callmodel

logger = logging.getLogger("petsitter")


class ProxyHandler:
    """Handles proxied requests to the upstream model."""

    def __init__(
        self,
        model_url: str,
        model_name: str | None,
        api_key: str = "",
        tricks: list[Trick] | None = None,
    ):
        self.model_url = model_url.rstrip("/")
        self.model_name = model_name
        self.api_key = api_key
        self.tricks = tricks or []

    def _build_headers(self) -> dict[str, str]:
        """Build request headers."""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _apply_system_prompt_tricks(self, system_prompt: str) -> str:
        """Apply system_prompt hooks from all tricks."""
        result = system_prompt
        for trick in self.tricks:
            addition = trick.system_prompt(result)
            if addition:
                result = result + "\n" + addition if result else addition
        return result

    def _apply_pre_hooks(self, context: list, params: dict) -> list:
        """Apply pre_hook from all tricks."""
        result = context
        for trick in self.tricks:
            result = trick.pre_hook(result, params)
        return result

    def _apply_post_hooks(self, context: list) -> list:
        """Apply post_hook from all tricks."""
        result = context
        for trick in self.tricks:
            result = trick.post_hook(result)
        return result

    def _merge_capabilities(self) -> dict:
        """Merge capabilities from all tricks."""
        capabilities = {}
        for trick in self.tricks:
            capabilities = trick.info(capabilities)
        return capabilities

    async def chat_completions(self, payload: dict) -> dict:
        """Handle /v1/chat/completions request.

        Args:
            payload: The incoming request body.

        Returns:
            The response from the upstream model (possibly modified).
        """
        # Extract messages and apply system prompt tricks
        messages = payload.get("messages", [])
        system_prompt = ""
        if messages and messages[0].get("role") == "system":
            system_prompt = messages[0].get("content", "")
            messages = messages[1:]

        # Apply system prompt tricks
        new_system_prompt = self._apply_system_prompt_tricks(system_prompt)
        if new_system_prompt:
            messages = [{"role": "system", "content": new_system_prompt}] + messages

        # Apply pre-hooks
        messages = self._apply_pre_hooks(messages, payload)

        # Build upstream request
        upstream_payload = {
            "model": self.model_name or payload.get("model", "default"),
            "messages": messages,
        }
        # Pass through optional params (but force stream=false for upstream)
        for key in ["temperature", "max_tokens"]:
            if key in payload:
                upstream_payload[key] = payload[key]
        
        # Don't pass tools/stream to upstream - we handle tool calling via tricks
        # and always fetch full response for post-processing
        # upstream_payload["stream"] = False

        logger.info(f"Calling upstream model: {self.model_url}/v1/chat/completions")
        logger.debug(f"Upstream payload: {json.dumps(upstream_payload, indent=2)}")

        # Call upstream model
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.model_url}/v1/chat/completions",
                json=upstream_payload,
                headers=self._build_headers(),
                timeout=120.0,
            )
            
            # Log response details for debugging
            logger.info(f"Upstream response status: {response.status_code}")
            logger.debug(f"Upstream response headers: {dict(response.headers)}")
            logger.debug(f"Upstream response body: {response.text[:500] if response.text else '(empty)'}")
            
            response.raise_for_status()
            
            # Check for empty response
            if not response.content:
                logger.error(f"Empty response from upstream. Status: {response.status_code}")
                logger.error(f"Response headers: {dict(response.headers)}")
                raise ValueError(f"Upstream returned empty response (status {response.status_code})")
            
            result = response.json()

        logger.debug(f"Upstream response: {json.dumps(result, indent=2)}")

        # Extract assistant message and build context for post-hooks
        assistant_message = result["choices"][0]["message"]
        context = messages + [assistant_message]

        logger.debug(f"Context before post-hooks: {json.dumps(context, indent=2)}")

        # Apply post-hooks
        context = self._apply_post_hooks(context)

        logger.debug(f"Context after post-hooks: {json.dumps(context, indent=2)}")

        # Update result with potentially modified response
        result["choices"][0]["message"] = context[-1]

        # Merge capabilities into response if present
        capabilities = self._merge_capabilities()
        if capabilities:
            result["capabilities"] = capabilities

        return result

    async def models(self) -> dict:
        """Handle /v1/models request.

        Returns:
            Model listing response.
        """
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.model_url}/v1/models",
                headers=self._build_headers(),
                timeout=30.0,
            )
            response.raise_for_status()
            return response.json()

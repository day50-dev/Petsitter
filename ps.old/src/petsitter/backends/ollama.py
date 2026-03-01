"""Ollama backend client for PetSitter."""

from __future__ import annotations

import uuid
from datetime import datetime

import httpx

from petsitter.backends.base import LLMBackend
from petsitter.models import ChatRequest, ChatResponse, Message


class OllamaBackend(LLMBackend):
    """Ollama backend client."""

    name = "ollama"

    def __init__(self, base_url: str = "http://localhost:11434", model: str = "qwen3"):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=120.0,
            )
        return self._client

    async def chat(self, request: ChatRequest) -> ChatResponse:
        """Send a chat request to Ollama.

        Args:
            request: Chat request with messages

        Returns:
            Chat response from Ollama
        """
        client = await self._get_client()

        # Determine model to use
        model = request.model if request.model != "default" else self.model

        # Convert messages to Ollama format
        messages = []
        system_prompt = None

        for msg in request.messages:
            if msg.role.value == "system":
                system_prompt = msg.content
            else:
                messages.append({
                    "role": msg.role.value,
                    "content": msg.content,
                })

        # Build request body
        body = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": request.temperature,
            },
        }

        if system_prompt:
            body["system"] = system_prompt

        if request.max_tokens:
            body["options"]["num_predict"] = request.max_tokens

        # Make request
        response = await client.post("/api/chat", json=body)
        response.raise_for_status()

        data = response.json()

        # Extract response
        content = ""
        if "message" in data:
            content = data["message"].get("content", "")

        # Extract usage if available
        usage = None
        if "prompt_eval_count" in data or "eval_count" in data:
            usage = {
                "prompt_tokens": data.get("prompt_eval_count", 0),
                "completion_tokens": data.get("eval_count", 0),
            }

        return ChatResponse(
            content=content,
            model=model,
            usage=usage,
        )

    async def is_available(self) -> bool:
        """Check if Ollama is available."""
        try:
            client = await self._get_client()
            response = await client.get("/api/tags", timeout=5.0)
            return response.status_code == 200
        except Exception:
            return False

    async def get_models(self) -> list[str]:
        """Get list of available Ollama models."""
        try:
            client = await self._get_client()
            response = await client.get("/api/tags")
            response.raise_for_status()

            data = response.json()
            models = []

            for model in data.get("models", []):
                models.append(model.get("name", ""))

            return models
        except Exception:
            return []

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

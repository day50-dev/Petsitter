"""Backend interface for PetSitter."""

from __future__ import annotations

from abc import ABC, abstractmethod

from petsitter.models import ChatRequest, ChatResponse


class LLMBackend(ABC):
    """Abstract base class for LLM backends."""

    name: str = "base"

    @abstractmethod
    async def chat(self, request: ChatRequest) -> ChatResponse:
        """Send a chat request to the backend.

        Args:
            request: Chat request with messages

        Returns:
            Chat response from the backend
        """
        ...

    @abstractmethod
    async def is_available(self) -> bool:
        """Check if the backend is available.

        Returns:
            True if backend is reachable
        """
        ...

    @abstractmethod
    async def get_models(self) -> list[str]:
        """Get list of available models.

        Returns:
            List of model names
        """
        ...

"""Tests for petsitter proxy handler."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.proxy import ProxyHandler
from src.trick import Trick


class MockTrick(Trick):
    """Mock trick for testing."""

    def __init__(self, name: str = "mock"):
        self.name = name

    def system_prompt(self, to_add: str) -> str:
        return f"[{self.name} system]"

    def info(self, capabilities: dict) -> dict:
        capabilities[self.name] = True
        return capabilities


def create_mock_response(data: dict) -> MagicMock:
    """Create a mock httpx Response."""
    mock = MagicMock()
    mock.json.return_value = data
    mock.status_code = 200
    return mock


class TestProxyHandler:
    """Tests for ProxyHandler."""

    def test_init_basic(self):
        """ProxyHandler initializes with required params."""
        handler = ProxyHandler(
            model_url="http://localhost:11434",
            model_name="test-model",
        )
        assert handler.model_url == "http://localhost:11434"
        assert handler.model_name == "test-model"
        assert handler.tricks == []

    def test_init_with_tricks(self):
        """ProxyHandler initializes with tricks."""
        trick = MockTrick()
        handler = ProxyHandler(
            model_url="http://localhost:11434",
            model_name="test-model",
            tricks=[trick],
        )
        assert len(handler.tricks) == 1

    def test_apply_system_prompt_tricks(self):
        """System prompt tricks are applied."""
        trick = MockTrick("test")
        handler = ProxyHandler(
            model_url="http://localhost:11434",
            model_name="test",
            tricks=[trick],
        )
        result = handler._apply_system_prompt_tricks("original")
        assert "original" in result
        assert "[test system]" in result

    def test_apply_system_prompt_tricks_empty(self):
        """System prompt tricks work with empty initial prompt."""
        trick = MockTrick("test")
        handler = ProxyHandler(
            model_url="http://localhost:11434",
            model_name="test",
            tricks=[trick],
        )
        result = handler._apply_system_prompt_tricks("")
        assert "[test system]" in result

    def test_merge_capabilities(self):
        """Capabilities are merged from all tricks."""
        trick1 = MockTrick("trick1")
        trick2 = MockTrick("trick2")
        handler = ProxyHandler(
            model_url="http://localhost:11434",
            model_name="test",
            tricks=[trick1, trick2],
        )
        caps = handler._merge_capabilities()
        assert caps.get("trick1") is True
        assert caps.get("trick2") is True

    @pytest.mark.asyncio
    async def test_chat_completions_basic(self):
        """Chat completions proxies to upstream."""
        handler = ProxyHandler(
            model_url="http://localhost:11434",
            model_name="test-model",
        )

        mock_response = create_mock_response({
            "choices": [{"message": {"role": "assistant", "content": "Hello!"}}]
        })

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            payload = {
                "messages": [{"role": "user", "content": "Hi"}],
                "temperature": 0.7,
            }
            result = await handler.chat_completions(payload)

            assert result["choices"][0]["message"]["content"] == "Hello!"
            mock_client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_chat_completions_applies_tricks(self):
        """Chat completions applies trick transformations."""

        class TestTrick(Trick):
            def post_hook(self, context: list) -> list:
                context[-1]["content"] = context[-1]["content"] + " [modified]"
                return context

        handler = ProxyHandler(
            model_url="http://localhost:11434",
            model_name="test-model",
            tricks=[TestTrick()],
        )

        mock_response = create_mock_response({
            "choices": [{"message": {"role": "assistant", "content": "Hello!"}}]
        })

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            payload = {"messages": [{"role": "user", "content": "Hi"}]}
            result = await handler.chat_completions(payload)

            assert result["choices"][0]["message"]["content"] == "Hello! [modified]"

    @pytest.mark.asyncio
    async def test_models(self):
        """Models endpoint proxies to upstream."""
        handler = ProxyHandler(
            model_url="http://localhost:11434",
            model_name="test-model",
        )

        mock_response = create_mock_response({"data": [{"id": "test-model"}]})

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await handler.models()
            assert "data" in result

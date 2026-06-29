"""Tests for petsitter proxy handler."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.proxy import ProxyHandler
from src.trick import Trick


class KeywordTrick(Trick):
    """Mock trick with keyword activation."""

    keywords = ["multiround"]

    def __init__(self, name: str = "kw_trick"):
        self.name = name

    def system_prompt(self, to_add: str) -> str:
        return f"[{self.name} system]"

    def info(self, capabilities: dict) -> dict:
        capabilities[self.name] = True
        return capabilities


class MultiKeywordTrick(Trick):
    """Mock trick with multiple keywords."""

    keywords = ["alpha", "beta"]

    def __init__(self, name: str = "multi_kw"):
        self.name = name


class NonKeywordTrick(Trick):
    """Mock trick without keywords (always active)."""

    def __init__(self, name: str = "always_on"):
        self.name = name

    def system_prompt(self, to_add: str) -> str:
        return f"[{self.name} system]"

    def info(self, capabilities: dict) -> dict:
        capabilities[self.name] = True
        return capabilities


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


class TestKeywordActivation:
    """Tests for keyword-based trick activation."""

    def test_trick_without_keywords_always_active(self):
        handler = ProxyHandler("http://localhost:11434", "test")
        tricks = [NonKeywordTrick()]
        msgs = [{"role": "user", "content": "hello"}]
        active, modified = handler._filter_tricks_by_keywords(tricks, msgs)
        assert len(active) == 1
        assert active[0] is tricks[0]
        assert modified[0]["content"] == "hello"

    def test_trick_with_keyword_activates_when_present(self):
        handler = ProxyHandler("http://localhost:11434", "test")
        tricks = [KeywordTrick()]
        msgs = [{"role": "user", "content": "do multiround validation"}]
        active, modified = handler._filter_tricks_by_keywords(tricks, msgs)
        assert len(active) == 1
        assert active[0] is tricks[0]
        assert "multiround" not in modified[0]["content"]
        assert modified[0]["content"] == "do validation"

    def test_trick_with_keyword_skipped_when_absent(self):
        handler = ProxyHandler("http://localhost:11434", "test")
        tricks = [KeywordTrick()]
        msgs = [{"role": "user", "content": "regular message"}]
        active, modified = handler._filter_tricks_by_keywords(tricks, msgs)
        assert len(active) == 0

    def test_mixed_keyword_and_non_keyword_tricks(self):
        handler = ProxyHandler("http://localhost:11434", "test")
        kw = KeywordTrick()
        always = NonKeywordTrick()
        tricks = [always, kw]
        msgs = [{"role": "user", "content": "do multiround validation"}]
        active, modified = handler._filter_tricks_by_keywords(tricks, msgs)
        assert len(active) == 2
        assert always in active
        assert kw in active
        assert "multiround" not in modified[0]["content"]

    def test_multiple_keywords_one_trick(self):
        handler = ProxyHandler("http://localhost:11434", "test")
        trick = MultiKeywordTrick()
        msgs = [{"role": "user", "content": "use alpha mode"}]
        active, modified = handler._filter_tricks_by_keywords([trick], msgs)
        assert len(active) == 1
        assert "alpha" not in modified[0]["content"]

    def test_either_keyword_triggers(self):
        handler = ProxyHandler("http://localhost:11434", "test")
        trick = MultiKeywordTrick()
        msgs = [{"role": "user", "content": "run beta test"}]
        active, modified = handler._filter_tricks_by_keywords([trick], msgs)
        assert len(active) == 1
        assert "beta" not in modified[0]["content"]

    def test_case_insensitive_matching(self):
        handler = ProxyHandler("http://localhost:11434", "test")
        trick = KeywordTrick()
        msgs = [{"role": "user", "content": "MultiRound please"}]
        active, modified = handler._filter_tricks_by_keywords([trick], msgs)
        assert len(active) == 1
        assert "MultiRound" not in modified[0]["content"]

    def test_word_boundary_no_partial_match(self):
        handler = ProxyHandler("http://localhost:11434", "test")
        trick = KeywordTrick()
        msgs = [{"role": "user", "content": "multirounding is not valid"}]
        active, modified = handler._filter_tricks_by_keywords([trick], msgs)
        assert len(active) == 0
        assert "multirounding" in modified[0]["content"]

    def test_only_last_user_message_checked(self):
        handler = ProxyHandler("http://localhost:11434", "test")
        trick = KeywordTrick()
        msgs = [
            {"role": "user", "content": "multiround first message"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "no keyword here"},
        ]
        active, modified = handler._filter_tricks_by_keywords([trick], msgs)
        assert len(active) == 0

    def test_keyword_in_system_message_ignored(self):
        handler = ProxyHandler("http://localhost:11434", "test")
        trick = KeywordTrick()
        msgs = [
            {"role": "system", "content": "use multiround mode"},
            {"role": "user", "content": "hello"},
        ]
        active, modified = handler._filter_tricks_by_keywords([trick], msgs)
        assert len(active) == 0

    @pytest.mark.asyncio
    async def test_keyword_stripped_before_upstream(self):
        class KwTrick(Trick):
            keywords = ["multiround"]
            def system_prompt(self, to_add: str) -> str:
                return "[kw mode]"

        handler = ProxyHandler(
            "http://localhost:11434", "test",
            tricks=[KwTrick()],
        )
        mock_response = create_mock_response({
            "choices": [{"message": {"role": "assistant", "content": "done"}}]
        })
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            payload = {"messages": [{"role": "user", "content": "do multiround now"}]}
            result = await handler.chat_completions(payload)
            call_kwargs = mock_client.post.call_args[1]
            sent_messages = call_kwargs["json"]["messages"]
            user_msg = [m for m in sent_messages if m["role"] == "user"][0]
            assert "multiround" not in user_msg["content"]
            assert user_msg["content"] == "do now"

    @pytest.mark.asyncio
    async def test_keyword_trick_skipped_without_keyword(self):
        class KwTrick(Trick):
            keywords = ["multiround"]
            def post_hook(self, context: list) -> list:
                context[-1]["content"] += " [activated]"
                return context

        handler = ProxyHandler(
            "http://localhost:11434", "test",
            tricks=[KwTrick()],
        )
        mock_response = create_mock_response({
            "choices": [{"message": {"role": "assistant", "content": "done"}}]
        })
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            payload = {"messages": [{"role": "user", "content": "regular request"}]}
            result = await handler.chat_completions(payload)
            assert result["choices"][0]["message"]["content"] == "done"

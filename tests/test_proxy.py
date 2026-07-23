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


class TestFindPromptKeywordPatterns:
    """Tests for _find_prompt_keyword_patterns."""

    def test_basic_pattern(self):
        handler = ProxyHandler("http://localhost:11434", "test")
        result = handler._find_prompt_keyword_patterns("(test: hello)")
        assert len(result) == 1
        assert result[0]["keyword"] == "test"
        assert result[0]["request"] == "hello"

    def test_no_pattern(self):
        handler = ProxyHandler("http://localhost:11434", "test")
        result = handler._find_prompt_keyword_patterns("just regular text")
        assert result == []

    def test_nested_parens(self):
        handler = ProxyHandler("http://localhost:11434", "test")
        result = handler._find_prompt_keyword_patterns("(test: hello (world) foo)")
        assert len(result) == 1
        assert result[0]["request"] == "hello (world) foo"

    def test_deeply_nested(self):
        handler = ProxyHandler("http://localhost:11434", "test")
        result = handler._find_prompt_keyword_patterns("(test: a (b (c) d) e)")
        assert len(result) == 1
        assert result[0]["request"] == "a (b (c) d) e"

    def test_multiple_patterns(self):
        handler = ProxyHandler("http://localhost:11434", "test")
        result = handler._find_prompt_keyword_patterns("foo (test: one) bar (test: two)")
        assert len(result) == 2
        assert result[0]["request"] == "one"
        assert result[1]["request"] == "two"

    def test_multiple_with_nested(self):
        handler = ProxyHandler("http://localhost:11434", "test")
        result = handler._find_prompt_keyword_patterns("(test: a (b) c) and (test: d (e) f)")
        assert len(result) == 2
        assert result[0]["request"] == "a (b) c"
        assert result[1]["request"] == "d (e) f"

    def test_keyword_with_slash(self):
        handler = ProxyHandler("http://localhost:11434", "test")
        result = handler._find_prompt_keyword_patterns("(my/command: hello)")
        assert len(result) == 1
        assert result[0]["keyword"] == "my/command"

    def test_unbalanced_parens(self):
        handler = ProxyHandler("http://localhost:11434", "test")
        result = handler._find_prompt_keyword_patterns("(test: hello (world)")
        assert result == []

    def test_no_parens_at_all(self):
        handler = ProxyHandler("http://localhost:11434", "test")
        result = handler._find_prompt_keyword_patterns("test: hello)")
        assert result == []

    def test_missing_space_after_colon(self):
        handler = ProxyHandler("http://localhost:11434", "test")
        result = handler._find_prompt_keyword_patterns("(test:hello)")
        assert result == []

    def test_empty_request(self):
        handler = ProxyHandler("http://localhost:11434", "test")
        result = handler._find_prompt_keyword_patterns("(test: )")
        assert len(result) == 1
        assert result[0]["request"] == ""

    def test_keyword_with_underscore(self):
        handler = ProxyHandler("http://localhost:11434", "test")
        result = handler._find_prompt_keyword_patterns("(my_keyword: hello)")
        assert len(result) == 1
        assert result[0]["keyword"] == "my_keyword"

    def test_keyword_with_digits(self):
        handler = ProxyHandler("http://localhost:11434", "test")
        result = handler._find_prompt_keyword_patterns("(cmd42: hello)")
        assert len(result) == 1
        assert result[0]["keyword"] == "cmd42"

    def test_start_and_end_positions(self):
        handler = ProxyHandler("http://localhost:11434", "test")
        text = "prefix (kw: hello) suffix"
        result = handler._find_prompt_keyword_patterns(text)
        assert len(result) == 1
        assert text[result[0]["start"]:result[0]["end"]] == "(kw: hello)"

    def test_pattern_adjacent_to_text(self):
        handler = ProxyHandler("http://localhost:11434", "test")
        text = "before(kw: hi)after"
        result = handler._find_prompt_keyword_patterns(text)
        assert len(result) == 1
        assert result[0]["request"] == "hi"
        assert text[result[0]["start"]:result[0]["end"]] == "(kw: hi)"


class TestFilterPromptKeywords:
    """Tests for _filter_prompt_keywords."""

    def test_recognized_keyword_returns_response(self):
        class PromptTrick(Trick):
            prompt_keyword = "cmd"
            def handle_prompt_keyword(self, request: str, messages: list | None = None, payload: dict | None = None) -> dict | None:
                return {"role": "assistant", "content": f"handled: {request}"}

        handler = ProxyHandler("http://localhost:11434", "test", tricks=[PromptTrick()])
        messages = [{"role": "user", "content": "hello (cmd: do thing)"}]
        modified, response = handler._filter_prompt_keywords(messages)
        assert modified[0]["content"] == "hello"
        assert response == {"role": "assistant", "content": "handled: do thing"}

    def test_handler_returns_none_strips_pattern(self):
        class PromptTrick(Trick):
            prompt_keyword = "cmd"
            def handle_prompt_keyword(self, request: str, messages: list | None = None, payload: dict | None = None) -> dict | None:
                return None

        handler = ProxyHandler("http://localhost:11434", "test", tricks=[PromptTrick()])
        messages = [{"role": "user", "content": "hello (cmd: do thing) world"}]
        modified, response = handler._filter_prompt_keywords(messages)
        assert "cmd" not in modified[0]["content"]
        assert modified[0]["content"] == "hello world"
        assert response is None

    def test_unrecognized_keyword_adds_system_note(self):
        handler = ProxyHandler("http://localhost:11434", "test")
        messages = [{"role": "user", "content": "hello (unknown: do thing)"}]
        modified, response = handler._filter_prompt_keywords(messages)
        assert response is None
        assert modified[0]["role"] == "system"
        assert "unrecognized prompt keyword" in modified[0]["content"]
        assert '"unknown"' in modified[0]["content"]

    def test_multiple_unrecognized_keywords(self):
        handler = ProxyHandler("http://localhost:11434", "test")
        messages = [{"role": "user", "content": "(foo: a) and (bar: b)"}]
        modified, response = handler._filter_prompt_keywords(messages)
        assert response is None
        assert modified[0]["role"] == "system"
        assert "unrecognized prompt keywords" in modified[0]["content"]
        assert '"foo"' in modified[0]["content"]
        assert '"bar"' in modified[0]["content"]

    def test_recognized_and_unrecognized(self):
        class PromptTrick(Trick):
            prompt_keyword = "cmd"
            def handle_prompt_keyword(self, request: str, messages: list | None = None, payload: dict | None = None) -> dict | None:
                return None

        handler = ProxyHandler("http://localhost:11434", "test", tricks=[PromptTrick()])
        messages = [{"role": "user", "content": "(cmd: hi) (unknown: bye)"}]
        modified, response = handler._filter_prompt_keywords(messages)
        assert "unrecognized prompt keyword" in modified[0]["content"]
        assert response is None

    def test_handler_raises_returns_error(self):
        class PromptTrick(Trick):
            prompt_keyword = "cmd"
            def handle_prompt_keyword(self, request: str, messages: list | None = None, payload: dict | None = None) -> dict | None:
                raise ValueError("oops")

        handler = ProxyHandler("http://localhost:11434", "test", tricks=[PromptTrick()])
        messages = [{"role": "user", "content": "(cmd: do thing)"}]
        modified, response = handler._filter_prompt_keywords(messages)
        assert "Error handling prompt keyword" in response["content"]
        assert "oops" in response["content"]

    def test_no_patterns_returns_none(self):
        handler = ProxyHandler("http://localhost:11434", "test")
        messages = [{"role": "user", "content": "just a normal message"}]
        modified, response = handler._filter_prompt_keywords(messages)
        assert modified[0]["content"] == "just a normal message"
        assert response is None

    def test_non_user_message_skipped(self):
        class PromptTrick(Trick):
            prompt_keyword = "cmd"
            def handle_prompt_keyword(self, request: str, messages: list | None = None, payload: dict | None = None) -> dict | None:
                return {"role": "assistant", "content": "done"}

        handler = ProxyHandler("http://localhost:11434", "test", tricks=[PromptTrick()])
        messages = [
            {"role": "system", "content": "(cmd: setup)"},
            {"role": "user", "content": "hello"},
        ]
        modified, response = handler._filter_prompt_keywords(messages)
        assert response is None  # prompt keyword in system is ignored

    def test_case_insensitive_keyword_matching(self):
        class PromptTrick(Trick):
            prompt_keyword = "cmd"
            def handle_prompt_keyword(self, request: str, messages: list | None = None, payload: dict | None = None) -> dict | None:
                return {"role": "assistant", "content": f"ok: {request}"}

        handler = ProxyHandler("http://localhost:11434", "test", tricks=[PromptTrick()])
        messages = [{"role": "user", "content": "(CMD: hello)"}]
        modified, response = handler._filter_prompt_keywords(messages)
        assert response["content"] == "ok: hello"

    def test_earliest_in_text_order_wins(self):
        class FirstTrick(Trick):
            prompt_keyword = "first"
            def handle_prompt_keyword(self, request: str, messages: list | None = None, payload: dict | None = None) -> dict | None:
                return {"role": "assistant", "content": f"first: {request}"}

        class SecondTrick(Trick):
            prompt_keyword = "second"
            def handle_prompt_keyword(self, request: str, messages: list | None = None, payload: dict | None = None) -> dict | None:
                return {"role": "assistant", "content": f"second: {request}"}

        handler = ProxyHandler("http://localhost:11434", "test", tricks=[FirstTrick(), SecondTrick()])
        messages = [{"role": "user", "content": "(second: b) and (first: a)"}]
        modified, response = handler._filter_prompt_keywords(messages)
        assert response["content"] == "second: b"

    def test_all_handlers_return_none(self):
        class PromptTrick(Trick):
            prompt_keyword = "cmd"
            def handle_prompt_keyword(self, request: str, messages: list | None = None, payload: dict | None = None) -> dict | None:
                return None

        handler = ProxyHandler("http://localhost:11434", "test", tricks=[PromptTrick()])
        messages = [{"role": "user", "content": "(cmd: hi) text (cmd: bye)"}]
        modified, response = handler._filter_prompt_keywords(messages)
        assert response is None
        assert "cmd" not in modified[0]["content"]
        assert modified[0]["content"] == "text"

    def test_nested_parens_in_prompt_keyword(self):
        class PromptTrick(Trick):
            prompt_keyword = "cmd"
            def handle_prompt_keyword(self, request: str, messages: list | None = None, payload: dict | None = None) -> dict | None:
                return {"role": "assistant", "content": f"got: {request}"}

        handler = ProxyHandler("http://localhost:11434", "test", tricks=[PromptTrick()])
        messages = [{"role": "user", "content": "prefix (cmd: hello (world) foo) suffix"}]
        modified, response = handler._filter_prompt_keywords(messages)
        assert response["content"] == "got: hello (world) foo"
        assert modified[0]["content"] == "prefix suffix"

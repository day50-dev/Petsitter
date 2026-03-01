"""Tests for petsitter tricks."""

import json
import pytest

from src.trick import Trick, callmodel
from tricks.json_mode import JsonModeTrick
from tricks.tool_call import ToolCallTrick
from tricks.list_files import ListFilesTrick


class TestTrick:
    """Tests for the base Trick class."""

    def test_base_trick_system_prompt(self):
        """Base Trick.system_prompt returns empty string."""
        trick = Trick()
        assert trick.system_prompt("existing") == ""

    def test_base_trick_pre_hook(self):
        """Base Trick.pre_hook returns context unchanged."""
        trick = Trick()
        context = [{"role": "user", "content": "hello"}]
        assert trick.pre_hook(context, {}) == context

    def test_base_trick_post_hook(self):
        """Base Trick.post_hook returns context unchanged."""
        trick = Trick()
        context = [{"role": "assistant", "content": "hi"}]
        assert trick.post_hook(context) == context

    def test_base_trick_info(self):
        """Base Trick.info returns capabilities unchanged."""
        trick = Trick()
        caps = {"existing": True}
        assert trick.info(caps) == caps


class TestJsonModeTrick:
    """Tests for JsonModeTrick."""

    def test_system_prompt_adds_instruction(self):
        """JsonModeTrick adds JSON formatting instructions."""
        trick = JsonModeTrick()
        result = trick.system_prompt("")
        assert "valid JSON" in result
        assert "markdown" in result

    def test_post_hook_valid_json(self):
        """JsonModeTrick passes through valid JSON."""
        trick = JsonModeTrick()
        context = [{"role": "assistant", "content": '{"key": "value"}'}]
        result = trick.post_hook(context)
        assert result[-1]["content"] == '{"key": "value"}'

    def test_post_hook_invalid_json_strips_markdown(self):
        """JsonModeTrick handles markdown-wrapped JSON."""
        trick = JsonModeTrick()
        context = [
            {"role": "assistant", "content": '```json\n{"key": "value"}\n```'}
        ]
        result = trick.post_hook(context)
        # Should strip markdown and parse
        assert result[-1]["content"] == '{"key": "value"}'

    def test_info_declares_capability(self):
        """JsonModeTrick declares json_mode capability."""
        trick = JsonModeTrick()
        caps = trick.info({})
        assert caps.get("json_mode") is True


class TestToolCallTrick:
    """Tests for ToolCallTrick."""

    def test_system_prompt_adds_instruction(self):
        """ToolCallTrick adds tool calling instructions."""
        trick = ToolCallTrick()
        result = trick.system_prompt("")
        assert "tools/call" in result
        assert "jsonrpc" in result

    def test_pre_hook_injects_tools(self):
        """ToolCallTrick injects tool definitions into context."""
        trick = ToolCallTrick()
        context = [{"role": "system", "content": "You are helpful"}]
        params = {
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "test_tool", "description": "A test"},
                }
            ]
        }
        result = trick.pre_hook(context, params)
        assert "test_tool" in result[0]["content"]

    def test_post_hook_parses_tool_call(self):
        """ToolCallTrick converts JSONRPC to OpenAI tool_call format."""
        trick = ToolCallTrick()
        context = [
            {
                "role": "assistant",
                "content": '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_weather","arguments":{"city":"NYC"}}}',
            }
        ]
        result = trick.post_hook(context)
        assert "tool_calls" in result[-1]
        assert result[-1]["tool_calls"][0]["function"]["name"] == "get_weather"
        assert result[-1]["content"] is None

    def test_post_hook_non_tool_response_unchanged(self):
        """ToolCallTrick leaves non-tool responses unchanged."""
        trick = ToolCallTrick()
        context = [{"role": "assistant", "content": "The weather is nice"}]
        result = trick.post_hook(context)
        assert "tool_calls" not in result[-1]

    def test_info_declares_capability(self):
        """ToolCallTrick declares tools_support capability."""
        trick = ToolCallTrick()
        caps = trick.info({})
        assert caps.get("tools_support") is True


class TestListFilesTrick:
    """Tests for ListFilesTrick."""

    def test_system_prompt(self):
        """ListFilesTrick adds list_files instructions."""
        trick = ListFilesTrick()
        result = trick.system_prompt("")
        assert "list_files" in result

    def test_pre_hook_adds_tool(self):
        """ListFilesTrick adds list_files tool definition."""
        trick = ListFilesTrick()
        context = [{"role": "user", "content": "hello"}]
        params = {}
        result = trick.pre_hook(context, params)
        # Tool should be added to params
        assert "tools" in params

    def test_info_declares_capabilities(self):
        """ListFilesTrick declares custom tools."""
        trick = ListFilesTrick()
        caps = trick.info({})
        assert caps.get("tools_support") is True
        assert "list_files" in caps.get("custom_tools", [])

"""Tests for the Anthropic Messages API surface.

Claude Code and anything else pointed at petsitter via ANTHROPIC_BASE_URL uses
``/v1/messages``, not ``/v1/chat/completions``. These check that the request is
translated into the shape tricks expect, that the ordinary pipeline runs over
it, and that what comes back is a valid Anthropic response either way.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from petsitter import anthropic_compat as ac
from petsitter.proxy import ProxyHandler
from petsitter.trick import Trick
from petsitter.trickset import SCHEMA, Trickset


ANTHROPIC_REPLY = {
    "id": "msg_01",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-4",
    "content": [{"type": "text", "text": "two files"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 12, "output_tokens": 3},
}


def _mock_post(captured: dict, reply=None, status=200):
    def make(*a, **kw):
        client = AsyncMock()
        async def post(url, json=None, headers=None, timeout=None):
            captured["url"] = url
            captured["body"] = json
            captured["headers"] = headers
            resp = AsyncMock()
            resp.status_code = status
            resp.json = lambda: (reply if reply is not None else ANTHROPIC_REPLY)
            resp.text = "err" if status >= 400 else ""
            return resp
        client.post = post
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        return client
    return make


def _request(**over):
    body = {
        "model": "claude-sonnet-4",
        "max_tokens": 1024,
        "system": "be terse",
        "messages": [{"role": "user", "content": "list ~/mp3"}],
        "tools": [{"name": "list_mp3s", "description": "List MP3s",
                   "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}}],
    }
    body.update(over)
    return body


class RecordingTrick(Trick):
    """Sees the request in the OpenAI shape, like every other trick."""

    def __init__(self):
        self.seen_messages = None
        self.seen_tools = None
        self.seen_answer = None

    def system_prompt(self, to_add):
        return "[house rules]"

    def pre_hook(self, context, params):
        self.seen_messages = [dict(m) for m in context]
        self.seen_tools = list(params.get("tools") or [])
        return context

    def post_hook(self, context):
        self.seen_answer = dict(context[-1]) if context else None
        return context


class GateTrick(Trick):
    def pre_hook(self, context, params):
        params["tools"] = []
        return context


class ShoutTrick(Trick):
    def post_hook(self, context):
        if context and context[-1].get("content"):
            context[-1]["content"] = context[-1]["content"].upper()
        return context


@pytest.mark.asyncio
async def test_pipeline_sees_openai_shape_and_anthropic_gets_its_own():
    trick = RecordingTrick()
    handler = ProxyHandler("http://unused", "m", tricks=[trick])
    captured = {}
    with patch("httpx.AsyncClient", _mock_post(captured)):
        result = await handler.messages(_request(), x_title="claude")

    # the trick saw OpenAI shapes
    assert trick.seen_messages[0]["role"] == "system"
    assert "house rules" in trick.seen_messages[0]["content"]
    assert trick.seen_tools[0]["function"]["name"] == "list_mp3s"
    assert "properties" in trick.seen_tools[0]["function"]["parameters"]

    # Anthropic got its own shape
    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    body = captured["body"]
    assert "be terse" in body["system"] and "house rules" in body["system"]
    assert body["tools"][0]["input_schema"]["properties"]["path"]["type"] == "string"
    assert body["max_tokens"] == 1024
    assert all(m["role"] != "system" for m in body["messages"])

    # and the caller gets an Anthropic response back
    assert result["type"] == "message"
    assert result["content"][0]["text"] == "two files"


@pytest.mark.asyncio
async def test_client_credentials_are_forwarded_and_petsitter_adds_none():
    handler = ProxyHandler("http://unused", "m", tricks=[])
    captured = {}
    headers = {"x-api-key": "sk-ant-caller", "anthropic-version": "2023-06-01",
               "host": "localhost:8080", "content-length": "99"}
    with patch("httpx.AsyncClient", _mock_post(captured)):
        await handler.messages(_request(), forward_headers=headers)
    sent = captured["headers"]
    assert sent["x-api-key"] == "sk-ant-caller"
    assert sent["anthropic-version"] == "2023-06-01"
    # hop-by-hop headers must not be replayed upstream
    assert "host" not in sent and "content-length" not in sent


@pytest.mark.asyncio
async def test_a_trick_can_withhold_tools_from_the_model():
    handler = ProxyHandler("http://unused", "m", tricks=[GateTrick()])
    captured = {}
    with patch("httpx.AsyncClient", _mock_post(captured)):
        await handler.messages(_request())
    assert "tools" not in captured["body"] or captured["body"]["tools"] == []


@pytest.mark.asyncio
async def test_post_hook_edits_reach_the_caller():
    handler = ProxyHandler("http://unused", "m", tricks=[ShoutTrick()])
    with patch("httpx.AsyncClient", _mock_post({})):
        result = await handler.messages(_request())
    assert result["content"][0]["text"] == "TWO FILES"


@pytest.mark.asyncio
async def test_tool_calls_survive_the_round_trip():
    reply = {**ANTHROPIC_REPLY, "stop_reason": "tool_use", "content": [
        {"type": "text", "text": "looking"},
        {"type": "tool_use", "id": "tu_9", "name": "list_mp3s", "input": {"path": "~/mp3"}},
    ]}
    trick = RecordingTrick()
    handler = ProxyHandler("http://unused", "m", tricks=[trick])
    with patch("httpx.AsyncClient", _mock_post({}, reply=reply)):
        result = await handler.messages(_request())
    assert [c["function"]["name"] for c in trick.seen_answer["tool_calls"]] == ["list_mp3s"]
    blocks = {b["type"] for b in result["content"]}
    assert blocks == {"text", "tool_use"}


@pytest.mark.asyncio
async def test_prior_tool_results_become_tool_messages():
    trick = RecordingTrick()
    handler = ProxyHandler("http://unused", "m", tricks=[trick])
    req = _request(messages=[
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tu_1", "name": "list_mp3s", "input": {}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_1", "content": "a.mp3"}]},
    ])
    with patch("httpx.AsyncClient", _mock_post({})):
        await handler.messages(req)
    roles = [m["role"] for m in trick.seen_messages]
    assert "tool" in roles
    tool_msg = next(m for m in trick.seen_messages if m["role"] == "tool")
    assert tool_msg["tool_call_id"] == "tu_1" and tool_msg["content"] == "a.mp3"


@pytest.mark.asyncio
async def test_upstream_failure_is_reported_not_swallowed():
    handler = ProxyHandler("http://unused", "m", tricks=[])
    with patch("httpx.AsyncClient", _mock_post({}, status=401)):
        with pytest.raises(ValueError, match="401"):
            await handler.messages(_request())


@pytest.mark.asyncio
async def test_trickset_filters_route_anthropic_traffic():
    ts = Trickset("claude-code", SCHEMA, {"X-Title": "*", "Model": "claude*"}, [])
    trick = RecordingTrick()
    ts.tricks = [trick]
    ts.trick_enabled = [True]
    handler = ProxyHandler("http://unused", "m", tricksets={"claude-code": ts})
    with patch("httpx.AsyncClient", _mock_post({})):
        await handler.messages(_request())
    assert trick.seen_messages is not None, "the claude* trickset should have matched"


class ExportItLikeTrick(Trick):
    """Stands in for exportit.py: short-circuits via a prompt keyword."""

    prompt_keyword = "exportit"

    def handle_prompt_keyword(self, request, messages=None, payload=None):
        return {"role": "assistant", "content": f"exported ({len(messages or [])} messages)"}


@pytest.mark.asyncio
async def test_prompt_keyword_short_circuits_anthropic_path_without_calling_upstream():
    ts = Trickset("claude-code", SCHEMA, {"X-Title": "*", "Model": "claude*"}, [])
    trick = ExportItLikeTrick()
    ts.tricks = [trick]
    ts.trick_enabled = [True]
    ts.trick_keywords = [None]
    handler = ProxyHandler("http://unused", "m", tricksets={"claude-code": ts})
    req = _request(messages=[{"role": "user", "content": "(exportit:) please"}])
    captured = {}
    with patch("httpx.AsyncClient", _mock_post(captured)):
        result = await handler.messages(req, x_title="claude")
    assert not captured, "upstream should never be called when a prompt keyword short-circuits"
    assert result["type"] == "message"
    assert result["role"] == "assistant"
    assert "exported" in result["content"][0]["text"]
    assert result["stop_reason"] == "end_turn"


@pytest.mark.asyncio
async def test_paused_forwards_untouched_on_the_anthropic_path_too():
    """The same kill switch, exercised through /v1/messages (Claude Code's path)."""
    ts = Trickset("claude-code", SCHEMA, {"X-Title": "*", "Model": "claude*"}, [])
    trick = ExportItLikeTrick()
    ts.tricks = [trick]
    ts.trick_enabled = [True]
    ts.trick_keywords = [None]
    handler = ProxyHandler("http://unused", "m", tricksets={"claude-code": ts})
    handler.paused = True
    req = _request(messages=[{"role": "user", "content": "(exportit:) please"}])
    captured = {}
    reply = {**ANTHROPIC_REPLY, "content": [{"type": "text", "text": "two files"}]}
    with patch("httpx.AsyncClient", _mock_post(captured, reply=reply)):
        result = await handler.messages(req, x_title="claude")
    assert captured, "paused should still forward to upstream, just untouched"
    sent_text = captured["body"]["messages"][0]["content"]
    assert sent_text == "(exportit:) please", "prompt keyword must not be stripped while paused"
    assert result["content"][0]["text"] == "two files"


def test_streaming_replays_a_complete_reply_as_events():
    events = list(ac.stream_events(ANTHROPIC_REPLY))
    names = [e.split("event: ", 1)[1].split("\n", 1)[0] for e in events]
    assert names[0] == "message_start" and names[-1] == "message_stop"
    assert "content_block_delta" in names
    for chunk in events:
        payload = chunk.split("data: ", 1)[1].strip()
        json.loads(payload)   # every frame must be valid JSON

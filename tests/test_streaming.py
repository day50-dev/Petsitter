"""Who holds the reply, and what a buffered reply looks like as a stream."""

import json

from petsitter.proxy import sse_from_result
from petsitter.trick import Trick
from petsitter.tricks.context_monitor import ContextMonitorTrick
from petsitter.tricks.logger import LoggerTrick
from petsitter.tricks.no_emdash import NoEmDashTrick
from petsitter.tricks.secrets_protector import SecretsProtectorTrick
from petsitter.tricks.tool_monitor import ToolMonitorTrick


def test_channel_window():
    from petsitter.reply_window import channel_window
    cm, log, tm = ContextMonitorTrick(), LoggerTrick(), ToolMonitorTrick()
    sp, ne = SecretsProtectorTrick(), NoEmDashTrick()

    class Whole(Trick):
        def post_hook(self, context):
            return context

    assert channel_window([cm, log, tm]) == (0, [], [log, tm])   # no post_hook / looks only
    assert channel_window([ne, log]) == (1, [ne], [log])
    assert channel_window([ne, sp])[0] == sp.needs_window >= 52   # the largest wins
    assert channel_window([ne, sp, Whole()])[0] == -1              # whole reply wins outright


def test_secrets_window_is_one_stand_in():
    sp = SecretsProtectorTrick()
    assert sp.needs_window == len(sp._marker("anything")) == 58


import random

from petsitter.reply_window import ReplyWindow


def _swap(msg):
    """A rewriter like Secrets Protector: stand-in -> value, and value -> value."""
    msg["content"] = msg["content"].replace("<<STAND-IN-7>>", "hunter2").replace("--", "-")
    for tc in msg.get("tool_calls") or []:
        tc["function"]["arguments"] = tc["function"]["arguments"].replace("<<STAND-IN-7>>", "hunter2")
    return msg


def test_window_rewrites_across_any_chunking():
    text = "pw is <<STAND-IN-7>> -- again <<STAND-IN-7>><<STAND-IN-7>> end--" * 5
    want = _swap({"content": text})["content"]
    rnd = random.Random(7)
    for _ in range(200):
        w, out, i = ReplyWindow(_swap, 14), "", 0
        while i < len(text):
            n = rnd.randint(1, 9)
            out += w.feed(text[i:i + n])
            i += n
        tail, _ = w.finish()
        assert out + tail == want


def test_window_holds_back_only_its_size():
    w = ReplyWindow(_swap, 14)
    sent = w.feed("a" * 100)
    assert sent == "a" * 86          # all but the window


def test_window_gives_tool_calls_whole_at_the_end():
    w = ReplyWindow(_swap, 14)
    call = {"id": "c", "type": "function", "function": {"name": "f", "arguments": '{"p": "<<STAND-IN-7>>"}'}}
    tail, calls = w.finish([call])
    assert calls[0]["function"]["arguments"] == '{"p": "hunter2"}'


def test_buffered_reply_as_events_keeps_tool_call_index():
    result = {"id": "x", "model": "m", "choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": "ok", "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "f", "arguments": "{}"}},
            {"id": "b", "type": "function", "function": {"name": "g", "arguments": "{}"}}]}}]}
    lines = list(sse_from_result(result))
    assert lines[-1] == "data: [DONE]\n\n"
    deltas = [json.loads(l[6:])["choices"][0]["delta"] for l in lines[:-1]]
    calls = next(d["tool_calls"] for d in deltas if "tool_calls" in d)
    assert [c["index"] for c in calls] == [0, 1]
    assert "".join(d.get("content", "") for d in deltas) == "ok"


# --- Anthropic /v1/messages -------------------------------------------------

import asyncio

import httpx
import pytest

import petsitter.proxy as proxy_mod
from petsitter.proxy import ProxyHandler

ANTHROPIC_EVENTS = [
    ("message_start", {"type": "message_start", "message": {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude",
        "content": [], "stop_reason": None, "usage": {"input_tokens": 3, "output_tokens": 0}}}),
    ("content_block_start", {"type": "content_block_start", "index": 0,
                             "content_block": {"type": "thinking", "thinking": ""}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                             "delta": {"type": "thinking_delta", "thinking": "hmm"}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    ("content_block_start", {"type": "content_block_start", "index": 1,
                             "content_block": {"type": "text", "text": ""}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 1,
                             "delta": {"type": "text_delta", "text": "Hello "}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 1,
                             "delta": {"type": "text_delta", "text": "there"}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 1}),
    ("content_block_start", {"type": "content_block_start", "index": 2,
                             "content_block": {"type": "tool_use", "id": "tu_1", "name": "Read", "input": {}}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 2,
                             "delta": {"type": "input_json_delta", "partial_json": '{"path": '}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 2,
                             "delta": {"type": "input_json_delta", "partial_json": '"a.txt"}'}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 2}),
    ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
                       "usage": {"output_tokens": 9}}),
    ("message_stop", {"type": "message_stop"}),
]
WHOLE = {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude",
         "content": [{"type": "text", "text": "Hello there"}], "stop_reason": "end_turn",
         "usage": {"input_tokens": 3, "output_tokens": 2}}


@pytest.fixture
def fake_anthropic(monkeypatch):
    """Anthropic's /v1/messages: streams when asked to, whole reply otherwise."""
    seen = []

    def handle(request):
        body = json.loads(request.content)
        seen.append(body)
        if body.get("stream"):
            text = "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in ANTHROPIC_EVENTS)
            return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=WHOLE)

    real = httpx.AsyncClient
    monkeypatch.setattr(proxy_mod.httpx, "AsyncClient",
                        lambda *a, **kw: real(*a, transport=httpx.MockTransport(handle), **kw))
    return seen


def _collect(handler, payload):
    async def go():
        return [c async for c in handler.messages_stream(payload)]
    return "".join(asyncio.run(go()))


class Watcher(Trick):
    needs_window = 0

    def __init__(self):
        self.replies = []

    def post_hook(self, context):
        self.replies.append(context[-1])
        return context


class Shouter(Trick):
    def post_hook(self, context):
        context[-1]["content"] = (context[-1].get("content") or "").upper()
        return context


PAYLOAD = {"model": "claude", "stream": True, "max_tokens": 50,
           "messages": [{"role": "user", "content": "hi"}]}


def test_messages_stream_forwards_anthropic_events_untouched(fake_anthropic):
    watcher = Watcher()
    handler = ProxyHandler(model_url="http://unused", model_name="m", tricks=[watcher])
    out = _collect(handler, PAYLOAD)
    assert fake_anthropic[-1]["stream"] is True
    # the client gets exactly what Anthropic sent, thinking block included
    for event, data in ANTHROPIC_EVENTS:
        assert f"event: {event}\ndata: {json.dumps(data)}\n" in out
    # and the observer saw the whole reply afterwards, tool call and all
    reply = watcher.replies[-1]
    assert reply["content"] == "Hello there"
    call = reply["tool_calls"][0]
    assert call["function"]["name"] == "Read"
    assert json.loads(call["function"]["arguments"]) == {"path": "a.txt"}


def test_messages_stream_holds_for_a_rewriter(fake_anthropic, monkeypatch):
    monkeypatch.setattr(proxy_mod, "HEARTBEAT_SECONDS", 0.01)
    handler = ProxyHandler(model_url="http://unused", model_name="m", tricks=[Shouter()])
    out = _collect(handler, PAYLOAD)
    assert "stream" not in fake_anthropic[-1]     # asked for the whole reply
    assert out.startswith('event: ping\ndata: {"type": "ping"}')
    assert "HELLO THERE" in out and "Hello there" not in out


class Swapper(Trick):
    needs_window = 14

    def post_hook(self, context):
        _swap(context[-1])
        return context


def _anthropic_text_events(pieces):
    evs = [ANTHROPIC_EVENTS[0],
           ("content_block_start", {"type": "content_block_start", "index": 0,
                                    "content_block": {"type": "text", "text": ""}})]
    evs += [("content_block_delta", {"type": "content_block_delta", "index": 0,
                                     "delta": {"type": "text_delta", "text": p}}) for p in pieces]
    evs += [("content_block_stop", {"type": "content_block_stop", "index": 0})] + ANTHROPIC_EVENTS[8:]
    return evs


def test_messages_stream_through_a_window(monkeypatch):
    text = "the password is <<STAND-IN-7>>, ok"
    pieces = [text[i:i + 3] for i in range(0, len(text), 3)]
    events = _anthropic_text_events(pieces)
    events[-5] = ("content_block_delta", {"type": "content_block_delta", "index": 2,
                  "delta": {"type": "input_json_delta", "partial_json": '{"p": "<<STAND-'}})
    events[-4] = ("content_block_delta", {"type": "content_block_delta", "index": 2,
                  "delta": {"type": "input_json_delta", "partial_json": 'IN-7>>"}'}})

    def handle(request):
        assert json.loads(request.content).get("stream") is True
        return httpx.Response(200, text="".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events),
                              headers={"content-type": "text/event-stream"})

    real = httpx.AsyncClient
    monkeypatch.setattr(proxy_mod.httpx, "AsyncClient",
                        lambda *a, **kw: real(*a, transport=httpx.MockTransport(handle), **kw))
    watcher = Watcher()
    handler = ProxyHandler(model_url="http://unused", model_name="m", tricks=[Swapper(), watcher])
    out = _collect(handler, PAYLOAD)
    texts, args = "", ""
    for block in out.split("\n\n"):
        for line in block.split("\n"):
            if line.startswith("data: "):
                d = json.loads(line[6:]).get("delta") or {}
                texts += d.get("text", "") if d.get("type") == "text_delta" else ""
                args += d.get("partial_json", "") if d.get("type") == "input_json_delta" else ""
    assert texts == "the password is hunter2, ok"
    assert json.loads(args) == {"p": "hunter2"}
    assert "STAND-IN" not in out
    assert watcher.replies[-1]["content"] == "the password is hunter2, ok"


def test_chat_stream_through_a_window(monkeypatch):
    text = "the password is <<STAND-IN-7>>, ok"
    pieces = [text[i:i + 4] for i in range(0, len(text), 4)]

    def ch(delta, finish=None):
        return "data: " + json.dumps({"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m",
                                      "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"

    body = ch({"role": "assistant"}) + "".join(ch({"content": p}) for p in pieces)
    body += ch({"tool_calls": [{"index": 0, "id": "t", "type": "function",
                                "function": {"name": "f", "arguments": '{"p": "<<STAND-'}}]})
    body += ch({"tool_calls": [{"index": 0, "function": {"arguments": 'IN-7>>"}'}}]})
    body += ch({}, "tool_calls") + "data: [DONE]\n\n"

    def handle(request):
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    real = httpx.AsyncClient
    monkeypatch.setattr(proxy_mod.httpx, "AsyncClient",
                        lambda *a, **kw: real(*a, transport=httpx.MockTransport(handle), **kw))
    watcher = Watcher()
    handler = ProxyHandler(model_url="http://upstream", model_name="m", tricks=[Swapper(), watcher])

    async def go():
        return [c async for c in handler.chat_completions_stream(
            {"model": "m", "stream": True, "messages": [{"role": "user", "content": "hi"}]})]
    out = asyncio.run(go())
    assert out[-1] == "data: [DONE]\n\n"
    chunks = [json.loads(l[6:]) for l in out[:-1] if l.startswith("data: ")]
    deltas = [c["choices"][0]["delta"] for c in chunks]
    assert "".join(d.get("content", "") for d in deltas) == "the password is hunter2, ok"
    calls = [tc for d in deltas for tc in d.get("tool_calls", [])]
    assert calls[0]["index"] == 0 and json.loads(calls[0]["function"]["arguments"]) == {"p": "hunter2"}
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert "STAND-IN" not in "".join(out)
    assert watcher.replies[-1]["content"] == "the password is hunter2, ok"


def _bare_root_upstream(monkeypatch, seen):
    """An API at the bare base (no /v1), like GitHub Models."""
    def handle(request):
        seen.append(str(request.url))
        if "/v1/" in request.url.path:
            return httpx.Response(404, text="not found")
        body = json.loads(request.content)
        if body.get("stream"):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=(
                'data: {"id":"c","choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":"stop"}]}\n\n'
                "data: [DONE]\n\n"))
        return httpx.Response(200, json={"id": "c", "choices": [{"index": 0, "finish_reason": "stop",
                                         "message": {"role": "assistant", "content": "hi"}}]})
    real = httpx.AsyncClient
    monkeypatch.setattr(proxy_mod.httpx, "AsyncClient",
                        lambda *a, **kw: real(*a, transport=httpx.MockTransport(handle), **kw))


@pytest.mark.parametrize("stream", [False, True])
def test_chat_finds_an_api_without_v1_and_remembers(monkeypatch, stream):
    import petsitter.trick as trick_mod
    from petsitter.trick import configure_modelset
    trick_mod._API_ROOTS.clear()
    configure_modelset({"default": {"url": "http://h/inference", "model": "m"}})
    seen = []
    _bare_root_upstream(monkeypatch, seen)
    handler = ProxyHandler(model_url="http://h/inference", model_name="m")
    payload = {"model": "m", "stream": stream, "messages": [{"role": "user", "content": "x"}]}

    async def go():
        if stream:
            return "".join([c async for c in handler.chat_completions_stream(payload)])
        return (await handler.chat_completions(payload))["choices"][0]["message"]["content"]
    try:
        assert "hi" in asyncio.run(go())
        assert seen == ["http://h/inference/v1/chat/completions", "http://h/inference/chat/completions"]
        seen.clear()
        asyncio.run(go())
        assert seen == ["http://h/inference/chat/completions"]   # learned
    finally:
        trick_mod._API_ROOTS.clear()
        configure_modelset({})


def test_parallel_calls_sharing_an_index_stay_separate():
    """Five parallel search_web calls that all say index 0 (some upstreams do):
    each named piece starts a call, so they don't glue into
    "search_websearch_web..." with their arguments run together."""
    from petsitter.proxy import add_tool_call_pieces, merge_tool_call_fragments
    pieces = []
    for q in ["a", "b", "c", "d", "e"]:
        pieces.append({"index": 0, "id": f"call_{q}", "type": "function",
                       "function": {"name": "search_web", "arguments": ""}})
        pieces.append({"index": 0, "function": {"arguments": '{"query": '}})
        pieces.append({"index": 0, "function": {"arguments": f'"{q}"}}'}})
    calls = []
    add_tool_call_pieces(calls, pieces)
    assert [c["function"]["name"] for c in calls] == ["search_web"] * 5
    assert [json.loads(c["function"]["arguments"])["query"] for c in calls] == list("abcde")
    assert [c["id"] for c in calls] == [f"call_{q}" for q in "abcde"]
    assert merge_tool_call_fragments(pieces) == calls        # the non-streaming repair agrees


def test_chat_stream_window_keeps_parallel_calls_apart(monkeypatch):
    def ch(delta, finish=None):
        return "data: " + json.dumps({"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m",
                                      "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"
    body = ch({"role": "assistant"})
    for q in "abc":
        body += ch({"tool_calls": [{"index": 0, "id": f"call_{q}", "type": "function",
                                    "function": {"name": "search_web", "arguments": ""}}]})
        body += ch({"tool_calls": [{"index": 0, "function": {"arguments": f'{{"query": "{q}"}}'}}]})
    body += ch({}, "tool_calls") + "data: [DONE]\n\n"

    real = httpx.AsyncClient
    monkeypatch.setattr(proxy_mod.httpx, "AsyncClient", lambda *a, **kw: real(
        *a, transport=httpx.MockTransport(lambda r: httpx.Response(200, text=body,
                                          headers={"content-type": "text/event-stream"})), **kw))
    handler = ProxyHandler(model_url="http://upstream", model_name="m", tricks=[Swapper()])

    async def go():
        return [c async for c in handler.chat_completions_stream(
            {"model": "m", "stream": True, "messages": [{"role": "user", "content": "search"}]})]
    out = asyncio.run(go())
    deltas = [json.loads(l[6:])["choices"][0]["delta"] for l in out if l.startswith("data: {")]
    calls = [tc for d in deltas for tc in d.get("tool_calls", [])]
    assert [c["function"]["name"] for c in calls] == ["search_web"] * 3
    assert [c["index"] for c in calls] == [0, 1, 2]          # re-numbered for the client
    assert [json.loads(c["function"]["arguments"])["query"] for c in calls] == list("abc")

"""One ID per request, from arrival to reply, visible to tricks."""

import asyncio
import json

import httpx

import petsitter.proxy as proxy_mod
from petsitter.observability import current_request_id, reset_request_id, set_request_id
from petsitter.proxy import ProxyHandler
from petsitter.server import _NormalizeV1Path
from petsitter.trick import Trick
from petsitter.tricks.logger import LoggerTrick


def test_the_edge_assigns_an_id_on_arrival():
    seen = []

    async def app(scope, receive, send):
        seen.append(current_request_id())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def call(path):
        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(_):
            pass
        await _NormalizeV1Path(app)({"type": "http", "path": path, "headers": []}, receive, send)

    async def go():
        # each request in its own task, as a server runs them
        await asyncio.create_task(call("/v1/chat/completions"))
        await asyncio.create_task(call("/v1/messages"))
    asyncio.run(go())
    assert all(len(rid) == 8 for rid in seen) and seen[0] != seen[1]


def test_tricks_and_the_traffic_logger_keep_the_arrival_id(tmp_path, monkeypatch):
    def handle(request):
        return httpx.Response(200, json={"id": "c", "choices": [{"index": 0, "finish_reason": "stop",
                              "message": {"role": "assistant", "content": "hi"}}]})
    real = httpx.AsyncClient
    monkeypatch.setattr(proxy_mod.httpx, "AsyncClient",
                        lambda *a, **kw: real(*a, transport=httpx.MockTransport(handle), **kw))

    seen = []

    class Peek(Trick):
        def pre_hook(self, context, params):
            seen.append(self.request_id)
            return context

    log = LoggerTrick()
    log.path = str(tmp_path)
    handler = ProxyHandler(model_url="http://upstream", model_name="m", tricks=[log, Peek()])

    token = set_request_id("edge1234")
    try:
        asyncio.run(handler.chat_completions({"model": "m", "messages": [{"role": "user", "content": "x"}]}))
    finally:
        reset_request_id(token)

    records = [json.loads(l) for f in ("before.jsonl", "after.jsonl")
               for l in (tmp_path / f).read_text().splitlines()]
    assert [r["event"] for r in records] == ["request", "response"]
    assert {r["request_id"] for r in records} == {"edge1234"}
    assert seen == ["edge1234"]
    assert Trick().request_id == ""   # outside a request

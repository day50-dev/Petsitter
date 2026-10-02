"""Tests for the /use/ path-prefix transparent proxy."""

import json

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from petsitter.proxy import ProxyHandler
from petsitter.server import _parse_use_path, create_app


class TestParsePPath:
    """Tests for _parse_use_path."""

    def test_host_and_subpath(self):
        assert _parse_use_path("/use/build.nvidia.com/v1/chat/completions") == ("build.nvidia.com", "/v1/chat/completions")

    def test_host_only(self):
        assert _parse_use_path("/use/build.nvidia.com") == ("build.nvidia.com", "")

    def test_host_with_trailing_slash(self):
        assert _parse_use_path("/use/build.nvidia.com/v1/") == ("build.nvidia.com", "/v1/")

    def test_host_with_port(self):
        assert _parse_use_path("/use/localhost:11434/v1/models") == ("localhost:11434", "/v1/models")

    def test_not_a_p_path(self):
        assert _parse_use_path("/v1/chat/completions") is None

    def test_empty_host(self):
        assert _parse_use_path("/use//v1/chat/completions") is None

    def test_invalid_host_chars(self):
        assert _parse_use_path("/use/ht tp.com/v1") is None

    def test_double_slash_absorbed(self):
        assert _parse_use_path("/use/build.nvidia.com//v1/chat/completions") == ("build.nvidia.com", "/v1/chat/completions")


def create_mock_response(data: dict) -> MagicMock:
    """Create a mock httpx Response."""
    mock = MagicMock()
    mock.json.return_value = data
    mock.status_code = 200
    mock.content = b"{}"
    return mock


class TestUsePathHandler:
    """Tests for /use/ upstream overrides on ProxyHandler."""

    @pytest.mark.asyncio
    async def test_chat_completions_uses_override_url(self):
        """chat_completions targets the /use/ upstream and forwards client auth + model."""
        handler = ProxyHandler(model_url="", model_name=None, api_key="")

        mock_response = create_mock_response({
            "choices": [{"message": {"role": "assistant", "content": "Hello!"}}]
        })
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await handler.chat_completions(
                {"messages": [{"role": "user", "content": "Hi"}], "model": "llama3:8b"},
                upstream_request_url="https://build.nvidia.com/v1/chat/completions",
                forward_headers={"Authorization": "Bearer sk-client"},
            )

            assert result["choices"][0]["message"]["content"] == "Hello!"
            args = mock_client.post.call_args
            assert args.args[0] == "https://build.nvidia.com/v1/chat/completions"
            assert args.kwargs["headers"]["Authorization"] == "Bearer sk-client"
            assert args.kwargs["json"]["model"] == "llama3:8b"

    @pytest.mark.asyncio
    async def test_models_uses_override_url(self):
        """models() targets the /use/ upstream."""
        handler = ProxyHandler(model_url="", model_name=None, api_key="")

        mock_response = create_mock_response({"data": [{"id": "real-model"}]})
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await handler.models(
                upstream_url="https://build.nvidia.com/v1/models",
                forward_headers={"Authorization": "Bearer sk-client"},
            )

            ids = [m["id"] for m in result["data"]]
            assert "real-model" in ids
            assert mock_client.get.call_args.args[0] == "https://build.nvidia.com/v1/models"


class TestUsePathEndpoint:
    """Tests for the /use/ server route."""

    @pytest.mark.asyncio
    async def test_use_route_chat_completions(self):
        """POST /use/<host>/... proxies through the pipeline to https://<host>/..."""
        from httpx import AsyncClient, ASGITransport

        app = create_app(model_url="", model_name=None, api_key="", trick_paths=[])

        mock_response = create_mock_response({
            "choices": [{"message": {"role": "assistant", "content": "Hello from nvidia!"}}]
        })
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                response = await ac.post(
                    "/use/build.nvidia.com/v1/chat/completions",
                    json={"messages": [{"role": "user", "content": "Hi"}], "model": "nvidia-model"},
                    headers={"Authorization": "Bearer sk-nvidia"},
                )
                assert response.status_code == 200
                assert response.json()["choices"][0]["message"]["content"] == "Hello from nvidia!"
                args = mock_client.post.call_args
                assert args.args[0] == "https://build.nvidia.com/v1/chat/completions"
                assert args.kwargs["headers"]["Authorization"] == "Bearer sk-nvidia"

    @pytest.mark.asyncio
    async def test_use_route_models(self):
        """GET /use/<host>/models proxies to https://<host>/models."""
        from httpx import AsyncClient, ASGITransport

        app = create_app(model_url="", model_name=None, api_key="", trick_paths=[])

        mock_response = create_mock_response({"data": [{"id": "nv-1"}]})
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                response = await ac.get(
                    "/use/build.nvidia.com/v1/models",
                    headers={"Authorization": "Bearer sk-nvidia"},
                )
                assert response.status_code == 200
                ids = [m["id"] for m in response.json()["data"]]
                assert "nv-1" in ids
                assert mock_client.get.call_args.args[0] == "https://build.nvidia.com/v1/models"

    @pytest.mark.asyncio
    async def test_use_route_invalid_host(self):
        """Invalid /use/ targets return 400."""
        from httpx import AsyncClient, ASGITransport

        app = create_app(model_url="", model_name=None, api_key="", trick_paths=[])

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            response = await ac.get("/use/ht%20tp.com/v1/models")
            assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_stream_chat_completions_returns_valid_chunked_stream(self):
        """stream:true returns a spec-shaped chunked SSE stream when a trick
        holds the reply (so the upstream call is buffered), preserving
        reasoning_content."""
        from httpx import AsyncClient, ASGITransport

        app = create_app(model_url="", model_name=None, api_key="", trick_paths=[])

        long_content = "Hello! " * 20
        mock_response = create_mock_response({
            "id": "chatcmpl-test",
            "model": "test-model",
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": long_content,
                    "reasoning_content": "Thinking step by step.",
                },
                "finish_reason": "stop",
            }],
        })
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client), \
                patch("petsitter.proxy.channel_window", return_value=(-1, [], [])):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                async with ac.stream(
                    "POST",
                    "/use/build.nvidia.com/v1/chat/completions",
                    json={"messages": [{"role": "user", "content": "Hi"}], "stream": True},
                ) as response:
                    assert response.status_code == 200
                    assert response.headers["content-type"].startswith("text/event-stream")
                    body = b"".join([chunk async for chunk in response.aiter_bytes()]).decode()

        events = [line for line in body.splitlines() if line.startswith("data: ")]
        assert events[-1] == "data: [DONE]"
        chunks = [json.loads(ev[len("data: "):]) for ev in events[:-1]]
        assert len(chunks) >= 5
        for c in chunks:
            assert c["object"] == "chat.completion.chunk"
            assert c["choices"][0]["index"] == 0
        assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
        assert chunks[-1]["choices"][0]["delta"] == {}
        assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
        content_parts = [
            c["choices"][0]["delta"].get("content")
            for c in chunks
            if "content" in c["choices"][0]["delta"]
        ]
        assert "".join(content_parts) == long_content
        reasoning_parts = [
            c["choices"][0]["delta"].get("reasoning_content")
            for c in chunks
            if "reasoning_content" in c["choices"][0]["delta"]
        ]
        assert "".join(reasoning_parts) == "Thinking step by step."

    @pytest.mark.asyncio
    async def test_use_route_config_magic_streams_diag(self):
        """__petsitter_config__ through /use/ returns a streamed diag without hitting upstream."""
        from httpx import AsyncClient, ASGITransport

        app = create_app(model_url="", model_name=None, api_key="", trick_paths=[])

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=create_mock_response({}))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                async with ac.stream(
                    "POST",
                    "/use/build.nvidia.com/v1/chat/completions",
                    json={
                        "messages": [{"role": "user", "content": "__petsitter_config__"}],
                        "model": "nvidia-model",
                        "stream": True,
                    },
                    headers={"Authorization": "Bearer sk-nvidia"},
                ) as response:
                    assert response.status_code == 200
                    body = b"".join([chunk async for chunk in response.aiter_bytes()]).decode()

        assert mock_client.post.call_count == 0
        events = [line for line in body.splitlines() if line.startswith("data: ")]
        assert events[-1] == "data: [DONE]"
        chunks = [json.loads(ev[len("data: "):]) for ev in events[:-1]]
        content = "".join(
            c["choices"][0]["delta"].get("content", "")
            for c in chunks
            if "content" in c["choices"][0]["delta"]
        )
        diag = json.loads(content)
        assert diag["petsitter_config_diag"] is True
        assert diag["request"]["upstream"]["url"] == "https://build.nvidia.com/v1/chat/completions"
        assert diag["request"]["upstream"]["auth"] == "bearer"

    @pytest.mark.asyncio
    async def test_use_route_generic_passthrough(self):
        """Unknown paths under /use/ are forwarded transparently."""
        from httpx import AsyncClient, ASGITransport

        app = create_app(model_url="", model_name=None, api_key="", trick_paths=[])

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}
        mock_response.content = b'{"ok": true}'

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                response = await ac.post(
                    "/use/build.nvidia.com/v1/embeddings",
                    json={"input": "hello"},
                )
                assert response.status_code == 200
                assert response.json() == {"ok": True}
                assert mock_client.request.call_args.args[0] == "POST"
                assert mock_client.request.call_args.args[1] == "https://build.nvidia.com/v1/embeddings"


def test_bare_provider_domains_resolve_to_their_api_host():
    from petsitter.providers import api_host_for
    assert api_host_for("openai.com") == "api.openai.com"
    assert api_host_for("anthropic.com") == "api.anthropic.com"
    assert api_host_for("api.openai.com") == "api.openai.com"       # already an API host
    assert api_host_for("build.nvidia.com") == "build.nvidia.com"   # not in the catalog: as typed
    assert api_host_for("localhost:11434") == "localhost:11434"


async def test_use_anthropic_runs_messages_through_the_pipeline(monkeypatch):
    """Claude Code with ANTHROPIC_BASE_URL=.../use/anthropic.com: the extensions
    run, the request goes to api.anthropic.com, with the caller's own key."""
    from httpx import ASGITransport, AsyncClient
    from petsitter.proxy import ProxyHandler

    seen = {}

    async def fake_messages(self, payload, x_title="", forward_headers=None, upstream_request_url=""):
        seen.update(url=upstream_request_url, key=(forward_headers or {}).get("x-api-key"), model=payload["model"])
        return {"id": "m", "type": "message", "role": "assistant", "model": payload["model"],
                "content": [{"type": "text", "text": "hi"}], "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1}}
    monkeypatch.setattr(ProxyHandler, "messages", fake_messages)

    app = create_app(model_url="", model_name=None, api_key="", trick_paths=[])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post("/use/anthropic.com/v1/messages",
                          headers={"x-api-key": "sk-ant-test", "anthropic-version": "2023-06-01"},
                          json={"model": "claude-opus-5-5", "max_tokens": 10,
                                "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200 and r.json()["content"][0]["text"] == "hi"
    assert seen == {"url": "https://api.anthropic.com/v1/messages", "key": "sk-ant-test",
                    "model": "claude-opus-5-5"}


def test_messages_pipeline_sends_to_the_given_target(monkeypatch):
    import asyncio
    import httpx
    import petsitter.proxy as proxy_mod
    from petsitter.proxy import ProxyHandler

    hits = []

    def handle(request):
        hits.append((str(request.url), request.headers.get("x-api-key")))
        return httpx.Response(200, json={"id": "m", "type": "message", "role": "assistant", "model": "c",
                                         "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
                                         "usage": {"input_tokens": 1, "output_tokens": 1}})
    real = httpx.AsyncClient
    monkeypatch.setattr(proxy_mod.httpx, "AsyncClient",
                        lambda *a, **kw: real(*a, transport=httpx.MockTransport(handle), **kw))
    handler = ProxyHandler(model_url="http://unused", model_name="m")
    asyncio.run(handler.messages({"model": "c", "max_tokens": 5, "messages": [{"role": "user", "content": "x"}]},
                                 forward_headers={"x-api-key": "sk-1"},
                                 upstream_request_url="https://api.example.com/v1/messages"))
    assert hits == [("https://api.example.com/v1/messages", "sk-1")]

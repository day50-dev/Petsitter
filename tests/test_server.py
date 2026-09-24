"""Tests for petsitter server and CLI."""

import json

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from petsitter.server import create_app, cli
from petsitter.trick import Trick
from petsitter.trickset import Trickset


def create_mock_response(data: dict) -> MagicMock:
    """Create a mock httpx Response."""
    mock = MagicMock()
    mock.json.return_value = data
    mock.status_code = 200
    return mock


class TestCreateApp:
    """Tests for create_app function."""

    def test_create_app_basic(self):
        """create_app creates a Starlette app."""
        app = create_app(
            model_url="http://localhost:11434",
            model_name="test-model",
            api_key="",
            trick_paths=[],
        )
        assert app is not None

    def test_create_app_with_tricks(self):
        """create_app loads tricks from paths."""
        app = create_app(
            model_url="http://localhost:11434",
            model_name="test-model",
            api_key="",
            trick_paths=["tricks/json_mode.py"],
        )
        assert app is not None


class TestCreateAppDefaults:
    """Tests for _default trickset seeding and persistence."""

    @staticmethod
    def _write_saved_default(tmp_path, tricks, logfile="~/.cache/petsitter/tricksets/_default.log"):
        (tmp_path / "_default.json").write_text(json.dumps({
            "schema": "0.8.0",
            "name": "_default",
            "filters": {"X-Title": "*", "Model": "*"},
            "tricks": tricks,
            "parameters": {},
            "models": {},
            "logfile": logfile,
            "loglevel": "INFO",
        }) + "\n")

    @staticmethod
    async def _trick_files(app, name="_default"):
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            return (await ac.get(f"/api/tricksets/{name}")).json()["tricks"]

    @pytest.mark.asyncio
    async def test_fresh_default_seeds_conversational_and_secrets(self, monkeypatch, tmp_path):
        """A brand-new _default starts with conversational_tool + secrets_protector."""
        monkeypatch.setattr("petsitter.server.TRICKSETS_DIR", tmp_path)
        app = create_app(model_url="", model_name=None, api_key="", trick_paths=[])
        tricks = await self._trick_files(app)
        files = [t["file"] for t in tricks]
        assert files == ["tricks/conversational_tool.py", "tricks/secrets_protector.py"]
        assert all(t["enabled"] for t in tricks)

    @pytest.mark.asyncio
    async def test_restore_saved_default_preserves_edits(self, monkeypatch, tmp_path):
        """A saved _default.json is restored, not re-seeded."""
        monkeypatch.setattr("petsitter.server.TRICKSETS_DIR", tmp_path)
        self._write_saved_default(tmp_path, [
            {"id": "abc123", "file": "tricks/json_mode.py", "enabled": False, "keyword": None},
        ])
        app = create_app(model_url="", model_name=None, api_key="", trick_paths=[], restore_saved=True)
        tricks = await self._trick_files(app)
        assert [t["file"] for t in tricks] == ["tricks/json_mode.py"]
        assert tricks[0]["enabled"] is False

    @pytest.mark.asyncio
    async def test_no_restore_without_flag(self, monkeypatch, tmp_path):
        """Without restore_saved, a saved file is ignored."""
        monkeypatch.setattr("petsitter.server.TRICKSETS_DIR", tmp_path)
        self._write_saved_default(tmp_path, [
            {"id": "abc123", "file": "tricks/json_mode.py", "enabled": False, "keyword": None},
        ])
        app = create_app(model_url="", model_name=None, api_key="", trick_paths=[])
        tricks = await self._trick_files(app)
        assert tricks[0]["file"] == "tricks/conversational_tool.py"

    @pytest.mark.asyncio
    async def test_restore_plus_trick_paths_adds_missing_only(self, monkeypatch, tmp_path):
        """CLI -t tricks are added to a restored _default without duplicating."""
        monkeypatch.setattr("petsitter.server.TRICKSETS_DIR", tmp_path)
        self._write_saved_default(tmp_path, [
            {"id": "abc123", "file": "tricks/conversational_tool.py", "enabled": True, "keyword": None},
        ])
        app = create_app(
            model_url="", model_name=None, api_key="",
            trick_paths=["tricks/conversational_tool.py", "tricks/json_mode.py"],
            restore_saved=True,
        )
        tricks = await self._trick_files(app)
        files = [t["file"] for t in tricks]
        assert files.count("tricks/conversational_tool.py") == 1
        assert files == ["tricks/conversational_tool.py", "tricks/json_mode.py"]

    @pytest.mark.asyncio
    async def test_missing_saved_file_seeds_defaults(self, monkeypatch, tmp_path):
        """No saved file + restore_saved falls back to the default seed."""
        monkeypatch.setattr("petsitter.server.TRICKSETS_DIR", tmp_path)
        app = create_app(model_url="", model_name=None, api_key="", trick_paths=[], restore_saved=True)
        tricks = await self._trick_files(app)
        assert tricks[0]["file"] == "tricks/conversational_tool.py"


class TestServerEndpoints:
    """Tests for server endpoints."""

    @pytest.mark.asyncio
    async def test_health_endpoint(self):
        """Health endpoint returns OK."""
        from httpx import AsyncClient, ASGITransport

        app = create_app(
            model_url="http://localhost:11434",
            model_name="test-model",
            api_key="",
            trick_paths=[],
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            response = await ac.get("/health")
            assert response.status_code == 200
            assert response.json() == {"status": "ok", "paused": False}

    @pytest.mark.asyncio
    async def test_pause_endpoint_toggles_and_takes_effect_immediately(self):
        """/api/pause is the kill switch for a client whose env is already baked in.

        Unregistering only edits settings.json, which a live client process
        never re-reads, so this flag -- flipped over HTTP and checked on
        every request -- is what actually stops such a client's traffic from
        being touched, without killing the shared server.
        """
        from httpx import AsyncClient, ASGITransport

        app = create_app(
            model_url="http://localhost:11434",
            model_name="test-model",
            api_key="",
            trick_paths=[],
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            assert (await ac.get("/api/pause")).json() == {"paused": False}

            response = await ac.post("/api/pause", json={"paused": True})
            assert response.status_code == 200
            assert response.json() == {"paused": True}
            assert (await ac.get("/health")).json()["paused"] is True

            response = await ac.post("/api/pause", json={"paused": False})
            assert response.json() == {"paused": False}
            assert (await ac.get("/health")).json()["paused"] is False

    @pytest.mark.asyncio
    async def test_paused_v1_messages_is_a_raw_passthrough(self):
        """Paused must bypass to_openai_messages/to_anthropic_payload/stream_events
        entirely, not just skip tricks -- that translation round trip is what
        was silently dropping thinking blocks (and everything else outside
        text/tool_use/tool_result) even while "off", which is how a paused
        petsitter still corrupted extended-thinking turns.
        """
        from httpx import AsyncClient, ASGITransport

        app = create_app(model_url="http://localhost:11434", model_name="m", api_key="", trick_paths=[])

        captured = {}

        def make(*a, **kw):
            client = AsyncMock()
            async def request(method, url, content=None, headers=None, timeout=None):
                captured["method"] = method
                captured["url"] = url
                captured["content"] = content
                captured["headers"] = headers
                resp = MagicMock()
                resp.status_code = 200
                resp.content = b'{"id": "msg_1", "type": "message", "content": []}'
                resp.headers = {"content-type": "application/json"}
                return resp
            client.request = request
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=None)
            return client

        # A thinking block only survives a raw byte passthrough -- the
        # translate/rebuild path has no field for it and would silently drop
        # it even with no tricks loaded.
        body = {
            "model": "claude-sonnet-4",
            "max_tokens": 1024,
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": [
                    {"type": "thinking", "thinking": "let me see", "signature": "sig123"},
                    {"type": "tool_use", "id": "tu_1", "name": "look", "input": {}},
                ]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "tu_1", "content": "ok"}]},
            ],
        }

        with patch("httpx.AsyncClient", make):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                await ac.post("/api/pause", json={"paused": True})
                response = await ac.post(
                    "/v1/messages", json=body,
                    headers={"x-api-key": "sk-ant-caller", "anthropic-version": "2023-06-01"},
                )

        assert response.status_code == 200
        assert captured["url"] == "https://api.anthropic.com/v1/messages"
        sent = json.loads(captured["content"])
        assert sent == body, "paused must forward the exact request body, not a rebuilt one"
        assert captured["headers"]["x-api-key"] == "sk-ant-caller"

    @pytest.mark.asyncio
    async def test_paused_chat_completions_is_a_raw_passthrough(self):
        """Same guarantee on the OpenAI-shaped surface: build_upstream_payload
        only carries a handful of fields through, so even a paused request
        going through it would silently lose everything else.
        """
        from httpx import AsyncClient, ASGITransport

        app = create_app(model_url="http://localhost:11434", model_name="m", api_key="sk-configured", trick_paths=[])

        captured = {}

        def make(*a, **kw):
            client = AsyncMock()
            async def request(method, url, content=None, headers=None, timeout=None):
                captured["url"] = url
                captured["content"] = content
                captured["headers"] = headers
                resp = MagicMock()
                resp.status_code = 200
                resp.content = b'{"choices": [{"message": {"role": "assistant", "content": "hi"}}]}'
                resp.headers = {"content-type": "application/json"}
                return resp
            client.request = request
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=None)
            return client

        body = {"messages": [{"role": "user", "content": "hi"}], "top_p": 0.4, "seed": 7}

        with patch("httpx.AsyncClient", make):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                await ac.post("/api/pause", json={"paused": True})
                response = await ac.post("/v1/chat/completions", json=body)

        assert response.status_code == 200
        assert captured["url"] == "http://localhost:11434/v1/chat/completions"
        sent = json.loads(captured["content"])
        assert sent == body, "fields build_upstream_payload would drop must still reach upstream while paused"
        assert captured["headers"]["authorization"] == "Bearer sk-configured", \
            "petsitter's own configured key must still be attached when the client sent none"

    @pytest.mark.asyncio
    async def test_loaded_trick_persists_across_restart(self, monkeypatch, tmp_path):
        """A trick loaded via the dashboard survives a restart."""
        from httpx import AsyncClient, ASGITransport

        monkeypatch.setattr("petsitter.server.TRICKSETS_DIR", tmp_path)
        app = create_app(model_url="", model_name=None, api_key="", trick_paths=[], restore_saved=True)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.post("/api/tricks/load", json={"path": "tricks/mcp_tools.py"})
            assert r.status_code == 200
            names = [t["name"] for t in (await ac.get("/api/tricks")).json()]
            assert "McpToolsTrick" in names
            # the trickset file was written, not config.json
            assert (tmp_path / "_default.json").exists()

        app2 = create_app(model_url="", model_name=None, api_key="", trick_paths=[], restore_saved=True)
        async with AsyncClient(transport=ASGITransport(app=app2), base_url="http://test") as ac:
            names = [t["name"] for t in (await ac.get("/api/tricks")).json()]
            assert "McpToolsTrick" in names

    @pytest.mark.asyncio
    async def test_unloaded_trick_persists_across_restart(self, monkeypatch, tmp_path):
        """A trick unloaded via the dashboard stays gone after a restart."""
        from httpx import AsyncClient, ASGITransport

        monkeypatch.setattr("petsitter.server.TRICKSETS_DIR", tmp_path)
        app = create_app(model_url="", model_name=None, api_key="", trick_paths=[], restore_saved=True)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.post("/api/tricks/load", json={"path": "tricks/json_mode.py"})
            assert r.status_code == 200
            loaded = (await ac.get("/api/tricks")).json()
            jmt = next(t for t in loaded if t["name"] == "JsonModeTrick")
            r = await ac.post("/api/tricks/unload", json={"id": jmt["id"]})
            assert r.status_code == 200

        app2 = create_app(model_url="", model_name=None, api_key="", trick_paths=[], restore_saved=True)
        async with AsyncClient(transport=ASGITransport(app=app2), base_url="http://test") as ac:
            names = [t["name"] for t in (await ac.get("/api/tricks")).json()]
            assert "JsonModeTrick" not in names


    @pytest.mark.asyncio
    async def test_chat_completions_endpoint(self):
        """Chat completions endpoint proxies requests."""
        from httpx import AsyncClient, ASGITransport

        app = create_app(
            model_url="http://localhost:11434",
            model_name="test-model",
            api_key="",
            trick_paths=[],
        )

        mock_response = create_mock_response({
            "choices": [{"message": {"role": "assistant", "content": "Hello!"}}]
        })

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                response = await ac.post(
                    "/v1/chat/completions",
                    json={"messages": [{"role": "user", "content": "Hi"}]},
                )
                assert response.status_code == 200
                assert response.json()["choices"][0]["message"]["content"] == "Hello!"

    @pytest.mark.asyncio
    async def test_chat_completions_error_handling(self):
        """Chat completions handles errors gracefully."""
        from httpx import AsyncClient, ASGITransport

        app = create_app(
            model_url="http://localhost:11434",
            model_name="test-model",
            api_key="",
            trick_paths=[],
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # Invalid JSON should return error response
            response = await ac.post(
                "/v1/chat/completions",
                content="not valid json",
            )
            assert response.status_code == 400
            assert "error" in response.json()

    @pytest.mark.asyncio
    async def test_models_endpoint(self):
        """Models endpoint proxies requests."""
        from httpx import AsyncClient, ASGITransport

        app = create_app(
            model_url="http://localhost:11434",
            model_name="test-model",
            api_key="",
            trick_paths=[],
        )

        mock_response = create_mock_response({"data": [{"id": "test-model"}]})

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                response = await ac.get("/v1/models")
                assert response.status_code == 200
                assert "data" in response.json()


class TestReadConfig:
    """Tests for the /readconfig endpoint and Trickset.reread_config."""

    @staticmethod
    def _write_trickset(path, name, filters, tricks=None, loglevel="INFO"):
        path.write_text(json.dumps({
            "schema": "0.8.0",
            "name": name,
            "filters": filters,
            "tricks": tricks or [],
            "parameters": {},
            "models": {},
            "logfile": "",
            "loglevel": loglevel,
        }) + "\n")

    @staticmethod
    def _monkeypatch_paths(monkeypatch, tmp_path):
        from petsitter import trick as trick_mod
        trick_mod._modelset.clear()
        monkeypatch.setattr("petsitter.server.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("petsitter.server.CONFIG_PATH", tmp_path / "config.json")
        monkeypatch.setattr("petsitter.server.TRICKSETS_DIR", tmp_path / "tricksets")
        (tmp_path / "tricksets").mkdir(parents=True, exist_ok=True)

    @pytest.mark.asyncio
    async def test_reread_config_preserves_loaded_trick_objects(self, tmp_path):
        """reread_config applies config in place without re-instantiating tricks."""
        p = tmp_path / "opencode.json"
        self._write_trickset(p, "opencode", {"X-Title": "opencode", "Model": "*"},
                             [{"id": "abc", "file": "tricks/json_mode.py", "enabled": True, "keyword": None}])
        ts = Trickset.load_from_file(str(p))
        orig = ts.tricks[0]
        orig.max_attempts = 9

        self._write_trickset(p, "opencode", {"X-Title": "opencode2", "Model": "foo"},
                             [{"id": "abc", "file": "tricks/json_mode.py", "enabled": False,
                               "keyword": "om", "config": {"max_attempts": 5}}],
                             loglevel="DEBUG")
        res = ts.reread_config()

        assert res["action"] == "reloaded"
        assert ts.filters == {"X-Title": "opencode2", "Model": "foo"}
        assert ts.loglevel == "DEBUG"
        assert ts.tricks[0] is orig
        assert ts.tricks[0].max_attempts == 5
        assert ts.trick_enabled == [False]
        assert ts.trick_keywords == ["om"]

    @pytest.mark.asyncio
    async def test_reread_config_keeps_default_without_file(self, tmp_path):
        """An in-memory _default with no file on disk is kept, not removed."""
        ts = Trickset("_default", "0.8.0", {"X-Title": "*", "Model": "*"}, [],
                      file_path=str(tmp_path / "tricksets" / "_default.json"))
        assert ts.reread_config()["action"] == "kept"

    @pytest.mark.asyncio
    async def test_readconfig_reroutes_model(self, monkeypatch, tmp_path):
        """Changing model routing in config.json takes effect after /readconfig."""
        self._monkeypatch_paths(monkeypatch, tmp_path)
        app = create_app(model_url="http://old:11434", model_name="old-model", api_key="", trick_paths=[])
        (tmp_path / "config.json").write_text(json.dumps({
            "model_url": "http://new:11434",
            "model_name": "new-model",
            "modelset": {"default": {"url": "http://new:11434", "model": "new-model", "key": ""}},
        }) + "\n")

        from httpx import AsyncClient, ASGITransport
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.post("/readconfig")
            assert r.status_code == 200
            assert r.json()["models"]["model_url"] == "http://new:11434"

        mock_response = create_mock_response({"choices": [{"message": {"role": "assistant", "content": "Hi"}}]})
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        with patch("httpx.AsyncClient", return_value=mock_client):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                await ac.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "Hi"}]})
        assert mock_client.post.call_args[0][0] == "http://new:11434/v1/chat/completions"

    @pytest.mark.asyncio
    async def test_readconfig_reloads_filters_and_adds_new_trickset(self, monkeypatch, tmp_path):
        """Filter edits are re-applied and new on-disk tricksets get loaded."""
        self._monkeypatch_paths(monkeypatch, tmp_path)
        ts_path = tmp_path / "tricksets" / "opencode.json"
        self._write_trickset(ts_path, "opencode", {"X-Title": "opencode", "Model": "*"},
                             [{"id": "abc", "file": "tricks/json_mode.py", "enabled": True, "keyword": None}])
        app = create_app(model_url="", model_name=None, api_key="", trick_paths=[],
                         trickset_paths=[str(ts_path)])

        self._write_trickset(ts_path, "opencode", {"X-Title": "opencode2", "Model": "opencode2"},
                             [{"id": "abc", "file": "tricks/json_mode.py", "enabled": True, "keyword": None}])
        self._write_trickset(tmp_path / "tricksets" / "gemma4.json", "gemma4",
                             {"X-Title": "gemma4", "Model": "*"})

        from httpx import AsyncClient, ASGITransport
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.post("/readconfig")
            assert r.status_code == 200
            body = r.json()["tricksets"]
            assert "opencode" in body["reloaded"]
            assert "gemma4" in body["added"]

            names = [t["name"] for t in (await ac.get("/api/tricksets")).json()]
            assert "opencode" in names and "gemma4" in names
            opencode = await ac.get("/api/tricksets/opencode")
            assert opencode.json()["filters"] == {"X-Title": "opencode2", "Model": "opencode2"}

    @pytest.mark.asyncio
    async def test_readconfig_unloads_deleted_trickset(self, monkeypatch, tmp_path):
        """A trickset whose file was deleted is unloaded after /readconfig."""
        self._monkeypatch_paths(monkeypatch, tmp_path)
        ts_path = tmp_path / "tricksets" / "opencode.json"
        self._write_trickset(ts_path, "opencode", {"X-Title": "opencode", "Model": "*"},
                             [{"id": "abc", "file": "tricks/json_mode.py", "enabled": True, "keyword": None}])
        app = create_app(model_url="", model_name=None, api_key="", trick_paths=[],
                         trickset_paths=[str(ts_path)])

        ts_path.unlink()

        from httpx import AsyncClient, ASGITransport
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.post("/readconfig")
            assert r.status_code == 200
            assert "opencode" in r.json()["tricksets"]["removed"]
            names = [t["name"] for t in (await ac.get("/api/tricksets")).json()]
            assert "opencode" not in names


class TestInlineTricksets:
    """config.json 'tricksets' entries can be inline objects, not just file paths."""

    INLINE_TS = {
        "schema": "0.8.0",
        "name": "inline_ts",
        "filters": {"X-Title": "inline", "Model": "*"},
        "tricks": [{"id": "abc", "file": "tricks/json_mode.py", "enabled": True, "keyword": None}],
    }

    def test_from_dict_builds_trickset(self):
        ts = Trickset.from_dict(dict(self.INLINE_TS))
        assert ts.name == "inline_ts"
        assert ts.filters == {"X-Title": "inline", "Model": "*"}
        assert ts.file_path is None
        assert len(ts.tricks) == 1
        assert ts.tricks[0].__class__.__name__ == "JsonModeTrick"

    def test_from_dict_requires_name(self):
        with pytest.raises(ValueError):
            Trickset.from_dict({"filters": {"X-Title": "*", "Model": "*"}, "tricks": []})

    def test_reread_inline_data_in_place(self):
        ts = Trickset.from_dict(dict(self.INLINE_TS))
        orig = ts.tricks[0]
        res = ts.reread_config(data={
            "filters": {"X-Title": "inline2", "Model": "*"},
            "tricks": [{"id": "abc", "file": "tricks/json_mode.py", "enabled": False, "keyword": "om"}],
        })
        assert res["action"] == "reloaded"
        assert ts.filters == {"X-Title": "inline2", "Model": "*"}
        assert ts.tricks[0] is orig
        assert ts.trick_enabled == [False]
        assert ts.trick_keywords == ["om"]

    @pytest.mark.asyncio
    async def test_create_app_loads_inline_trickset(self, monkeypatch, tmp_path):
        monkeypatch.setattr("petsitter.server.TRICKSETS_DIR", tmp_path / "tricksets")
        app = create_app(model_url="", model_name=None, api_key="", trick_paths=[],
                         trickset_paths=[dict(self.INLINE_TS)])
        from httpx import AsyncClient, ASGITransport
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            body = (await ac.get("/api/tricksets/inline_ts")).json()
        assert body["filters"] == {"X-Title": "inline", "Model": "*"}
        assert body["tricks"][0]["file"] == "tricks/json_mode.py"

    @pytest.mark.asyncio
    async def test_readconfig_loads_inline_trickset(self, monkeypatch, tmp_path):
        """An inline trickset added to config.json appears after /readconfig."""
        TestReadConfig._monkeypatch_paths(monkeypatch, tmp_path)
        app = create_app(model_url="", model_name=None, api_key="", trick_paths=[],
                         trickset_paths=[])
        (tmp_path / "config.json").write_text(json.dumps({"tricksets": [dict(self.INLINE_TS)]}) + "\n")

        from httpx import AsyncClient, ASGITransport
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.post("/readconfig")
            assert r.status_code == 200
            assert "inline_ts" in r.json()["tricksets"]["added"]
            body = (await ac.get("/api/tricksets/inline_ts")).json()
            assert body["tricks"][0]["file"] == "tricks/json_mode.py"


class TestCLI:
    """Tests for the petsitter CLI (config flag + listen parsing)."""

    @pytest.fixture(autouse=True)
    def _restore_paths(self):
        """cli() reassigns server path globals; restore them after each test."""
        from petsitter import server

        orig = (server.CONFIG_DIR, server.CONFIG_PATH, server.TRICKSETS_DIR, server.BACKUPS_DIR)
        yield
        server.CONFIG_DIR, server.CONFIG_PATH, server.TRICKSETS_DIR, server.BACKUPS_DIR = orig

    def _invoke(self, *args):
        from click.testing import CliRunner

        runner = CliRunner()
        # The CLI builds a uvicorn.Server and runs it, so that is the seam to
        # intercept; patching uvicorn.run would let a real server start.
        with patch("petsitter.server.uvicorn.Server") as mock_server, \
             patch("petsitter.server.uvicorn.Config") as mock_config, \
             patch("petsitter.server.create_app") as mock_create:
            mock_create.return_value = None
            mock_server.return_value.started = True
            result = runner.invoke(cli, list(args))
        return result, mock_config, mock_create

    def test_cli_parse_host_port(self, tmp_path):
        """-c points at a config dir and -l parses host:port."""
        result, mock_run, _ = self._invoke(
            "-c", str(tmp_path / "configdir"),
            "-l", "0.0.0.0:9000",
        )
        assert result.exit_code == 0
        call_args = mock_run.call_args
        assert call_args[1]["host"] == "0.0.0.0"
        assert call_args[1]["port"] == 9000

    def test_cli_default_port(self, tmp_path):
        """CLI uses default port 8080 if not specified."""
        result, mock_run, _ = self._invoke(
            "-c", str(tmp_path / "configdir"),
            "-l", "localhost",
        )
        assert result.exit_code == 0
        call_args = mock_run.call_args
        assert call_args[1]["port"] == 8080

    def test_cli_config_directory_sets_paths(self, tmp_path):
        """A directory arg makes CONFIG_PATH <dir>/config.json and config_path=<dir>/config.json."""
        cfg_dir = tmp_path / "custom"
        result, _, mock_create = self._invoke("-c", str(cfg_dir))
        assert result.exit_code == 0
        from petsitter import server
        assert server.CONFIG_PATH == cfg_dir / "config.json"
        assert server.TRICKSETS_DIR == cfg_dir / "tricksets"
        call_args = mock_create.call_args
        assert call_args[1]["config_path"] == str(cfg_dir / "config.json")

    def test_cli_config_file_sets_paths(self, tmp_path):
        """A file arg is used as-is for CONFIG_PATH; base dir is its parent."""
        cfg_file = tmp_path / "another_petsitter_config.conf.json"
        cfg_file.write_text(json.dumps({
            "model_url": "http://localhost:11434",
            "model_name": "llama3:8b",
            "tricksets": ["custom/trickset.json"],
        }) + "\n")
        result, _, mock_create = self._invoke("-c", str(cfg_file))
        assert result.exit_code == 0
        from petsitter import server
        assert server.CONFIG_PATH == cfg_file
        assert server.CONFIG_DIR == tmp_path
        call_args = mock_create.call_args
        assert call_args[1]["config_path"] == str(cfg_file)
        assert call_args[0][0] == "http://localhost:11434"
        assert call_args[0][1] == "llama3:8b"
        assert call_args[1]["trickset_paths"] == ["custom/trickset.json"]

    def test_cli_saves_config_back(self, tmp_path):
        """cli persists resolved settings back to the chosen config file."""
        cfg_file = tmp_path / "config.json"
        self._invoke("-c", str(cfg_file))
        saved = json.loads(cfg_file.read_text())
        assert saved["model_url"] == ""
        assert saved["modelset"] == {}
        assert saved["tricksets"] == []

    def test_cli_inline_trickset_passes_through(self, tmp_path):
        """An inline trickset object in the config file reaches create_app."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({
            "tricksets": [{
                "schema": "0.8.0",
                "name": "inline_ts",
                "filters": {"X-Title": "inline", "Model": "*"},
                "tricks": ["tricks/json_mode.py"],
            }],
        }) + "\n")
        result, _, mock_create = self._invoke("-c", str(cfg_file))
        assert result.exit_code == 0
        ts_list = mock_create.call_args[1]["trickset_paths"]
        assert len(ts_list) == 1 and isinstance(ts_list[0], dict)
        assert ts_list[0]["name"] == "inline_ts"

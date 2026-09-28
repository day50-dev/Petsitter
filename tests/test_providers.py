"""Tests for the provider catalog and live model discovery."""

import pytest
from unittest.mock import patch

import httpx

from petsitter.providers import (
    GROUPS,
    PROVIDERS,
    _build_auth,
    _normalize,
    discover_models,
    discover_url_for,
    find_provider,
    provider_catalog,
)
from petsitter.server import create_app


def mock_response(status: int = 200, payload=None, body: str = "") -> httpx.Response:
    """Build a real httpx.Response so .json() and .status_code behave."""
    if payload is not None:
        return httpx.Response(status, json=payload)
    return httpx.Response(status, text=body)


class TestCatalog:
    """The catalog is endpoints only -- it must never carry model names."""

    def test_no_provider_ships_a_model_list(self):
        """A hardcoded model list would be stale within weeks."""
        for p in PROVIDERS:
            assert "models" not in p, f"{p['id']} hardcodes models"
            assert "model" not in p, f"{p['id']} hardcodes a model"

    def test_every_provider_is_complete(self):
        for p in PROVIDERS:
            assert p["id"] and p["label"] and p["group"]
            assert p["base_url"].startswith(("http://", "https://"))
            assert p["auth"] in ("bearer", "x-api-key", "query", "none")
            assert isinstance(p["routable"], bool)

    def test_provider_ids_are_unique(self):
        ids = [p["id"] for p in PROVIDERS]
        assert len(ids) == len(set(ids))

    def test_every_group_is_labelled(self):
        for p in PROVIDERS:
            assert p["group"] in GROUPS, f"{p['id']} has an unlabelled group"

    def test_routable_providers_expose_a_v1_surface(self):
        """petsitter appends /v1/chat/completions, so anything marked routable
        has to actually serve that path."""
        for p in PROVIDERS:
            if p["routable"]:
                assert p["base_url"].rstrip("/").endswith("/v1"), (
                    f"{p['id']} is marked routable but its base URL has no /v1"
                )
                assert p["auth"] != "none" or p["group"] == "local", (
                    f"{p['id']} is marked routable but is a remote endpoint with no auth"
                )

    def test_no_provider_carries_prose_or_offsite_links(self):
        """The picker shows facts, not copy, and stays on this page. Anything
        chatty belongs in the error the user actually hits."""
        for p in PROVIDERS:
            for junk in ("note", "description", "key_url", "docs", "blurb"):
                assert junk not in p, f"{p['id']} ships {junk}"
            assert len(p["label"]) <= 24, f"{p['id']} label is prose: {p['label']!r}"

    def test_local_providers_need_no_key(self):
        for p in PROVIDERS:
            if p["group"] == "local":
                assert p["auth"] == "none"
                assert not p["key_url"]

    def test_pay_providers_need_a_key_and_have_a_link(self):
        for p in PROVIDERS:
            if p["group"] == "pay":
                assert p["auth"] != "none"
                assert p["key_url"].startswith("https://")

    def test_catalog_is_json_safe_and_hides_nothing_but_secrets(self):
        import json
        rows = provider_catalog()
        assert len(rows) == len(PROVIDERS)
        json.dumps(rows)  # must not raise
        for row in rows:
            assert set(row) == set(provider_catalog()[0])
            assert "api_key" not in row

    def test_find_provider(self):
        assert find_provider("openai")["base_url"] == "https://api.openai.com/v1"
        assert find_provider("nope") is None


class TestNormalize:
    """Providers disagree about what a model list looks like."""

    def test_openai_shape(self):
        got = _normalize({"object": "list", "data": [
            {"id": "gpt-4o", "object": "model"},
            {"id": "gpt-4o-mini", "object": "model"},
        ]})
        assert [m["id"] for m in got] == ["gpt-4o", "gpt-4o-mini"]

    def test_anthropic_shape_uses_display_name(self):
        got = _normalize({"data": [{
            "id": "claude-sonnet-4-5",
            "type": "model",
            "display_name": "Claude Sonnet 4.5",
        }]})
        assert got == [{"id": "claude-sonnet-4-5", "label": "Claude Sonnet 4.5"}]

    def test_google_shape_strips_models_prefix(self):
        got = _normalize({"models": [
            {"name": "models/gemini-2.0-flash", "displayName": "Gemini 2.0 Flash"},
        ]})
        assert got == [{"id": "gemini-2.0-flash", "label": "Gemini 2.0 Flash"}]

    def test_google_shape_without_display_name(self):
        assert _normalize({"models": [{"name": "models/gemini-x"}]}) == [
            {"id": "gemini-x", "label": "gemini-x"}
        ]

    def test_bare_list_of_strings(self):
        assert _normalize(["b", "a"]) == [{"id": "a", "label": "a"}, {"id": "b", "label": "b"}]

    def test_drops_entries_with_no_identifier(self):
        got = _normalize({"data": [{"id": "ok"}, {"display_name": "nameless"}, {}]})
        assert [m["id"] for m in got] == ["ok"]

    def test_deduplicates(self):
        got = _normalize({"data": [{"id": "x"}, {"id": "x"}, {"name": "y"}]})
        assert [m["id"] for m in got] == ["x", "y"]

    def test_sorted_by_id(self):
        got = _normalize({"data": [{"id": "zeta"}, {"id": "alpha"}, {"id": "mid"}]})
        assert [m["id"] for m in got] == ["alpha", "mid", "zeta"]

    def test_unrecognised_envelope_is_empty_not_a_crash(self):
        assert _normalize({"error": "nope"}) == []
        assert _normalize(None) == []
        assert _normalize("a string") == []


class TestBuildAuth:
    def test_bearer(self):
        h, q = _build_auth("bearer", "sk-1")
        assert h == {"Authorization": "Bearer sk-1"}
        assert q == {}

    def test_anthropic_headers(self):
        h, _ = _build_auth("x-api-key", "sk-ant-1")
        assert h["x-api-key"] == "sk-ant-1"
        assert h["anthropic-version"] == "2023-06-01"
        assert "Authorization" not in h

    def test_google_query_param(self):
        h, q = _build_auth("query", "AIza1")
        assert q == {"key": "AIza1"}
        assert h == {}

    def test_no_auth(self):
        assert _build_auth("none", "sk-1") == ({}, {})

    def test_empty_key_sends_nothing(self):
        assert _build_auth("bearer", "") == ({}, {})

    def test_key_is_never_put_in_a_header_for_query_style(self):
        h, q = _build_auth("query", "AIza1")
        assert all("AIza1" not in v for v in h.values())


class TestDiscoverUrlFor:
    def test_defaults_to_base_plus_models(self):
        assert discover_url_for("https://api.openai.com/v1") == "https://api.openai.com/v1/models"

    def test_trailing_slash_is_tolerated(self):
        assert discover_url_for("http://localhost:11434/v1/") == "http://localhost:11434/v1/models"

    def test_provider_override_wins(self):
        p = find_provider("anthropic")
        assert discover_url_for("https://api.anthropic.com", p) == "https://api.anthropic.com/v1/models"


class TestDiscoverModels:
    async def test_openai_compatible(self):
        with patch("httpx.AsyncClient") as client_cls:
            client_cls.return_value.__aenter__.return_value.get.return_value = mock_response(
                200, {"data": [{"id": "m1"}, {"id": "m0"}]}
            )
            got = await discover_models("https://api.openai.com/v1", "sk-1")
        assert [m["id"] for m in got] == ["m0", "m1"]

    async def test_sends_bearer_header(self):
        with patch("httpx.AsyncClient") as client_cls:
            get = client_cls.return_value.__aenter__.return_value.get
            get.return_value = mock_response(200, {"data": [{"id": "m"}]})
            await discover_models("https://api.openai.com/v1", "sk-1")
        assert get.call_args.kwargs["headers"] == {"Authorization": "Bearer sk-1"}

    async def test_provider_supplies_its_own_auth_style(self):
        """The dashboard never sends an auth style; the catalog has to know that
        Anthropic does not take a bearer token."""
        with patch("httpx.AsyncClient") as client_cls:
            get = client_cls.return_value.__aenter__.return_value.get
            get.return_value = mock_response(200, {"data": [{"id": "c"}]})
            await discover_models("https://api.anthropic.com", "sk-ant", provider=find_provider("anthropic"))
        assert get.call_args.kwargs["headers"]["x-api-key"] == "sk-ant"
        assert get.call_args.args[0] == "https://api.anthropic.com/v1/models"

    async def test_explicit_auth_beats_the_catalog(self):
        with patch("httpx.AsyncClient") as client_cls:
            get = client_cls.return_value.__aenter__.return_value.get
            get.return_value = mock_response(200, {"data": [{"id": "c"}]})
            await discover_models("https://gateway.example/v1", "sk-1", "bearer", provider=find_provider("anthropic"))
        assert get.call_args.kwargs["headers"] == {"Authorization": "Bearer sk-1"}

    async def test_local_provider_needs_no_credentials(self):
        with patch("httpx.AsyncClient") as client_cls:
            get = client_cls.return_value.__aenter__.return_value.get
            get.return_value = mock_response(200, {"data": [{"id": "qwen"}]})
            await discover_models("http://localhost:11434/v1", "", provider=find_provider("ollama"))
        assert get.call_args.kwargs["headers"] == {}
        assert get.call_args.kwargs["params"] == {}

    async def test_rejected_key(self):
        with patch("httpx.AsyncClient") as client_cls:
            client_cls.return_value.__aenter__.return_value.get.return_value = mock_response(401)
            with pytest.raises(ValueError, match="rejected"):
                await discover_models("https://api.openai.com/v1", "bad")

    async def test_wrong_url(self):
        with patch("httpx.AsyncClient") as client_cls:
            client_cls.return_value.__aenter__.return_value.get.return_value = mock_response(404)
            with pytest.raises(ValueError, match="No model list"):
                await discover_models("https://example.com/nope", "sk-1")

    async def test_unreachable(self):
        with patch("httpx.AsyncClient") as client_cls:
            client_cls.return_value.__aenter__.return_value.get.side_effect = httpx.ConnectError("refused")
            with pytest.raises(ValueError, match="Can't reach"):
                await discover_models("http://localhost:1/v1", "")

    async def test_not_json(self):
        with patch("httpx.AsyncClient") as client_cls:
            client_cls.return_value.__aenter__.return_value.get.return_value = mock_response(200, body="<html>hi</html>")
            with pytest.raises(ValueError, match="didn't answer with JSON"):
                await discover_models("http://localhost:11434/v1", "")

    async def test_empty_list_is_an_error_not_an_empty_dropdown(self):
        with patch("httpx.AsyncClient") as client_cls:
            client_cls.return_value.__aenter__.return_value.get.return_value = mock_response(200, {"data": []})
            with pytest.raises(ValueError, match="listed no models"):
                await discover_models("http://localhost:11434/v1", "")

    async def test_rejects_a_missing_url(self):
        with pytest.raises(ValueError, match="No base URL"):
            await discover_models("")

    async def test_rejects_a_url_without_a_scheme(self):
        with pytest.raises(ValueError, match="http"):
            await discover_models("api.openai.com/v1", "sk-1")


class TestProviderRoutes:
    @staticmethod
    def _app():
        return create_app(model_url="", model_name="", api_key="", trick_paths=[])

    async def test_providers_endpoint_lists_endpoints_without_models(self):
        from httpx import ASGITransport, AsyncClient
        app = self._app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.get("/api/providers")
        assert r.status_code == 200
        body = r.json()
        assert body["groups"]["pay"] == "Pay per token"
        assert len(body["providers"]) == len(PROVIDERS)
        assert any(p["id"] == "openai" for p in body["providers"])
        for p in body["providers"]:
            assert "models" not in p
            assert "note" not in p

    async def test_discover_returns_the_providers_current_list(self):
        from httpx import ASGITransport, AsyncClient
        app = self._app()
        with patch("petsitter.gui_routes.discover_models") as disc:
            disc.return_value = [{"id": "gpt-newest", "label": "gpt-newest"}]
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                r = await ac.post("/api/models/discover", json={"provider": "openai", "api_key": "sk-1"})
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True
        assert body["models"] == [{"id": "gpt-newest", "label": "gpt-newest"}]
        assert body["count"] == 1

    async def test_discover_passes_the_key_through_but_does_not_persist_it(self):
        from httpx import ASGITransport, AsyncClient
        from petsitter.trick import _modelset
        app = self._app()
        _modelset.clear()
        with patch("petsitter.gui_routes.discover_models") as disc:
            disc.return_value = [{"id": "m", "label": "m"}]
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                await ac.post("/api/models/discover", json={"provider": "openai", "api_key": "sk-secret"})
        assert disc.call_args.args[1] == "sk-secret"
        # Discovery is a read. Saving stays an explicit, separate act, so a
        # key someone was just trying out never lands in the config file.
        assert _modelset == {}

    async def test_discover_reports_failure_without_a_stack_trace(self):
        from httpx import ASGITransport, AsyncClient
        app = self._app()
        with patch("petsitter.gui_routes.discover_models") as disc:
            disc.side_effect = ValueError("That API key was rejected.")
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                r = await ac.post("/api/models/discover", json={"provider": "openai", "api_key": "bad"})
        assert r.status_code == 502
        assert "rejected" in r.json()["error"]

    async def test_discover_never_echoes_the_key_back(self):
        from httpx import ASGITransport, AsyncClient
        app = self._app()
        with patch("petsitter.gui_routes.discover_models") as disc:
            disc.return_value = [{"id": "m", "label": "m"}]
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                r = await ac.post("/api/models/discover", json={"provider": "openai", "api_key": "sk-secret"})
        assert "sk-secret" not in r.text

    async def test_custom_endpoint_with_no_provider_still_works(self):
        from httpx import ASGITransport, AsyncClient
        app = self._app()
        with patch("petsitter.gui_routes.discover_models") as disc:
            disc.return_value = [{"id": "local-7b", "label": "local-7b"}]
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                r = await ac.post("/api/models/discover", json={"url": "http://192.168.1.5:8000/v1", "api_key": ""})
        assert r.status_code == 200
        assert disc.call_args.args[0] == "http://192.168.1.5:8000/v1"

    async def test_boolean_key_is_treated_as_passthrough_not_the_string_True(self):
        from httpx import ASGITransport, AsyncClient
        app = self._app()
        with patch("petsitter.gui_routes.discover_models") as disc:
            disc.return_value = [{"id": "m", "label": "m"}]
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                await ac.post("/api/models/discover", json={"provider": "openai", "api_key": False})
        assert disc.call_args.args[1] == ""

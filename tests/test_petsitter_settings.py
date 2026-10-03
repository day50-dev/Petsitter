"""petsitter's own settings: the provider timeout and the reserved id."""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from petsitter import raw, trick
from petsitter.server import apply_settings, create_app


@pytest.fixture
def cfg_dir(monkeypatch, tmp_path):
    monkeypatch.setattr("petsitter.server.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("petsitter.server.CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr("petsitter.server.TRICKSETS_DIR", tmp_path / "tricksets")
    (tmp_path / "config.json").write_text(json.dumps({"first_run": True}))
    yield tmp_path
    raw.set_upstream_timeout(raw.DEFAULT_TIMEOUT_MINUTES)
    trick.set_prefix(trick.DEFAULT_PREFIX)


async def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_defaults(cfg_dir):
    app = create_app(model_url="", model_name=None, api_key="", trick_paths=[])
    async with await _client(app) as ac:
        d = (await ac.get("/api/settings")).json()
    assert d["settings"] == {"upstream_timeout_minutes": 30, "reserved_id": "gRefWg2D7zO8"}
    assert raw.upstream_timeout().connect == 30 * 60


@pytest.mark.asyncio
async def test_timeout_applies_at_once_and_is_saved(cfg_dir):
    app = create_app(model_url="", model_name=None, api_key="", trick_paths=[])
    async with await _client(app) as ac:
        r = await ac.put("/api/settings", json={"upstream_timeout_minutes": 45})
    assert r.status_code == 200
    assert raw.upstream_timeout().read == 45 * 60
    assert json.loads((cfg_dir / "config.json").read_text())["settings"]["upstream_timeout_minutes"] == 45


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"upstream_timeout_minutes": 0}, {"upstream_timeout_minutes": "soon"},
                                  {"reserved_id": "short"}, {"reserved_id": "has-hyphens-in-it"}])
async def test_bad_values_are_refused(cfg_dir, body):
    app = create_app(model_url="", model_name=None, api_key="", trick_paths=[])
    async with await _client(app) as ac:
        r = await ac.put("/api/settings", json=body)
    assert r.status_code == 400
    assert "settings" not in json.loads((cfg_dir / "config.json").read_text())


@pytest.mark.asyncio
async def test_reserved_id_waits_for_a_restart(cfg_dir):
    app = create_app(model_url="", model_name=None, api_key="", trick_paths=[])
    async with await _client(app) as ac:
        d = (await ac.put("/api/settings", json={"reserved_id": "Zq7Lm2Xc9Vb4"})).json()
    assert d["settings"]["reserved_id"] == "Zq7Lm2Xc9Vb4"
    assert d["active"]["reserved_id"] == "gRefWg2D7zO8"
    assert trick.get_prefix() == "gRefWg2D7zO8"
    # what startup does
    apply_settings(startup=True)
    assert trick.get_prefix() == "Zq7Lm2Xc9Vb4"
    name = trick.reserved("sp")
    assert name.startswith("Zq7Lm2Xc9Vb4-sp-") and trick.reserved_pattern("sp").fullmatch(name)

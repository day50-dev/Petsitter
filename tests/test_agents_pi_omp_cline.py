"""pi, omp and Cline: connecting points the provider in use at petsitter, with
an X-Title so their channel can find them, and disconnecting puts the file
back exactly as it was."""

import json

import pytest
import yaml

from petsitter.agents import AgentContext, set_petsitter_url
from petsitter.agents.cline import ClineAgent
from petsitter.agents.omp import OmpAgent
from petsitter.agents.pi import PiAgent


def _ctx():
    return AgentContext(trickset_name="t", model_config={}, trick_paths=[])


def test_pi_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi"))
    set_petsitter_url("http://localhost:8099")
    d = tmp_path / "pi"
    d.mkdir()
    (d / "settings.json").write_text('{"defaultProvider": "ol", "defaultModel": "qwen3.8"}')
    original = '{\n  "providers": {"ol": {"baseUrl": "https://9ol.es/11434/v1", "api": "openai-completions",\n  "apiKey": "none", "models": [{"id": "qwen3.8"}]}}\n}\n'
    (d / "models.json").write_text(original)
    a, ctx = PiAgent(), _ctx()
    assert a.installed() and not a.is_registered()
    a.register(ctx)
    ol = json.loads((d / "models.json").read_text())["providers"]["ol"]
    assert ol["baseUrl"] == "http://localhost:8099/v1" and ol["headers"] == {"X-Title": "pi"}
    assert ol["models"] == [{"id": "qwen3.8"}]
    assert a.is_registered()
    a.unregister(ctx)
    assert (d / "models.json").read_text() == original


def test_pi_with_a_built_in_provider_gets_a_models_json(tmp_path, monkeypatch):
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi"))
    d = tmp_path / "pi"
    d.mkdir()
    (d / "settings.json").write_text('{"defaultProvider": "anthropic"}')
    a, ctx = PiAgent(), _ctx()
    a.register(ctx)
    assert set(json.loads((d / "models.json").read_text())["providers"]["anthropic"]) == {"baseUrl", "headers"}
    a.unregister(ctx)
    assert not (d / "models.json").exists()                           # it wasn't there before


def test_pi_without_a_default_provider_refuses(tmp_path, monkeypatch):
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi"))
    (tmp_path / "pi").mkdir()
    (tmp_path / "pi" / "settings.json").write_text("{}")
    with pytest.raises(RuntimeError, match="no default provider"):
        PiAgent().register(_ctx())


def test_omp_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "omp"))
    set_petsitter_url("http://localhost:8099")
    d = tmp_path / "omp"
    d.mkdir()
    (d / "config.yml").write_text("modelRoles:\n  default: ol/qwen3.8\n")
    original = "# my providers\nproviders:\n  ol:\n    baseUrl: https://9ol.es/11434/v1\n    api: openai-completions\n    apiKey: none\n    models:\n      - id: qwen3.8\n"
    (d / "models.yml").write_text(original)
    a, ctx = OmpAgent(), _ctx()
    assert a.installed() and not a.is_registered()
    a.register(ctx)
    ol = yaml.safe_load((d / "models.yml").read_text())["providers"]["ol"]
    assert ol["baseUrl"] == "http://localhost:8099/v1" and ol["headers"] == {"X-Title": "omp"}
    assert a.is_registered()
    a.unregister(ctx)
    assert (d / "models.yml").read_text() == original                   # comments too


def test_cline_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("CLINE_DIR", str(tmp_path / "cline"))
    set_petsitter_url("http://localhost:8099")
    path = tmp_path / "cline" / "data" / "settings" / "providers.json"
    path.parent.mkdir(parents=True)
    original = json.dumps({"version": 1, "lastUsedProvider": "openai-compatible", "modes": {}, "providers": {
        "openai-compatible": {"settings": {"provider": "openai-compatible", "apiKey": "none", "model": "qwen3.8",
                                           "baseUrl": "https://9ol.es/11434/v1"},
                              "updatedAt": "2026-10-03T10:03:46.699Z", "tokenSource": "manual"}}}, indent=2) + "\n"
    path.write_text(original)
    a, ctx = ClineAgent(), _ctx()
    assert a.installed() and not a.is_registered()
    a.register(ctx)
    s = json.loads(path.read_text())["providers"]["openai-compatible"]["settings"]
    assert s["baseUrl"] == "http://localhost:8099/v1" and s["headers"] == {"X-Title": "Cline"}
    assert a.is_registered()
    a.unregister(ctx)
    assert path.read_text() == original


def test_cline_without_a_provider_refuses(tmp_path, monkeypatch):
    monkeypatch.setenv("CLINE_DIR", str(tmp_path / "cline"))
    with pytest.raises(RuntimeError, match="cline auth"):
        ClineAgent().register(_ctx())


def test_channels_and_checks():
    for agent, title in ((PiAgent, "pi"), (OmpAgent, "omp"), (ClineAgent, "Cline")):
        assert agent.trickset_filters["X-Title"] == title and agent.checked

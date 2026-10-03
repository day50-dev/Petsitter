"""Hermes Agent and Kilo Code: connecting points them at petsitter, and
disconnecting puts their config back exactly as it was."""

import json

import yaml

from petsitter.agents import AgentContext, set_petsitter_url
from petsitter.agents.hermes import HermesAgent
from petsitter.agents.kilo import KiloAgent

HERMES = """# my hermes setup
model:
  default: qwen3.8          # the gateway
  provider: openrouter
  api_key: none
"""

KILO = """{
  // my kilo setup
  "model": "ol/qwen3.8",
  "provider": {
    "ol": {
      "npm": "@ai-sdk/openai-compatible",
      "options": { "baseURL": "https://9ol.es/11434/v1", },
      "models": { "qwen3.8": {} },
    },
  },
}
"""


def _ctx():
    return AgentContext(trickset_name="t", model_config={}, trick_paths=[])


def test_hermes_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    set_petsitter_url("http://localhost:8099")
    path = tmp_path / "hh" / "config.yaml"
    path.parent.mkdir()
    path.write_text(HERMES)
    a, ctx = HermesAgent(), _ctx()
    assert a.installed() and a.detect().status == "ready" and not a.is_registered()
    a.register(ctx)
    model = yaml.safe_load(path.read_text())["model"]
    assert model["provider"] == "custom" and model["base_url"] == "http://localhost:8099/v1"
    assert model["extra_headers"] == {"X-Title": "Hermes Agent"}     # so its channel can find it
    assert model["default"] == "qwen3.8"
    assert a.is_registered()
    a.unregister(ctx)
    assert path.read_text() == HERMES                                   # byte for byte, comments too
    assert not a.is_registered()


def test_hermes_created_file_is_removed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    a, ctx = HermesAgent(), _ctx()
    a.register(ctx)
    assert (tmp_path / "hh" / "config.yaml").exists()
    a.unregister(ctx)
    assert not (tmp_path / "hh" / "config.yaml").exists()


def test_kilo_jsonc_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    set_petsitter_url("http://localhost:8099")
    path = tmp_path / "cfg" / "kilo" / "kilo.jsonc"
    path.parent.mkdir(parents=True)
    path.write_text(KILO)
    a, ctx = KiloAgent(), _ctx()
    assert a.config_file() == path and a.installed() and not a.is_registered()
    a.register(ctx)
    data = json.loads(path.read_text())
    assert data["provider"]["ol"]["options"]["baseURL"] == "http://localhost:8099/v1"
    assert data["provider"]["ol"]["models"] == {"qwen3.8": {}}       # the rest survives the comments
    assert a.is_registered()
    a.unregister(ctx)
    assert path.read_text() == KILO                                     # comments and trailing commas back
    assert not a.is_registered()


def test_kilo_without_a_provider_patches_its_own_gateway(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    path = tmp_path / "cfg" / "kilo" / "kilo.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    a, ctx = KiloAgent(), _ctx()
    a.register(ctx)
    assert "kilo" in json.loads(path.read_text())["provider"]
    a.unregister(ctx)
    assert path.read_text() == "{}"


def test_channels_match_only_their_tool():
    assert HermesAgent.trickset_filters["X-Title"] == "Hermes Agent"
    assert KiloAgent.trickset_filters["X-Title"] == "Kilo Code*"


def test_an_unreadable_config_is_refused_and_left_alone(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    hermes = tmp_path / "hh" / "config.yaml"
    hermes.parent.mkdir()
    hermes.write_text("model: [unclosed\n")
    kilo = tmp_path / "cfg" / "kilo" / "kilo.json"
    kilo.parent.mkdir(parents=True)
    kilo.write_text("{ not json")
    for agent, path, text in ((HermesAgent(), hermes, "model: [unclosed\n"), (KiloAgent(), kilo, "{ not json")):
        with pytest.raises(RuntimeError, match="not changing it"):
            agent.register(_ctx())
        assert path.read_text() == text


def test_adapters_say_what_they_were_checked_with():
    assert "0.19.0" in HermesAgent.checked and "7.8.3" in KiloAgent.checked

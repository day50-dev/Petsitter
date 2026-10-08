"""OpenClaw: connecting points the provider in use at petsitter, with an
X-Title so its channel can find it, and disconnecting puts openclaw.json back
exactly as it was."""

import json

import pytest

from petsitter.agents import AgentContext, set_petsitter_url
from petsitter.agents.openclaw import OpenClawAgent


def _ctx():
    return AgentContext(trickset_name="t", model_config={}, trick_paths=[])


@pytest.fixture
def config(tmp_path, monkeypatch):
    path = tmp_path / "openclaw.json"
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(path))
    set_petsitter_url("http://localhost:8099")
    return path


def test_round_trip(config):
    original = ('{\n  "models": {"providers": {"ninebol": {"baseUrl": "https://9ol.es/11434/v1", "apiKey": "none",\n'
                '    "api": "openai-completions", "models": [{"id": "qwen3.8"}]}}},\n'
                '  "agents": {"defaults": {"model": {"primary": "ninebol/qwen3.8"}}}\n}\n')
    config.write_text(original)
    a, ctx = OpenClawAgent(), _ctx()
    assert a.installed() and not a.is_registered()
    assert "model: ninebol/qwen3.8" in a.detect().message
    a.register(ctx)
    p = json.loads(config.read_text())["models"]["providers"]["ninebol"]
    assert p["baseUrl"] == "http://localhost:8099/v1" and p["headers"] == {"X-Title": "OpenClaw"}
    assert p["models"] == [{"id": "qwen3.8"}] and p["apiKey"] == "none"
    assert a.is_registered()
    a.unregister(ctx)
    assert config.read_text() == original


def test_a_built_in_provider_gets_an_entry(config):
    config.write_text('{"agents": {"defaults": {"model": "openai/gpt-5.4"}}}')
    OpenClawAgent().register(_ctx())
    assert json.loads(config.read_text())["models"]["providers"]["openai"] == {
        "baseUrl": "http://localhost:8099/v1", "headers": {"X-Title": "OpenClaw"}}


def test_anthropic_is_rooted_above_v1(config):
    config.write_text('{"agents": {"defaults": {"model": {"primary": "anthropic/claude-opus-5-5"}}}}')
    OpenClawAgent().register(_ctx())
    assert json.loads(config.read_text())["models"]["providers"]["anthropic"]["baseUrl"] == "http://localhost:8099"


def test_json5_with_comments_is_left_alone(config):
    text = '{\n  // my setup\n  agents: { defaults: { model: { primary: "ninebol/qwen3.8" } } },\n}\n'
    config.write_text(text)
    with pytest.raises(RuntimeError, match="JSON5"):
        OpenClawAgent().register(_ctx())
    assert config.read_text() == text


def test_no_model_chosen_yet(config):
    config.write_text('{"agents": {"defaults": {}}}')
    with pytest.raises(RuntimeError, match="openclaw onboard"):
        OpenClawAgent().register(_ctx())


def test_not_installed(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(tmp_path / "nope.json"))
    assert OpenClawAgent().detect().status == "missing_creds"

"""Discovered programs: what X-Title values (and models) were seen, and where they went."""

from petsitter.discovered import DiscoveredPrograms
from petsitter.proxy import ProxyHandler
from petsitter.trickset import Trickset


def test_accumulates_and_survives_a_restart(tmp_path):
    path = tmp_path / "discovered.json"
    d = DiscoveredPrograms(path)
    d.note("OpenCode", "qwen3", ["_default"])
    d.note("OpenCode", "qwen3", ["_default"])
    d.note("", "gpt-4o", ["_default"])
    d.save()
    again = DiscoveredPrograms(path)
    snap = {p["key"]: p for p in again.snapshot()}
    assert snap["OpenCode"]["count"] == 2
    assert snap["OpenCode"]["channels"] == {"_default": 2}
    assert snap["ua:"]["models"] == {"gpt-4o": 1}       # sends nothing at all: still recorded


def test_in_memory_without_a_path(tmp_path):
    d = DiscoveredPrograms(None)
    d.note("x", "m", [])
    d.save()
    assert d.snapshot()[0]["x_title"] == "x"


def test_proxy_records_where_requests_went():
    default = Trickset("_default", "0.3.0", {"X-Title": "*", "Model": "*"}, [])
    oc = Trickset("opencode", "0.3.0", {"X-Title": "opencode*", "Model": "*"}, [])
    h = ProxyHandler("http://unused", "m", tricksets={"_default": default, "opencode": oc})
    h._matching_tricks("OpenCode", "qwen3")
    h._matching_tricks("curl", "qwen3")
    snap = {p["x_title"]: p for p in h.discovered.snapshot()}
    assert snap["OpenCode"]["channels"] == {"opencode": 1}
    assert snap["curl"]["channels"] == {"_default": 1}


def test_forget():
    d = DiscoveredPrograms(None)
    d.note("x", "m", [])
    assert d.forget("x") and d.snapshot() == []


def test_programs_without_x_title_are_told_apart_by_user_agent():
    d = DiscoveredPrograms(None)
    d.note("", "gpt-4o", ["_default"], user_agent="goose/1.4.0")
    d.note("", "gpt-4o", ["_default"], user_agent="curl/8.5.0")
    d.note("Claude Code", "claude-sonnet-5", ["_default"], user_agent="claude-cli/2.0")
    snap = {p["key"]: p for p in d.snapshot()}
    assert set(snap) == {"ua:goose", "ua:curl", "Claude Code"}
    d.note("", "gpt-4o", ["_default"], user_agent="goose/1.5.2")       # an upgrade: same program
    snap = {p["key"]: p for p in d.snapshot()}
    assert snap["ua:goose"]["user_agents"] == {"goose/1.4.0": 1, "goose/1.5.2": 1}
    assert snap["Claude Code"]["user_agents"] == {"claude-cli/2.0": 1}


def test_channel_can_match_on_user_agent():
    from petsitter.trickset import Trickset
    goose = Trickset("goose", "0.3.0", {"User-Agent": "goose*", "Model": "*"}, [])
    assert goose.matches("", "gpt-4o", "Goose/1.4.0")
    assert not goose.matches("", "gpt-4o", "curl/8.5.0")
    default = Trickset("_default", "0.3.0", {"X-Title": "*", "Model": "*"}, [])
    h = ProxyHandler("http://unused", "m", tricksets={"_default": default, "goose": goose})
    _, matched = h._matching_tricks("", "gpt-4o", "goose/1.4.0")
    assert matched.name == "goose"


def test_sample_headers_mask_credentials(tmp_path):
    path = tmp_path / "discovered.json"
    d = DiscoveredPrograms(path)
    d.note("", "gpt-4o", ["_default"], user_agent="goose/1.4.0", headers=[
        ("user-agent", "goose/1.4.0"), ("authorization", "Bearer sk-live-SECRET123"),
        ("x-api-key", "SECRET456"), ("content-type", "application/json"), ("x-goose-session", "SECRET789"),
    ])
    d.save()
    sample = DiscoveredPrograms(path).snapshot()[0]["sample_headers"]
    assert sample["user-agent"] == "goose/1.4.0" and sample["content-type"] == "application/json"
    assert sample["authorization"].startswith("Bearer ") and "SECRET" not in sample["authorization"]
    assert "SECRET" not in path.read_text()            # never on disk

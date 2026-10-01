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
    snap = {p["x_title"]: p for p in again.snapshot()}
    assert snap["OpenCode"]["count"] == 2
    assert snap["OpenCode"]["channels"] == {"_default": 2}
    assert snap[""]["models"] == {"gpt-4o": 1}          # sends no X-Title: still recorded


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

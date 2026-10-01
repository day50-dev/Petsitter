"""Channels are on by default; off is a saved, deliberate choice."""

import json

from petsitter import server
from petsitter.trickset import SCHEMA, Trickset


def _write(dirpath, name, **extra):
    data = {"schema": SCHEMA, "name": name, "filters": {"X-Title": name, "Model": "*"}, "tricks": []}
    data.update(extra)
    (dirpath / f"{name}.json").write_text(json.dumps(data))


def test_enabled_round_trips_and_defaults_on(tmp_path):
    _write(tmp_path, "plain")
    _write(tmp_path, "shut", enabled=False)
    assert Trickset.load_from_file(str(tmp_path / "plain.json")).enabled is True
    shut = Trickset.load_from_file(str(tmp_path / "shut.json"))
    assert shut.enabled is False
    shut.enabled = True
    shut.save()
    assert json.loads((tmp_path / "shut.json").read_text())["enabled"] is True


def test_startup_loads_every_channel_that_is_on(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "TRICKSETS_DIR", tmp_path)
    _write(tmp_path, "codex")
    _write(tmp_path, "gemma4", enabled=False)
    loaded = server._load_saved_tricksets(skip=set())
    assert set(loaded) == {"codex"}


def test_turning_off_in_the_file_unloads_on_reload(tmp_path):
    _write(tmp_path, "codex")
    ts = Trickset.load_from_file(str(tmp_path / "codex.json"))
    _write(tmp_path, "codex", enabled=False)
    assert ts.reread_config()["action"] == "removed"


def test_shipped_examples_arrive_off():
    for name in ("gemma4", "opencode"):
        data = json.loads((server._SOURCE_TRICKSETS / f"{name}.json").read_text())
        assert data["enabled"] is False

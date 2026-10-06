"""Shared test setup: nothing a test does may land in the real petsitter cache."""

import pytest


@pytest.fixture(autouse=True)
def _contexts_in_tmp(tmp_path, monkeypatch):
    # Context Editor saves every conversation for (context:import:<id>)
    monkeypatch.setenv("PETSITTER_CONTEXTS_DIR", str(tmp_path / "contexts"))

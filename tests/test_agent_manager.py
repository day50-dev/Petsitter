"""Regression tests for AgentManager's shutdown/unregister bookkeeping.

The bug: if a per-agent unregister() swallowed its own write failure into a
log-only WARNING and returned as if nothing went wrong, AgentManager.unregister
would still treat it as a success, remove the entry from registry.json, and
save that -- so the agent's config file stayed pointed at petsitter forever,
with no record left anywhere that it still needed fixing. Fixed by having the
per-agent unregister() raise on a genuine restore failure instead of swallowing
it, so AgentManager's existing except-branch (which already correctly skips
saving on failure) does its job.
"""

from petsitter.agent_manager import AgentManager
from petsitter.agents import Agent, AgentContext, load_registry


class _FlakyAgent(Agent):
    id = "flaky"
    display_name = "Flaky Tool"
    tricks: list[str] = []
    model_config: dict = {}

    def __init__(self):
        self.fail_unregister = False
        self.unregister_calls = 0
        # Stands in for "the real config file currently points at petsitter" --
        # a live agent would compute this by reading its own config; here it's
        # just a flag the test flips to simulate drift from registry.json.
        self.live = False

    def is_registered(self) -> bool:
        return self.live

    def register(self, ctx: AgentContext) -> list[dict[str, str]]:
        self.live = True
        return [{"level": "INFO", "message": "registered"}]

    def unregister(self, ctx: AgentContext) -> list[dict[str, str]]:
        self.unregister_calls += 1
        if self.fail_unregister:
            raise RuntimeError("could not write flaky's config file")
        self.live = False
        return [{"level": "INFO", "message": "restored"}]


def _manager(tmp_path, agent):
    mgr = AgentManager(config_dir=str(tmp_path), agents_dir=tmp_path / "empty-agents")
    mgr._agents = {agent.id: agent}
    return mgr


def test_unregister_failure_keeps_registry_entry_for_retry(tmp_path):
    agent = _FlakyAgent()
    mgr = _manager(tmp_path, agent)

    ok, _log = mgr.register("flaky")
    assert ok
    assert load_registry(str(tmp_path))["agents"]["flaky"]["status"] == "registered"

    agent.fail_unregister = True
    mgr.unregister("flaky")

    # The write never actually succeeded, so the registry must still say this
    # agent is registered -- otherwise nothing will ever retry restoring it.
    registry = load_registry(str(tmp_path))
    assert registry["agents"].get("flaky", {}).get("status") == "registered"


def test_unregister_success_clears_registry_entry(tmp_path):
    agent = _FlakyAgent()
    mgr = _manager(tmp_path, agent)

    mgr.register("flaky")
    mgr.unregister("flaky")

    registry = load_registry(str(tmp_path))
    assert "flaky" not in registry["agents"]


def test_unregister_after_failure_can_be_retried_and_succeed(tmp_path):
    agent = _FlakyAgent()
    mgr = _manager(tmp_path, agent)

    mgr.register("flaky")
    agent.fail_unregister = True
    mgr.unregister("flaky")
    assert load_registry(str(tmp_path))["agents"]["flaky"]["status"] == "registered"

    agent.fail_unregister = False
    mgr.unregister("flaky")
    assert "flaky" not in load_registry(str(tmp_path))["agents"]
    assert agent.unregister_calls == 2


# ---- get_registered() / unregister() must trust the live config check, not
# the stored registry.json flag, so status self-corrects when they disagree.


def test_get_registered_reflects_live_check_not_stored_flag(tmp_path):
    """registry.json says registered, but the config file doesn't -- report false."""
    agent = _FlakyAgent()
    mgr = _manager(tmp_path, agent)

    mgr.register("flaky")
    assert mgr.get_registered()["agents"]["flaky"]["status"] == "registered"

    # Simulate drift: something reset the tool's config (hand edit, a
    # crash-restore CLI run, whatever) without petsitter's registry knowing.
    agent.live = False

    status = mgr.get_registered()["agents"]["flaky"]["status"]
    assert status == "unregistered", (
        "get_registered() must ask the agent's live is_registered(), "
        "not just echo registry.json's stored status"
    )


def test_get_registered_reports_registered_even_if_registry_entry_missing(tmp_path):
    """Config file points at petsitter, but registry.json has no record of it."""
    agent = _FlakyAgent()
    mgr = _manager(tmp_path, agent)

    # No register() call at all -- e.g. registry.json write silently failed
    # after the config write succeeded, or the file was hand-edited.
    agent.live = True

    status = mgr.get_registered()["agents"]["flaky"]["status"]
    assert status == "registered"


def test_unregister_restores_live_registration_with_no_registry_entry(tmp_path):
    """unregister() must still act when only the live check says registered."""
    agent = _FlakyAgent()
    mgr = _manager(tmp_path, agent)

    agent.live = True  # config points at petsitter; registry.json knows nothing

    ok, log = mgr.unregister("flaky")
    assert ok
    assert agent.unregister_calls == 1
    assert agent.live is False
    assert not any("not registered" in e.get("message", "") for e in log)


def test_unregister_all_includes_live_only_registrations(tmp_path):
    agent = _FlakyAgent()
    mgr = _manager(tmp_path, agent)

    agent.live = True  # drifted: live but not in registry.json

    mgr.unregister_all()
    assert agent.unregister_calls == 1
    assert agent.live is False

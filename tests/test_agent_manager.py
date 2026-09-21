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

    def register(self, ctx: AgentContext) -> list[dict[str, str]]:
        return [{"level": "INFO", "message": "registered"}]

    def unregister(self, ctx: AgentContext) -> list[dict[str, str]]:
        self.unregister_calls += 1
        if self.fail_unregister:
            raise RuntimeError("could not write flaky's config file")
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

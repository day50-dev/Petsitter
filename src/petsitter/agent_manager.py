"""AgentManager — discover agents, register, unregister, persist state."""

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

from petsitter.agents import Agent, AgentContext, AgentResult, load_registry, save_registry

logger = logging.getLogger("petsitter")


def _discover_agents(agents_dir: str | Path) -> dict[str, Agent]:
    """Scan agents/ directory and instantiate all Agent subclasses."""
    agents: dict[str, Agent] = {}
    d = Path(agents_dir)
    if not d.exists():
        logger.warning("Agents directory not found: %s", d)
        return agents
    for f in sorted(d.glob("*.py")):
        if f.name == "__init__.py":
            continue
        try:
            module_name = f"agents_{f.stem}"
            spec = importlib.util.spec_from_file_location(module_name, str(f))
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (
                    isinstance(attr, type)
                    and issubclass(attr, Agent)
                    and attr is not Agent
                ):
                    instance = attr()
                    if instance.id:
                        agents[instance.id] = instance
        except Exception as e:
            logger.warning("Failed to load agent %s: %s", f.name, e)
    return agents


class AgentManager:
    """Manages agent registration and unregistration.

    Discovers agents from the ``agents/`` directory and persists
    registration state to a JSON file in the config directory.
    """

    def __init__(self, config_dir: str, agents_dir: str | Path | None = None, handler=None):
        self.config_dir = config_dir
        self._agents = _discover_agents(agents_dir or Path(__file__).resolve().parent / "agents")
        # Needed to make a new trickset live immediately, rather than only on
        # the next restart.
        self.handler = handler

    def _tricksets_dir(self) -> Path:
        return Path(self.config_dir) / "tricksets"

    def _create_trickset(self, agent: Agent) -> tuple[str, list[dict[str, str]]]:
        """Give the agent its own trickset so its traffic isn't in everyone's default.

        Returns (name, log). Existing tricksets are left alone: the point is to
        set one up for you, not to overwrite one you've been editing.
        """
        from petsitter.trickset import SCHEMA, Trickset

        name = agent.id
        log: list[dict[str, str]] = []
        path = self._tricksets_dir() / f"{name}.json"

        if (self.handler and name in self.handler.tricksets) or path.exists():
            log.append({"level": "INFO", "message": f"Trickset '{name}' already exists; leaving it as it is"})
            return name, log

        ts = Trickset(name, SCHEMA, dict(agent.trickset_filters), [])
        ts.file_path = str(path)
        for trick_path in agent.tricks:
            try:
                ts.add_trick(trick_path)
            except Exception as e:
                log.append({"level": "WARNING", "message": f"Could not add {trick_path}: {e}"})
        ts.save()
        if self.handler is not None:
            self.handler.tricksets[name] = ts
        filters = ", ".join(f"{k}={v}" for k, v in agent.trickset_filters.items())
        log.append({"level": "INFO", "message": f"Created trickset '{name}' ({filters})"})
        log.append({"level": "INFO",
                    "message": "Tricks: " + ", ".join(t.split("/")[-1].replace(".py", "") for t in agent.tricks)})
        return name, log

    def _remove_trickset(self, agent: Agent, expected: list[str]) -> list[dict[str, str]]:
        """Remove the trickset we made, unless it has been changed since.

        A trickset someone has edited is their work, not ours to delete.
        """
        name = agent.id
        log: list[dict[str, str]] = []
        path = self._tricksets_dir() / f"{name}.json"
        ts = (self.handler.tricksets.get(name) if self.handler else None)

        current = list(getattr(ts, "trick_paths", None) or [])
        if ts is not None and current and sorted(current) != sorted(expected):
            log.append({"level": "INFO",
                        "message": f"Kept trickset '{name}' \u2014 its tricks have changed since it was created"})
            return log
        if self.handler is not None:
            self.handler.tricksets.pop(name, None)
        if path.exists():
            try:
                path.unlink()
                log.append({"level": "INFO", "message": f"Removed trickset '{name}'"})
            except OSError:
                log.append({"level": "WARNING", "message": f"Could not remove trickset '{name}'"})
        return log

    def get_agents(self) -> dict[str, dict[str, Any]]:
        """Return all agents with their current detect status."""
        result: dict[str, dict[str, Any]] = {}
        for agent_id, agent in self._agents.items():
            try:
                detect_result = agent.detect()
            except Exception as e:
                detect_result = AgentResult(status="error", message=str(e))
            result[agent_id] = {
                "id": agent.id,
                "display_name": agent.display_name,
                "description": agent.description,
                "provider_name": getattr(agent, "provider_name", "its AI provider"),
                "icon": agent.icon,
                "config_paths": list(agent.config_paths),
                "tricks": agent.tricks,
                "model_config": agent.model_config,
                "detect": {
                    "status": detect_result.status,
                    "installed": agent.installed(),
                    "found_env": detect_result.found_env,
                    "missing_env": detect_result.missing_env,
                    "message": detect_result.message,
                },
            }
        return result

    def detect(self, agent_id: str) -> AgentResult:
        """Run detect() for a specific agent."""
        agent = self._get(agent_id)
        return agent.detect()

    def register(self, agent_id: str) -> tuple[bool, list[dict[str, str]]]:
        """Register an agent: create trickset, swap config, persist state.

        Returns (success, log_entries).
        """
        agent = self._get(agent_id)
        log: list[dict[str, str]] = []

        # Create backup context
        backup: dict[str, Any] = {"trickset_name": agent_id, "env": {}, "files": {}}
        ctx = AgentContext(
            trickset_name=agent_id,
            model_config=dict(agent.model_config),
            trick_paths=list(agent.tricks),
            backup=backup,
        )

        # Register the agent (swap config)
        try:
            agent_log = agent.register(ctx)
            log.extend(agent_log)
        except Exception as e:
            logger.exception("Agent %s register failed", agent_id)
            log.append({"level": "ERROR", "message": f"Registration failed: {e}"})
            return False, log

        try:
            ts_name, ts_log = self._create_trickset(agent)
            log.extend(ts_log)
            ctx.backup["trickset_created"] = ts_name
            ctx.backup["trickset_tricks"] = list(agent.tricks)
        except Exception as e:
            logger.exception("Agent %s trickset setup failed", agent_id)
            log.append({"level": "WARNING", "message": f"Could not set up a trickset: {e}"})

        # Persist registry
        registry = load_registry(self.config_dir)
        registry.setdefault("agents", {})[agent_id] = {
            "status": "registered",
            "backup": ctx.backup,
        }
        save_registry(self.config_dir, registry)

        log.append({"level": "INFO", "message": "Configuration saved"})
        return True, log

    def ensure_trickset(self, agent_id: str) -> tuple[str, list[dict[str, str]]]:
        """Make sure this agent has a trickset, creating it if it doesn't.

        Registering creates one, but an agent connected before that existed --
        or one whose trickset was deleted -- would otherwise have nowhere for
        "Configure" to go. Idempotent: an existing trickset is left alone.
        """
        agent = self._get(agent_id)
        name, log = self._create_trickset(agent)
        registry = load_registry(self.config_dir)
        entry = registry.get("agents", {}).get(agent_id)
        if entry is not None:
            entry.setdefault("backup", {})["trickset_created"] = name
            entry["backup"]["trickset_tricks"] = list(agent.tricks)
            save_registry(self.config_dir, registry)
        return name, log

    def _is_registered(self, agent_id: str, agent: Agent) -> bool:
        """Live feature-check: does this agent's own config point at petsitter right now?

        Never trust registry.json for this question -- it only records what
        petsitter last *did*, and can drift from what is actually on disk (a
        write that silently failed before, a config hand-edited back, a
        registry.json that never got the write). Each agent knows how to
        read its own config format; this just calls through and treats an
        error as "can't confirm it's registered," not as registered.
        """
        try:
            return agent.is_registered()
        except Exception:
            logger.exception("Agent %s is_registered() failed", agent_id)
            return False

    def live_registered_ids(self) -> list[str]:
        """Ids of every discovered agent whose config currently points at petsitter.

        Computed fresh from each agent's own config file, not from
        registry.json -- see ``_is_registered``.
        """
        return [aid for aid, agent in self._agents.items() if self._is_registered(aid, agent)]

    def unregister(self, agent_id: str) -> tuple[bool, list[dict[str, str]]]:
        """Unregister a specific agent and restore its configuration.

        Always calls through to the agent's own unregister() when there is
        anything to restore -- either its config currently points at
        petsitter, or registry.json has backup data for it -- even if only
        one of those is true. registry.json can lose track of a
        registration (a write that landed but was never recorded, a
        corrupted registry.json) while the config file itself still shows
        petsitter's address; the only case genuinely safe to skip is neither
        being true, i.e. there is nothing anywhere to suggest this agent was
        ever registered.
        """
        agent = self._get(agent_id)
        log: list[dict[str, str]] = []

        registry = load_registry(self.config_dir)
        entry = registry.get("agents", {}).pop(agent_id, None)
        if entry is None and not self._is_registered(agent_id, agent):
            log.append({"level": "WARNING", "message": f"Agent {agent_id} is not registered"})
            return True, log

        backup = (entry or {}).get("backup", {})
        ctx = AgentContext(
            trickset_name=agent_id,
            model_config=dict(agent.model_config),
            trick_paths=list(agent.tricks),
            backup=backup,
        )

        try:
            agent_log = agent.unregister(ctx)
            log.extend(agent_log)
            if backup.get("trickset_created"):
                log.extend(self._remove_trickset(agent, backup.get("trickset_tricks") or []))
        except Exception as e:
            logger.exception("Agent %s unregister failed", agent_id)
            log.append({"level": "ERROR", "message": f"Restore failed: {e}"})
            # Still remove from registry even on error
        else:
            save_registry(self.config_dir, registry)

        return True, log

    def unregister_all(self) -> list[dict[str, str]]:
        """Unregister every agent that is either registry-tracked or live-registered.

        The union, not just the registry's list: a registration whose config
        write landed but whose registry.json write did not (or got
        corrupted, or was hand-edited) would otherwise be left pointed at
        petsitter forever with nothing to say it needed restoring. See
        ``unregister()`` for the same reasoning applied to one agent.
        """
        all_log: list[dict[str, str]] = []
        registry = load_registry(self.config_dir)
        tracked = {aid for aid, e in (registry.get("agents") or {}).items()
                   if e.get("status") == "registered"}
        candidates = tracked | set(self.live_registered_ids())
        for agent_id in candidates:
            success, log = self.unregister(agent_id)
            all_log.extend(log)
        return all_log

    def get_registered(self) -> dict[str, Any]:
        """Report which agents are registered, checked live against each config file.

        registry.json is demoted to exactly what it's good for: the undo
        payload (the original value to restore) for whichever agents are, in
        fact, currently registered. The "registered" boolean itself always
        comes from ``Agent.is_registered()`` -- a fresh read of the real
        config file -- never from a stored flag, so this self-corrects if
        registry.json and reality ever disagree (a failed write, a hand
        edit, hand-deleting the key) instead of staying stuck on whatever
        petsitter last believed.

        A registry entry for an agent that is *not* live-registered is kept,
        not discarded here -- its backup data is still exactly what a future
        register()/unregister() cycle needs, and it costs nothing to hold
        onto until something explicitly cleans it up.
        """
        registry = load_registry(self.config_dir)
        agents_entry = dict(registry.get("agents") or {})
        result: dict[str, Any] = {}
        for agent_id, agent in self._agents.items():
            live = self._is_registered(agent_id, agent)
            entry = agents_entry.get(agent_id, {})
            merged = dict(entry)
            merged["status"] = "registered" if live else "unregistered"
            result[agent_id] = merged
        # Registry entries for agent ids petsitter no longer discovers (e.g. a
        # trick/agent file removed since it was registered) have no live
        # check to run -- surface them as the registry still describes them
        # rather than silently dropping the only record of their backup data.
        for agent_id, entry in agents_entry.items():
            if agent_id not in result:
                result[agent_id] = entry
        return {"agents": result}

    def _get(self, agent_id: str) -> Agent:
        agent = self._agents.get(agent_id)
        if not agent:
            known = list(self._agents.keys())
            raise KeyError(f"Unknown agent '{agent_id}'. Known: {known}")
        return agent

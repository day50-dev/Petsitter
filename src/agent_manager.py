"""AgentManager — discover agents, register, unregister, persist state."""

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

from agents import Agent, AgentContext, AgentResult, load_registry, save_registry

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

    def __init__(self, config_dir: str, agents_dir: str | Path = "agents"):
        self.config_dir = config_dir
        self._agents = _discover_agents(agents_dir)

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
                "icon": agent.icon,
                "tricks": agent.tricks,
                "model_config": agent.model_config,
                "detect": {
                    "status": detect_result.status,
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

        # Detect first
        detect = agent.detect()
        if detect.missing_env:
            log.append({"level": "ERROR", "message": f"Missing credentials: {', '.join(detect.missing_env)}"})
            return False, log

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

        # Persist registry
        registry = load_registry(self.config_dir)
        registry.setdefault("agents", {})[agent_id] = {
            "status": "registered",
            "backup": ctx.backup,
        }
        save_registry(self.config_dir, registry)

        log.append({"level": "INFO", "message": "Configuration saved"})
        return True, log

    def unregister(self, agent_id: str) -> tuple[bool, list[dict[str, str]]]:
        """Unregister a specific agent and restore its configuration."""
        agent = self._get(agent_id)
        log: list[dict[str, str]] = []

        registry = load_registry(self.config_dir)
        entry = registry.get("agents", {}).pop(agent_id, None)
        if entry is None:
            log.append({"level": "WARNING", "message": f"Agent {agent_id} is not registered"})
            return True, log

        backup = entry.get("backup", {})
        ctx = AgentContext(
            trickset_name=agent_id,
            model_config=dict(agent.model_config),
            trick_paths=list(agent.tricks),
            backup=backup,
        )

        try:
            agent_log = agent.unregister(ctx)
            log.extend(agent_log)
        except Exception as e:
            logger.exception("Agent %s unregister failed", agent_id)
            log.append({"level": "ERROR", "message": f"Restore failed: {e}"})
            # Still remove from registry even on error
        else:
            save_registry(self.config_dir, registry)

        return True, log

    def unregister_all(self) -> list[dict[str, str]]:
        """Unregister all registered agents. Called on shutdown."""
        all_log: list[dict[str, str]] = []
        for agent_id in list(self._agents):
            success, log = self.unregister(agent_id)
            all_log.extend(log)
        return all_log

    def get_registered(self) -> dict[str, Any]:
        """Return current registry state."""
        return load_registry(self.config_dir)

    def _get(self, agent_id: str) -> Agent:
        agent = self._agents.get(agent_id)
        if not agent:
            known = list(self._agents.keys())
            raise KeyError(f"Unknown agent '{agent_id}'. Known: {known}")
        return agent

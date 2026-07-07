"""Agent harness for OpenCode.

OpenCode config lives in ``~/.config/opencode/opencode.json`` (global) or
``./opencode.json`` (project).  To route through a proxy, set ``baseURL``
on the provider being used.

  https://opencode.ai/docs/providers/
"""

import json
from pathlib import Path
from typing import Any

from agents import Agent, AgentContext, AgentResult


GLOBAL_CONFIG = Path.home() / ".config" / "opencode" / "opencode.json"
PETSITTER_URL = "http://localhost:8080"


class OpenCodeAgent(Agent):
    id = "opencode"
    display_name = "OpenCode"
    description = "Open-source AI coding agent for the terminal"
    icon = "https://opencode.ai/favicon.ico"
    required_env: list[str] = []
    config_paths = ["~/.config/opencode/opencode.json"]
    tricks = [
        "tricks/json_mode.py",
        "tricks/tool_call.py",
    ]
    model_config: dict[str, Any] = {
        "url": "",
        "model": "",
        "key": "",
    }

    def detect(self) -> AgentResult:
        notes = []
        found: dict[str, str] = {}
        missing: list[str] = []

        if GLOBAL_CONFIG.exists():
            notes.append(f"Found {GLOBAL_CONFIG}")
            try:
                data = json.loads(GLOBAL_CONFIG.read_text())
                model = data.get("model", "")
                if model:
                    notes.append(f"Default model: {model}")
                # Check if any provider has a baseURL set already
                providers = data.get("provider", {})
                for pid, pcfg in providers.items() if isinstance(providers, dict) else []:
                    if isinstance(pcfg, dict) and pcfg.get("baseURL"):
                        notes.append(f"  {pid} baseURL: {pcfg['baseURL']}")
            except (json.JSONDecodeError, OSError):
                notes.append("Found opencode.json (unreadable)")
        else:
            missing.append("opencode.json")

        return AgentResult(
            status="ready" if GLOBAL_CONFIG.exists() else "missing_creds",
            found_env=found,
            missing_env=missing,
            message="; ".join(notes) if notes else "Not found",
        )

    def register(self, ctx: AgentContext) -> list[dict[str, str]]:
        log: list[dict[str, str]] = []
        backup: dict = ctx.backup

        existing: dict = {}
        if GLOBAL_CONFIG.exists():
            try:
                existing = json.loads(GLOBAL_CONFIG.read_text())
            except (json.JSONDecodeError, OSError):
                pass

        backup.setdefault("files", {})[f"file::{GLOBAL_CONFIG}"] = json.dumps(existing, indent=2) + "\n" if existing else ""

        # Determine which provider to patch
        model = existing.get("model", "")
        provider_id = model.split("/")[0] if "/" in model else "openai"

        providers = existing.get("provider", {})
        if not isinstance(providers, dict):
            providers = {}

        provider_cfg = providers.get(provider_id, {})
        if not isinstance(provider_cfg, dict):
            provider_cfg = {}

        existing_url = provider_cfg.get("baseURL", "")
        if existing_url:
            log.append({"level": "INFO", "message": f"Saved existing {provider_id} baseURL: {existing_url}"})

        provider_cfg["baseURL"] = f"{PETSITTER_URL}/v1"
        providers[provider_id] = provider_cfg
        existing["provider"] = providers

        GLOBAL_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        GLOBAL_CONFIG.write_text(json.dumps(existing, indent=2) + "\n")
        log.append({"level": "INFO", "message": f"Set {provider_id} baseURL → {PETSITTER_URL}/v1 in opencode.json"})

        log.append({"level": "INFO", "message": "OpenCode is now routed through petsitter"})
        return log

    def unregister(self, ctx: AgentContext) -> list[dict[str, str]]:
        log: list[dict[str, str]] = []
        backup = ctx.backup

        key = f"file::{GLOBAL_CONFIG}"
        original = backup.get("files", {}).get(key)
        if original:
            try:
                GLOBAL_CONFIG.write_text(original)
                log.append({"level": "INFO", "message": "Restored opencode.json"})
            except OSError:
                log.append({"level": "WARNING", "message": "Could not restore opencode.json"})
        elif GLOBAL_CONFIG.exists():
            try:
                GLOBAL_CONFIG.unlink()
                log.append({"level": "INFO", "message": "Removed opencode.json (created by petsitter)"})
            except OSError:
                pass

        log.append({"level": "INFO", "message": "Configuration restored"})
        return log

"""Agent harness for Claude Code (Anthropic's CLI coding tool).

The proper way to configure Claude Code is via ``~/.claude/settings.json``
with an ``env`` block.  This persists across sessions and is the official
approach recommended by Anthropic.

  https://code.claude.com/docs/en/llm-gateway-connect
"""

import json
from pathlib import Path
from typing import Any

from agents import Agent, AgentContext, AgentResult


SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
ANTHROPIC_BASE_URL = "ANTHROPIC_BASE_URL"
PETSITTER_URL = "http://localhost:8080"


class ClaudeCodeAgent(Agent):
    id = "claude-code"
    display_name = "Claude Code"
    description = "Anthropic official CLI coding agent"
    icon = "https://claude.ai/favicon.ico"
    required_env = ["ANTHROPIC_API_KEY"]
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
        result = super().detect()
        found = dict(result.found_env)
        notes = []

        if SETTINGS_PATH.exists():
            try:
                data = json.loads(SETTINGS_PATH.read_text())
                env_block = data.get("env", {})
                existing_url = env_block.get(ANTHROPIC_BASE_URL, "")
                if existing_url:
                    notes.append(f"Found {ANTHROPIC_BASE_URL}={existing_url} in settings.json")
                else:
                    notes.append("Found ~/.claude/settings.json")
            except (json.JSONDecodeError, OSError):
                notes.append("Found ~/.claude/settings.json (unreadable)")

        if found:
            notes.append(f"Found ${', '.join(found.keys())}")

        if result.missing_env:
            return AgentResult(
                status="missing_creds",
                found_env=found,
                missing_env=result.missing_env,
                message="; ".join(notes) if notes else f"Missing: {', '.join(result.missing_env)}",
            )
        return AgentResult(
            status="ready",
            found_env=found,
            message="; ".join(notes) if notes else "Ready",
        )

    def register(self, ctx: AgentContext) -> list[dict[str, str]]:
        log: list[dict[str, str]] = []
        backup: dict = ctx.backup

        # Read existing settings file (or start fresh)
        existing: dict = {}
        if SETTINGS_PATH.exists():
            try:
                existing = json.loads(SETTINGS_PATH.read_text())
            except (json.JSONDecodeError, OSError):
                pass

        # Save original into backup
        backup.setdefault("files", {})[f"file::{SETTINGS_PATH}"] = json.dumps(existing, indent=2) + "\n" if existing else ""

        # Merge the env block
        env_block = existing.get("env", {})
        existing_url = env_block.get(ANTHROPIC_BASE_URL, "")
        if existing_url:
            log.append({"level": "INFO", "message": f"Saved existing {ANTHROPIC_BASE_URL}={existing_url}"})
        env_block[ANTHROPIC_BASE_URL] = PETSITTER_URL
        existing["env"] = env_block

        # Write back
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(existing, indent=2) + "\n")
        log.append({"level": "INFO", "message": f"Set {ANTHROPIC_BASE_URL}={PETSITTER_URL} in ~/.claude/settings.json"})

        log.append({"level": "INFO", "message": "Claude Code is now routed through petsitter"})
        return log

    def unregister(self, ctx: AgentContext) -> list[dict[str, str]]:
        log: list[dict[str, str]] = []
        backup = ctx.backup

        key = f"file::{SETTINGS_PATH}"
        original = backup.get("files", {}).get(key)
        if original:
            try:
                SETTINGS_PATH.write_text(original)
                log.append({"level": "INFO", "message": "Restored ~/.claude/settings.json"})
            except OSError:
                log.append({"level": "WARNING", "message": "Could not restore ~/.claude/settings.json"})
        elif SETTINGS_PATH.exists():
            # No backup means we created it — remove the file entirely
            try:
                SETTINGS_PATH.unlink()
                log.append({"level": "INFO", "message": "Removed ~/.claude/settings.json (created by petsitter)"})
            except OSError:
                pass

        log.append({"level": "INFO", "message": "Configuration restored"})
        return log

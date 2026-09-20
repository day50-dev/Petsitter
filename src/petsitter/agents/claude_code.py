"""Agent harness for Claude Code (Anthropic's CLI coding tool).

The proper way to configure Claude Code is via ``~/.claude/settings.json``
with an ``env`` block.  This persists across sessions and is the official
approach recommended by Anthropic.

  https://code.claude.com/docs/en/llm-gateway-connect
"""

import json
from pathlib import Path
from typing import Any

from petsitter.agents import Agent, AgentContext, AgentResult


SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
ANTHROPIC_API_KEY = "ANTHROPIC_API_KEY"
ANTHROPIC_BASE_URL = "ANTHROPIC_BASE_URL"
PETSITTER_URL = "http://localhost:8080"


class ClaudeCodeAgent(Agent):
    id = "claude-code"
    display_name = "Claude Code"
    description = "Anthropic official CLI coding agent"
    icon = "https://claude.ai/favicon.ico"
    required_env = ["ANTHROPIC_API_KEY"]
    provider_name = "Anthropic"
    config_paths = ["~/.claude/settings.json"]
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
        """Report whether Claude Code can authenticate, by any of its means.

        An ``ANTHROPIC_API_KEY`` environment variable is only one of them, and
        not the usual one: signing in with a Claude account writes an OAuth
        blob to ``~/.claude/.credentials.json`` and never touches the
        environment. Requiring the env var marks an ordinary, fully working
        install as missing credentials.

        Petsitter does not need the credential itself either way - registering
        only points ``ANTHROPIC_BASE_URL`` at the proxy, and Claude Code keeps
        presenting whatever auth it already had.
        """
        result = super().detect()
        found = dict(result.found_env)
        notes = []
        authenticated = bool(found)
        if found:
            notes.append(f"Found ${', '.join(found.keys())}")

        if SETTINGS_PATH.exists():
            try:
                data = json.loads(SETTINGS_PATH.read_text())
                env_block = data.get("env", {}) or {}
                existing_url = env_block.get(ANTHROPIC_BASE_URL, "")
                if existing_url:
                    notes.append(f"Found {ANTHROPIC_BASE_URL}={existing_url} in settings.json")
                else:
                    notes.append("Found ~/.claude/settings.json")
                if env_block.get(ANTHROPIC_API_KEY):
                    authenticated = True
                    notes.append(f"Found {ANTHROPIC_API_KEY} in settings.json")
            except (json.JSONDecodeError, OSError):
                notes.append("Found ~/.claude/settings.json (unreadable)")

        if CREDENTIALS_PATH.exists():
            try:
                creds = json.loads(CREDENTIALS_PATH.read_text())
            except (json.JSONDecodeError, OSError):
                creds = {}
            if creds.get("claudeAiOauth"):
                authenticated = True
                notes.append("Signed in with a Claude account")
            elif creds.get("api_key"):
                authenticated = True
                notes.append("Found a stored API key")

        if not authenticated:
            return AgentResult(
                status="missing_creds",
                found_env=found,
                missing_env=result.missing_env,
                message="; ".join(notes) if notes else
                        "No Claude Code credentials found - run `claude` and sign in",
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

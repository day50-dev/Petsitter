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
from petsitter.agents import petsitter_url


class ClaudeCodeAgent(Agent):
    id = "claude-code"
    display_name = "Claude Code"
    description = "Anthropic official CLI coding agent"
    icon = "https://claude.ai/favicon.ico"
    required_env = ["ANTHROPIC_API_KEY"]
    provider_name = "Anthropic"
    trickset_filters = {"X-Title": "*", "Model": "claude*"}
    config_paths = ["~/.claude/settings.json"]
    # What a newly connected tool gets out of the box: see the traffic, keep
    # secrets out of it, carry your house rules, and be able to export a
    # conversation. Nothing here changes what the model is asked to do.
    tricks = [
        "tricks/secrets_protector.py",
        "tricks/rules_file.py",
        "tricks/exportit.py",
        "tricks/tool_monitor.py",
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
        """Point Claude Code at petsitter by setting one key in its settings.

        Only the single key is recorded for undo, not a snapshot of the whole
        file. Claude Code and the person using it both keep editing that file
        while petsitter is registered, and restoring a whole-file snapshot
        later would silently throw those edits away.
        """
        log: list[dict[str, str]] = []
        backup: dict = ctx.backup

        existing: dict = {}
        if SETTINGS_PATH.exists():
            try:
                existing = json.loads(SETTINGS_PATH.read_text())
            except (json.JSONDecodeError, OSError):
                log.append({"level": "WARNING",
                            "message": "~/.claude/settings.json is unreadable; leaving it alone"})
                return log

        env_block = dict(existing.get("env") or {})
        undo = backup.setdefault("undo", {})
        undo["file"] = str(SETTINGS_PATH)
        undo["file_existed"] = SETTINGS_PATH.exists()
        undo["had_env_block"] = "env" in existing
        undo["had_key"] = ANTHROPIC_BASE_URL in env_block
        undo["previous"] = env_block.get(ANTHROPIC_BASE_URL)

        if undo["previous"]:
            log.append({"level": "INFO",
                        "message": f"Saved existing {ANTHROPIC_BASE_URL}={undo['previous']}"})

        env_block[ANTHROPIC_BASE_URL] = petsitter_url()
        existing["env"] = env_block

        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(existing, indent=2) + "\n")
        log.append({"level": "INFO",
                    "message": f"Set {ANTHROPIC_BASE_URL}={petsitter_url()} in ~/.claude/settings.json"})
        log.append({"level": "INFO", "message": "Claude Code is now routed through petsitter"})
        return log

    def unregister(self, ctx: AgentContext) -> list[dict[str, str]]:
        """Undo exactly the key we set, leaving every other edit in place."""
        log: list[dict[str, str]] = []
        undo = (ctx.backup or {}).get("undo") or {}

        if not SETTINGS_PATH.exists():
            log.append({"level": "INFO", "message": "~/.claude/settings.json is already gone"})
            return log
        try:
            current = json.loads(SETTINGS_PATH.read_text())
        except (json.JSONDecodeError, OSError) as e:
            # Can't tell whether our key is still in there, so this must not be
            # reported as a successful unregister -- the caller needs to keep
            # this agent marked "registered" and retry later, or the env stays
            # pointed at petsitter with no record left to fix it.
            raise RuntimeError("~/.claude/settings.json is unreadable; not touching it") from e

        env_block = dict(current.get("env") or {})
        present = env_block.get(ANTHROPIC_BASE_URL)

        if undo.get("had_key") and undo.get("previous"):
            env_block[ANTHROPIC_BASE_URL] = undo["previous"]
            log.append({"level": "INFO",
                        "message": f"Put {ANTHROPIC_BASE_URL} back to {undo['previous']}"})
        elif present is not None:
            # Only remove a value we are responsible for; if the user pointed it
            # somewhere else in the meantime, that is their setting, not ours.
            if present == petsitter_url() or not undo:
                env_block.pop(ANTHROPIC_BASE_URL, None)
                log.append({"level": "INFO",
                            "message": f"Removed {ANTHROPIC_BASE_URL} from ~/.claude/settings.json"})
            else:
                log.append({"level": "WARNING",
                            "message": f"Left {ANTHROPIC_BASE_URL}={present} alone (changed since registering)"})
        else:
            log.append({"level": "INFO", "message": f"{ANTHROPIC_BASE_URL} was already unset"})

        if env_block:
            current["env"] = env_block
        else:
            # don't leave an empty env block behind if we introduced it
            if undo.get("had_env_block"):
                current["env"] = {}
            else:
                current.pop("env", None)

        try:
            SETTINGS_PATH.write_text(json.dumps(current, indent=2) + "\n")
        except OSError as e:
            # The key we set is still in the file. This must propagate so the
            # registry entry isn't cleared out from under a config that's still
            # pointed at petsitter.
            raise RuntimeError("Could not write ~/.claude/settings.json") from e

        log.append({"level": "INFO", "message": "Claude Code is talking to Anthropic directly again"})
        return log

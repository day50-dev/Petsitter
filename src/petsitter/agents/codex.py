"""Agent harness for Codex (OpenAI CLI coding agent).

Codex reads ``~/.codex/config.toml``.  The ``openai_base_url`` key overrides
the built-in OpenAI provider's endpoint — the official way to route through a
proxy.

  https://developers.openai.com/codex/config-advanced
"""

import os
from pathlib import Path
from typing import Any

from petsitter.agents import Agent, AgentContext, AgentResult


CODEX_HOME_VAR = "CODEX_HOME"
GLOBAL_CONFIG = Path.home() / ".codex" / "config.toml"
from petsitter.agents import petsitter_url
OPENAI_BASE_URL_KEY = "openai_base_url"


def _config_path() -> Path:
    override = os.environ.get(CODEX_HOME_VAR)
    if override:
        return Path(override) / "config.toml"
    return GLOBAL_CONFIG


class CodexAgent(Agent):
    id = "codex"
    display_name = "Codex"
    description = "OpenAI official CLI coding agent"
    icon = "https://chatgpt.com/favicon.ico"
    required_env = ["OPENAI_API_KEY"]
    provider_name = "OpenAI"
    trickset_filters = {"X-Title": "*", "Model": "gpt*"}
    config_paths = ["~/.codex/config.toml", "$CODEX_HOME/config.toml"]
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
        result = super().detect()
        config = _config_path()
        notes = list(result.found_env.keys())
        found = dict(result.found_env)

        if config.exists():
            content = config.read_text()
            notes.append(f"Found {config}")
            for line in content.splitlines():
                stripped = line.strip()
                if stripped.startswith(OPENAI_BASE_URL_KEY):
                    val = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                    if val:
                        notes.append(f"  {OPENAI_BASE_URL_KEY}={val}")

        return AgentResult(
            status="ready" if not result.missing_env else "missing_creds",
            found_env=found,
            missing_env=result.missing_env,
            message="; ".join(notes) if notes else "Not found",
        )

    def is_registered(self) -> bool:
        """Read ~/.codex/config.toml and check openai_base_url is ours now.

        A plain-text line scan, matching how ``detect`` and ``register``
        already parse this file -- no TOML dependency needed for one key.
        """
        config = _config_path()
        if not config.exists():
            return False
        try:
            content = config.read_text()
        except OSError:
            return False
        wanted = f"{petsitter_url()}/v1"
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith(OPENAI_BASE_URL_KEY):
                val = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                return val == wanted
        return False

    def register(self, ctx: AgentContext) -> list[dict[str, str]]:
        log: list[dict[str, str]] = []
        backup: dict = ctx.backup

        config = _config_path()
        original = ""
        if config.exists():
            original = config.read_text()

        backup.setdefault("files", {})[f"file::{config}"] = original

        # Find and replace openai_base_url, or append it
        new_value = f'{OPENAI_BASE_URL_KEY} = "{petsitter_url()}/v1"'
        if original.strip():
            lines = original.splitlines(keepends=True)
            replaced = False
            for i, line in enumerate(lines):
                if line.strip().startswith(OPENAI_BASE_URL_KEY):
                    existing = line.strip()
                    log.append({"level": "INFO", "message": f"Saved existing {existing}"})
                    # Preserve inline comment if any
                    comment = ""
                    if "#" in line:
                        comment = "  " + line[line.index("#"):]
                    lines[i] = f'{new_value}{comment}\n'
                    replaced = True
                    break
            content = "".join(lines)
            if not replaced:
                content += f"\n{new_value}\n"
        else:
            content = f"# Added by petsitter agent setup\n{new_value}\n"

        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(content)
        log.append({"level": "INFO", "message": f"Set {OPENAI_BASE_URL_KEY}={petsitter_url()}/v1 in ~/.codex/config.toml"})

        log.append({"level": "INFO", "message": "Codex is now routed through petsitter"})
        return log

    def unregister(self, ctx: AgentContext) -> list[dict[str, str]]:
        log: list[dict[str, str]] = []
        backup = ctx.backup

        config = _config_path()
        key = f"file::{config}"
        files = backup.get("files", {})
        have_backup = key in files
        original = files.get(key)
        if have_backup and original:
            config.write_text(original)
            log.append({"level": "INFO", "message": "Restored ~/.codex/config.toml"})
        elif have_backup and not original and config.exists():
            # We recorded that the file did not exist before we wrote it, so
            # deleting it is restoring, not destroying.
            config.unlink()
            log.append({"level": "INFO", "message": "Removed ~/.codex/config.toml (created by petsitter)"})
        elif not have_backup and config.exists():
            # No backup to restore from -- e.g. registry.json lost track of
            # this registration while the config file itself still points at
            # petsitter. We don't know what was here before, so the only safe
            # move is to strip just the key we recognize as ours, never
            # delete a file we didn't create and can't prove is empty
            # otherwise.
            wanted = f"{petsitter_url()}/v1"
            content = config.read_text()
            lines = content.splitlines(keepends=True)
            kept = []
            removed = False
            for line in lines:
                stripped = line.strip()
                if stripped.startswith(OPENAI_BASE_URL_KEY):
                    val = stripped.split("=", 1)[1].strip().strip('"').strip("'") if "=" in stripped else ""
                    if val == wanted:
                        removed = True
                        continue
                kept.append(line)
            if removed:
                config.write_text("".join(kept))
                log.append({"level": "INFO",
                            "message": f"Removed {OPENAI_BASE_URL_KEY} from ~/.codex/config.toml (no backup on record)"})
            else:
                log.append({"level": "INFO", "message": "~/.codex/config.toml did not point at petsitter"})

        log.append({"level": "INFO", "message": "Configuration restored"})
        return log

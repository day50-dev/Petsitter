"""Agent harness for Codex (OpenAI CLI coding agent).

Codex reads ``~/.codex/config.toml``.  The ``openai_base_url`` key overrides
the built-in OpenAI provider's endpoint — the official way to route through a
proxy.

  https://developers.openai.com/codex/config-advanced
"""

import os
from pathlib import Path
from typing import Any

from agents import Agent, AgentContext, AgentResult


CODEX_HOME_VAR = "CODEX_HOME"
GLOBAL_CONFIG = Path.home() / ".codex" / "config.toml"
PETSITTER_URL = "http://localhost:8080"
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

    def register(self, ctx: AgentContext) -> list[dict[str, str]]:
        log: list[dict[str, str]] = []
        backup: dict = ctx.backup

        config = _config_path()
        original = ""
        if config.exists():
            original = config.read_text()

        backup.setdefault("files", {})[f"file::{config}"] = original

        # Find and replace openai_base_url, or append it
        new_value = f'{OPENAI_BASE_URL_KEY} = "{PETSITTER_URL}/v1"'
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
        log.append({"level": "INFO", "message": f"Set {OPENAI_BASE_URL_KEY}={PETSITTER_URL}/v1 in ~/.codex/config.toml"})

        log.append({"level": "INFO", "message": "Codex is now routed through petsitter"})
        return log

    def unregister(self, ctx: AgentContext) -> list[dict[str, str]]:
        log: list[dict[str, str]] = []
        backup = ctx.backup

        config = _config_path()
        key = f"file::{config}"
        original = backup.get("files", {}).get(key)
        if original:
            config.write_text(original)
            log.append({"level": "INFO", "message": "Restored ~/.codex/config.toml"})
        elif config.exists():
            config.unlink()
            log.append({"level": "INFO", "message": "Removed ~/.codex/config.toml (created by petsitter)"})

        log.append({"level": "INFO", "message": "Configuration restored"})
        return log

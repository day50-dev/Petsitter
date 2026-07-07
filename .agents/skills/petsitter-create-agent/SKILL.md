---
name: petsitter-create-agent
description: Create new agent harnesses for the petsitter setup wizard. Use when the user asks to add, create, write, or implement a new agent for a coding tool or LLM harness. An agent is a Python class that detects credentials and swaps configuration so the tool routes through petsitter.
---

## How agents work

An agent is a Python class that subclasses `Agent` from `agents/__init__`. It lives in the `agents/` directory and defines how petsitter detects, registers, and unregisters a specific coding tool harness.

The flow is:

1. **Detect** — scan the environment for credentials and config files
2. **Register** — save original config, swap it to point at petsitter, persist backup
3. **Unregister** — restore original config from backup

Registration state is persisted to `~/.config/petsitter/registry.json` so the exit button can restore everything on shutdown.

## Creating an agent

1. Create a `.py` file in `agents/` (e.g. `agents/my_tool.py`)
2. Import `from agents import Agent, AgentContext, AgentResult`
3. Create a class that inherits from `Agent`
4. Set the class attributes: `id`, `display_name`, `description`, `icon`, `required_env`, `tricks`, `model_config`
5. Implement `detect()`, `register()`, `unregister()`

## Required class attributes

| Attribute | Purpose | Example |
|-----------|---------|---------|
| `id` | Short slug used in API routes and registry | `"claude-code"` |
| `display_name` | Human-readable name shown in dashboard | `"Claude Code"` |
| `description` | One-line description | `"Anthropic official CLI coding agent"` |
| `icon` | Favicon URL shown in agent card | `"https://claude.ai/favicon.ico"` |
| `required_env` | Env vars needed for the tool to work | `["ANTHROPIC_API_KEY"]` |
| `tricks` | Trick paths to include in the trickset | `["tricks/json_mode.py", "tricks/tool_call.py"]` |
| `model_config` | Default model config | `{"url": "", "model": "", "key": ""}` |

## How configuration swapping works

Each agent subclasses `Agent` and overrides `register()`/`unregister()`. The pattern is always:

1. **Read** the tool's config file (JSON, TOML, YAML, etc.)
2. **Save** the original content into `ctx.backup` under `files` key
3. **Write** the modified content with petsitter's URL
4. **On unregister**, restore from `ctx.backup`

Helpers on `Agent`:

| Helper | Purpose |
|--------|---------|
| `_patch_config_file(backup, file_path, patch_fn)` | Read, patch, write; saves original in backup |
| `_restore_config_file(backup, file_path)` | Restore a file from backup |
| `_save_env_var(backup, key)` | Save current env var value |
| `_set_env_var(key, value)` | Set an env var |
| `_restore_env_var(backup, key)` | Restore env var from backup |

## How each tool is configured

Each coding tool has its own config mechanism for pointing at a custom endpoint:

| Tool | Config mechanism | What to override |
|------|-----------------|-----------------|
| Claude Code | `~/.claude/settings.json` — `env` block | `ANTHROPIC_BASE_URL` in the env block |
| OpenCode | `~/.config/opencode/opencode.json` — `provider` block | `baseURL` on the active provider |
| Codex | `~/.codex/config.toml` — top-level key | `openai_base_url` |
| Any OpenAI client | `OPENAI_BASE_URL` env var | Env var (deprecated in Codex, but common) |

Research the tool's docs at setup time — the config approach may change. Check:
- GitHub repo README or docs site
- PRs/issues about custom endpoints or proxy support
- Environment variables the tool reads at startup

## Detect pattern

Detect should check:
1. That all `required_env` vars are set (call `super().detect()` first)
2. That the tool's config file exists
3. Any existing proxy/config already in place (so we can report it)

Return `AgentResult(status="ready", ...)` if good, or `AgentResult(status="missing_creds", missing_env=[...], ...)` if not.

The dashboard uses the status to enable/disable the "Set up" button.

## Register pattern

```python
def register(self, ctx: AgentContext) -> list[dict[str, str]]:
    log = []
    backup = ctx.backup
    config_path = Path.home() / ".config" / "tool" / "config.json"

    # 1. Read existing config
    existing = {}
    if config_path.exists():
        existing = json.loads(config_path.read_text())

    # 2. Save original
    backup.setdefault("files", {})[f"file::{config_path}"] = json.dumps(existing, indent=2) + "\n"

    # 3. Modify and write
    existing["base_url"] = "http://localhost:8080/v1"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(existing, indent=2) + "\n")
    log.append({"level": "INFO", "message": f"Set base_url → http://localhost:8080/v1"})

    log.append({"level": "INFO", "message": "Tool is now routed through petsitter"})
    return log
```

## Unregister pattern

```python
def unregister(self, ctx: AgentContext) -> list[dict[str, str]]:
    log = []
    backup = ctx.backup
    key = f"file::{config_path}"
    original = backup.get("files", {}).get(key)
    if original:
        config_path.write_text(original)
        log.append({"level": "INFO", "message": "Restored config"})
    elif config_path.exists():
        config_path.unlink()
        log.append({"level": "INFO", "message": "Removed config (created by petsitter)"})
    log.append({"level": "INFO", "message": "Configuration restored"})
    return log
```

## Gotchas

- Config files may contain API keys — the backup is stored in `~/.config/petsitter/registry.json` (plain JSON). Make sure users know this.
- TOML files need line-level insertion/replacement, not JSON parse/write. Preserve comments and ordering.
- If the tool has multiple config scopes (global, project, managed), prefer the global/user scope. Project scope varies per user.
- The `register()` method should be idempotent — running it twice should save the same backup.
- Some tools use `OPENAI_BASE_URL` env var; check if the tool has deprecated it in favor of a config file key (like Codex did).
- `required_env` should list env vars the tool **requires** to function, not optional ones.
- If the tool has no config file yet, register creates one and unregister removes it.

## Template

```python
"""Agent harness for <Tool Name>."""

import json
from pathlib import Path
from typing import Any

from agents import Agent, AgentContext, AgentResult


CONFIG_PATH = Path.home() / ".config" / "tool" / "config.json"
PETSITTER_URL = "http://localhost:8080"


class MyToolAgent(Agent):
    id = "my-tool"
    display_name = "My Tool"
    description = "One-line description"
    icon = "https://example.com/favicon.ico"
    required_env = ["MY_TOOL_API_KEY"]
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
        notes = list(result.found_env.keys())
        if CONFIG_PATH.exists():
            notes.append(f"Found {CONFIG_PATH}")
        return AgentResult(
            status="ready" if not result.missing_env else "missing_creds",
            found_env=result.found_env,
            missing_env=result.missing_env,
            message="; ".join(notes) if notes else "Not found",
        )

    def register(self, ctx: AgentContext) -> list[dict[str, str]]:
        log: list[dict[str, str]] = []
        backup = ctx.backup
        existing: dict = {}
        if CONFIG_PATH.exists():
            existing = json.loads(CONFIG_PATH.read_text())
        backup.setdefault("files", {})[f"file::{CONFIG_PATH}"] = json.dumps(existing, indent=2) + "\n"
        existing["base_url"] = f"{PETSITTER_URL}/v1"
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(existing, indent=2) + "\n")
        log.append({"level": "INFO", "message": f"Set base_url → {PETSITTER_URL}/v1"})
        log.append({"level": "INFO", "message": "My Tool is now routed through petsitter"})
        return log

    def unregister(self, ctx: AgentContext) -> list[dict[str, str]]:
        log: list[dict[str, str]] = []
        backup = ctx.backup
        key = f"file::{CONFIG_PATH}"
        original = backup.get("files", {}).get(key)
        if original:
            CONFIG_PATH.write_text(original)
            log.append({"level": "INFO", "message": "Restored configuration"})
        elif CONFIG_PATH.exists():
            CONFIG_PATH.unlink()
        log.append({"level": "INFO", "message": "Configuration restored"})
        return log
```

## Reference

- `agents/__init__.py` — Agent base class, AgentResult, AgentContext, registry helpers
- `agents/claude_code.py` — Claude Code (settings.json env block approach)
- `agents/opencode.py` — OpenCode (JSON provider baseURL approach)
- `agents/codex.py` — Codex (TOML openai_base_url approach)
- `src/agent_manager.py` — AgentManager: discover, register, unregister, registry persistence

"""Agent harness for pi, the terminal coding agent (pi.dev).

pi keeps its config in ``~/.pi/agent`` (``$PI_CODING_AGENT_DIR`` when that's
set). ``settings.json`` names the provider in use (``defaultProvider``), and a
provider entry in ``models.json`` with only ``baseUrl`` and ``headers``
re-points that provider and keeps its built-in models. Connecting writes that
entry, with an ``X-Title: pi`` header (pi sends no title to an endpoint that
isn't OpenRouter, only a ``pi (<os>; <arch>)`` User-Agent); disconnecting puts
``models.json`` back exactly as it was.

pi reads ``models.json`` when it starts, and again whenever ``/model`` opens.

  https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/models.md
"""

import json
import os
from pathlib import Path
from typing import Any

from petsitter.agents import Agent, AgentContext, AgentResult, petsitter_url

PI_DIR_VAR = "PI_CODING_AGENT_DIR"


def _dir() -> Path:
    override = os.environ.get(PI_DIR_VAR, "").strip()
    return Path(override) if override else Path.home() / ".pi" / "agent"


def _read(path: Path, strict: bool = False) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        if strict:
            raise RuntimeError(f"Can't read {path} ({e}); not changing it") from e
        return {}
    if not isinstance(data, dict):
        if strict:
            raise RuntimeError(f"{path} isn't a JSON object; not changing it")
        return {}
    return data


class PiAgent(Agent):
    id = "pi"
    display_name = "pi"
    description = "Minimal terminal coding agent (pi.dev)"
    icon = "/static/agents/pi.svg"   # bundled: gui/agents/
    required_env: list[str] = []
    provider_name = "its AI provider"
    title = "pi"
    trickset_filters = {"X-Title": "pi", "Model": "*"}
    config_paths = ["~/.pi/agent/settings.json", "$PI_CODING_AGENT_DIR/settings.json"]
    checked = "@earendil-works/pi-coding-agent 1.0.0 (`pi -p`), sandbox, 2026-10-03"
    tricks = [
        "tricks/context_monitor.py",
        "tricks/tool_monitor.py",
        "tricks/exportit.py",
    ]
    model_config: dict[str, Any] = {"url": "", "model": "", "key": ""}

    def installed(self) -> bool:
        return (_dir() / "settings.json").exists() or (_dir() / "models.json").exists() or super().installed()

    def _provider(self) -> str:
        return str(_read(_dir() / "settings.json").get("defaultProvider") or "")

    def detect(self) -> AgentResult:
        if not self.installed():
            return AgentResult(status="missing_creds", missing_env=["settings.json"], message="Not found")
        settings = _read(_dir() / "settings.json")
        notes = [f"Found {_dir()}"]
        if settings.get("defaultProvider"):
            notes.append(f"provider: {settings['defaultProvider']}")
        if settings.get("defaultModel"):
            notes.append(f"model: {settings['defaultModel']}")
        return AgentResult(status="ready", message="; ".join(notes))

    def is_registered(self) -> bool:
        wanted = f"{petsitter_url()}/v1"
        providers = _read(_dir() / "models.json").get("providers")
        return isinstance(providers, dict) and any(
            isinstance(p, dict) and str(p.get("baseUrl", "")).rstrip("/") == wanted for p in providers.values())

    def register(self, ctx: AgentContext) -> list[dict[str, str]]:
        log: list[dict[str, str]] = []
        provider = self._provider()
        if not provider:
            raise RuntimeError("pi has no default provider yet: choose a model in pi (/model, then Ctrl+S) first")
        path = _dir() / "models.json"
        data = _read(path, strict=True)
        # The file exactly as it was ("" if it didn't exist), so disconnecting
        # puts it back byte for byte.
        ctx.backup.setdefault("files", {})[f"file::{path}"] = path.read_text() if path.exists() else ""

        providers = data.get("providers")
        if not isinstance(providers, dict):
            providers = {}
        entry = providers.get(provider)
        if not isinstance(entry, dict):
            entry = {}
        if entry.get("baseUrl"):
            log.append({"level": "INFO", "message": f"Saved existing {provider} baseUrl: {entry['baseUrl']}"})
        entry["baseUrl"] = f"{petsitter_url()}/v1"
        headers = entry.get("headers") if isinstance(entry.get("headers"), dict) else {}
        headers.setdefault("X-Title", self.title)
        entry["headers"] = headers
        providers[provider] = entry
        data["providers"] = providers

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n")
        log.append({"level": "INFO", "message": f"Set {provider} baseUrl → {petsitter_url()}/v1 in {path}"})
        log.append({"level": "INFO", "message": "pi is routed through petsitter from its next start"})
        return log

    def unregister(self, ctx: AgentContext) -> list[dict[str, str]]:
        log: list[dict[str, str]] = []
        path = _dir() / "models.json"
        key = f"file::{path}"
        files = ctx.backup.get("files", {})
        if key in files:
            original = files[key]
            try:
                if original:
                    path.write_text(original)
                    log.append({"level": "INFO", "message": f"Restored {path}"})
                elif path.exists():
                    path.unlink()                 # petsitter created it
                    log.append({"level": "INFO", "message": f"Removed {path} (created by petsitter)"})
            except OSError as e:
                raise RuntimeError(f"Could not restore {path}") from e
        elif self.is_registered():
            # No backup on record: take out only petsitter's baseUrl.
            data = _read(path)
            wanted = f"{petsitter_url()}/v1"
            for entry in (data.get("providers") or {}).values():
                if isinstance(entry, dict) and str(entry.get("baseUrl", "")).rstrip("/") == wanted:
                    entry.pop("baseUrl", None)
            try:
                path.write_text(json.dumps(data, indent=2) + "\n")
            except OSError as e:
                raise RuntimeError(f"Could not write {path}") from e
            log.append({"level": "INFO", "message": f"Removed petsitter's baseUrl from {path} (no backup on record)"})
        log.append({"level": "INFO", "message": "Configuration restored"})
        return log

"""Agent harness for Cline's CLI (``cline``).

Cline keeps its providers in ``~/.cline/data/settings/providers.json``
(``$CLINE_PROVIDER_SETTINGS_PATH``, or under ``$CLINE_DATA_DIR`` /
``$CLINE_DIR``, when set). ``lastUsedProvider`` is the one in use, and each
provider's ``settings`` take a ``baseUrl`` and ``headers``. Connecting sets
those on the provider in use, with an ``X-Title: Cline`` header (Cline sends
no title to an OpenAI-compatible endpoint); disconnecting puts the file back
exactly as it was.

Cline reads the file when a run starts.

  https://docs.cline.bot/getting-started/config
"""

import json
import os
from pathlib import Path
from typing import Any

from petsitter.agents import Agent, AgentContext, AgentResult, petsitter_url


def _path() -> Path:
    explicit = os.environ.get("CLINE_PROVIDER_SETTINGS_PATH", "").strip()
    if explicit:
        return Path(explicit)
    data_dir = os.environ.get("CLINE_DATA_DIR", "").strip()
    if not data_dir:
        cline_dir = os.environ.get("CLINE_DIR", "").strip()
        data_dir = str((Path(cline_dir) if cline_dir else Path.home() / ".cline") / "data")
    return Path(data_dir) / "settings" / "providers.json"


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


class ClineAgent(Agent):
    id = "cline"
    display_name = "Cline"
    description = "Open-source AI coding agent, in the terminal (cline)"
    icon = "/static/agents/cline.png"   # bundled: gui/agents/
    required_env: list[str] = []
    provider_name = "its AI provider"
    title = "Cline"
    trickset_filters = {"X-Title": "Cline", "Model": "*"}
    config_paths = ["~/.cline/data/settings/providers.json"]
    checked = "cline 3.0.68 (`cline \"prompt\"`), sandbox, 2026-10-03"
    tricks = [
        "tricks/context_monitor.py",
        "tricks/tool_monitor.py",
        "tricks/exportit.py",
    ]
    model_config: dict[str, Any] = {"url": "", "model": "", "key": ""}

    def installed(self) -> bool:
        return _path().exists() or super().installed()

    def detect(self) -> AgentResult:
        path = _path()
        if not path.exists():
            return AgentResult(status="missing_creds", missing_env=["providers.json"], message="Not found")
        data = _read(path)
        notes = [f"Found {path}"]
        provider = data.get("lastUsedProvider")
        if provider:
            notes.append(f"provider: {provider}")
            settings = ((data.get("providers") or {}).get(provider) or {}).get("settings") or {}
            if isinstance(settings, dict) and settings.get("model"):
                notes.append(f"model: {settings['model']}")
        return AgentResult(status="ready", message="; ".join(notes))

    def is_registered(self) -> bool:
        wanted = f"{petsitter_url()}/v1"
        providers = _read(_path()).get("providers")
        return isinstance(providers, dict) and any(
            isinstance(p, dict) and isinstance(p.get("settings"), dict)
            and str(p["settings"].get("baseUrl", "")).rstrip("/") == wanted for p in providers.values())

    def register(self, ctx: AgentContext) -> list[dict[str, str]]:
        log: list[dict[str, str]] = []
        path = _path()
        data = _read(path, strict=True)
        provider = str(data.get("lastUsedProvider") or "")
        entry = (data.get("providers") or {}).get(provider) if provider else None
        if not isinstance(entry, dict) or not isinstance(entry.get("settings"), dict):
            raise RuntimeError("Cline has no provider set up yet: run `cline auth` first")
        ctx.backup.setdefault("files", {})[f"file::{path}"] = path.read_text()

        settings = entry["settings"]
        if settings.get("baseUrl"):
            log.append({"level": "INFO", "message": f"Saved existing {provider} baseUrl: {settings['baseUrl']}"})
        settings["baseUrl"] = f"{petsitter_url()}/v1"
        headers = settings.get("headers") if isinstance(settings.get("headers"), dict) else {}
        headers.setdefault("X-Title", self.title)
        settings["headers"] = headers

        path.write_text(json.dumps(data, indent=2) + "\n")
        log.append({"level": "INFO", "message": f"Set {provider} baseUrl → {petsitter_url()}/v1 in {path}"})
        log.append({"level": "INFO", "message": "Cline is routed through petsitter from its next run"})
        return log

    def unregister(self, ctx: AgentContext) -> list[dict[str, str]]:
        log: list[dict[str, str]] = []
        path = _path()
        key = f"file::{path}"
        files = ctx.backup.get("files", {})
        if key in files:
            try:
                path.write_text(files[key])
                log.append({"level": "INFO", "message": f"Restored {path}"})
            except OSError as e:
                raise RuntimeError(f"Could not restore {path}") from e
        elif self.is_registered():
            data = _read(path)
            wanted = f"{petsitter_url()}/v1"
            for entry in (data.get("providers") or {}).values():
                settings = entry.get("settings") if isinstance(entry, dict) else None
                if isinstance(settings, dict) and str(settings.get("baseUrl", "")).rstrip("/") == wanted:
                    settings.pop("baseUrl", None)
            try:
                path.write_text(json.dumps(data, indent=2) + "\n")
            except OSError as e:
                raise RuntimeError(f"Could not write {path}") from e
            log.append({"level": "INFO", "message": f"Removed petsitter's baseUrl from {path} (no backup on record)"})
        log.append({"level": "INFO", "message": "Configuration restored"})
        return log

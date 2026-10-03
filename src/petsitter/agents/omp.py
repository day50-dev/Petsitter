"""Agent harness for omp ("Oh My Pi", omp.sh), a fork of pi.

omp keeps its config in ``~/.omp/agent`` (``$PI_CODING_AGENT_DIR`` when that's
set; the ``.omp`` name itself follows ``$PI_CONFIG_DIR``). ``config.yml``
names the model in use (``modelRoles.default``, as ``provider/model``), and
``models.yml`` defines providers: a ``baseUrl`` and ``headers`` on the provider
in use routes it through a proxy. Connecting writes those, with an
``X-Title: omp`` header (omp sends only ``User-Agent: omp/<version>`` to an
endpoint that isn't OpenRouter); disconnecting puts ``models.yml`` back
exactly as it was.

  https://github.com/can1357/oh-my-pi/blob/main/docs/models.md
"""

import os
from pathlib import Path
from typing import Any

import yaml

from petsitter.agents import Agent, AgentContext, AgentResult, petsitter_url


def _dir() -> Path:
    override = os.environ.get("PI_CODING_AGENT_DIR", "").strip()
    if override:
        return Path(override)
    return Path.home() / (os.environ.get("PI_CONFIG_DIR", "").strip() or ".omp") / "agent"


def _first(*names: str) -> Path:
    d = _dir()
    return next((d / n for n in names if (d / n).exists()), d / names[0])


def _read(path: Path, strict: bool = False) -> dict:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as e:
        if strict:
            raise RuntimeError(f"Can't read {path} ({e}); not changing it") from e
        return {}
    if data is not None and not isinstance(data, dict):
        if strict:
            raise RuntimeError(f"{path} isn't the format omp uses; not changing it")
        return {}
    return data or {}


class OmpAgent(Agent):
    id = "omp"
    display_name = "omp"
    description = "Oh My Pi: terminal coding agent (omp.sh)"
    icon = "/static/agents/omp.png"   # bundled: gui/agents/
    required_env: list[str] = []
    provider_name = "its AI provider"
    title = "omp"
    trickset_filters = {"X-Title": "omp", "Model": "*"}
    config_paths = ["~/.omp/agent/config.yml", "~/.omp/agent/models.yml", "$PI_CODING_AGENT_DIR/models.yml"]
    checked = "@oh-my-pi/pi-coding-agent 18.5.0 (`omp -p`), sandbox, 2026-10-03"
    tricks = [
        "tricks/context_monitor.py",
        "tricks/tool_monitor.py",
        "tricks/exportit.py",
    ]
    model_config: dict[str, Any] = {"url": "", "model": "", "key": ""}

    @staticmethod
    def models_file() -> Path:
        return _first("models.yml", "models.yaml")

    @staticmethod
    def config_file() -> Path:
        return _first("config.yml", "config.yaml")

    def installed(self) -> bool:
        return self.config_file().exists() or self.models_file().exists() or super().installed()

    def _provider(self) -> str:
        roles = _read(self.config_file()).get("modelRoles") or {}
        default = str(roles.get("default") or "") if isinstance(roles, dict) else ""
        return default.split("/", 1)[0] if "/" in default else ""

    def detect(self) -> AgentResult:
        if not self.installed():
            return AgentResult(status="missing_creds", missing_env=["config.yml"], message="Not found")
        notes = [f"Found {_dir()}"]
        roles = _read(self.config_file()).get("modelRoles") or {}
        if isinstance(roles, dict) and roles.get("default"):
            notes.append(f"model: {roles['default']}")
        return AgentResult(status="ready", message="; ".join(notes))

    def is_registered(self) -> bool:
        wanted = f"{petsitter_url()}/v1"
        providers = _read(self.models_file()).get("providers")
        return isinstance(providers, dict) and any(
            isinstance(p, dict) and str(p.get("baseUrl", "")).rstrip("/") == wanted for p in providers.values())

    def register(self, ctx: AgentContext) -> list[dict[str, str]]:
        log: list[dict[str, str]] = []
        provider = self._provider()
        if not provider:
            raise RuntimeError("omp has no default model yet: choose one in omp (/model) first")
        path = self.models_file()
        data = _read(path, strict=True)
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
        path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
        log.append({"level": "INFO", "message": f"Set {provider} baseUrl → {petsitter_url()}/v1 in {path}"})
        log.append({"level": "INFO", "message": "omp is routed through petsitter from its next start"})
        return log

    def unregister(self, ctx: AgentContext) -> list[dict[str, str]]:
        log: list[dict[str, str]] = []
        path = self.models_file()
        key = f"file::{path}"
        files = ctx.backup.get("files", {})
        if key in files:
            original = files[key]
            try:
                if original:
                    path.write_text(original)
                    log.append({"level": "INFO", "message": f"Restored {path}"})
                elif path.exists():
                    path.unlink()
                    log.append({"level": "INFO", "message": f"Removed {path} (created by petsitter)"})
            except OSError as e:
                raise RuntimeError(f"Could not restore {path}") from e
        elif self.is_registered():
            data = _read(path)
            wanted = f"{petsitter_url()}/v1"
            for entry in (data.get("providers") or {}).values():
                if isinstance(entry, dict) and str(entry.get("baseUrl", "")).rstrip("/") == wanted:
                    entry.pop("baseUrl", None)
            try:
                path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
            except OSError as e:
                raise RuntimeError(f"Could not write {path}") from e
            log.append({"level": "INFO", "message": f"Removed petsitter's baseUrl from {path} (no backup on record)"})
        log.append({"level": "INFO", "message": "Configuration restored"})
        return log

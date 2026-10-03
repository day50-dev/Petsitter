"""Agent harness for Hermes Agent (Nous Research).

Hermes keeps its model in ``~/.hermes/config.yaml`` (``$HERMES_HOME/config.yaml``
when that's set), under ``model:``. ``base_url`` is only honoured for
``provider: custom``, so connecting sets ``provider: custom`` and ``base_url``
to petsitter, and an ``X-Title: Hermes Agent`` header (``extra_headers``) so
its requests can be told apart: its User-Agent is the OpenAI SDK's.
Disconnecting puts the file back exactly as it was.

Hermes reads the file when a session starts: a session already open keeps
the model it started with.

  https://hermes-agent.nousresearch.com/docs/user-guide/configuring-models
"""

import os
from pathlib import Path
from typing import Any

import yaml

from petsitter.agents import Agent, AgentContext, AgentResult, petsitter_url

HERMES_HOME_VAR = "HERMES_HOME"


def _config_path() -> Path:
    home = os.environ.get(HERMES_HOME_VAR, "").strip()
    return (Path(home) if home else Path.home() / ".hermes") / "config.yaml"


def _load(path: Path, strict: bool = False) -> dict:
    """config.yaml as a dict; {} if missing. Unreadable: {} unless strict,
    when it raises, so connecting never writes over a file it can't read."""
    try:
        data = yaml.safe_load(path.read_text()) if path.exists() else {}
    except (yaml.YAMLError, OSError) as e:
        if strict:
            raise RuntimeError(f"Can't read {path} ({e}); not changing it") from e
        return {}
    if data is not None and not isinstance(data, dict):
        if strict:
            raise RuntimeError(f"{path} isn't the format Hermes uses; not changing it")
        return {}
    return data or {}


class HermesAgent(Agent):
    id = "hermes"
    display_name = "Hermes Agent"
    description = "Nous Research's agent that grows with you"
    icon = "/static/agents/hermes.png"   # bundled: gui/agents/
    required_env: list[str] = []
    provider_name = "its AI provider"
    # Hermes sends no X-Title of its own (its User-Agent is the OpenAI SDK's),
    # so connecting gives it one, and its channel matches only Hermes.
    title = "Hermes Agent"
    trickset_filters = {"X-Title": "Hermes Agent", "Model": "*"}
    config_paths = ["~/.hermes/config.yaml", "$HERMES_HOME/config.yaml"]
    checked = "hermes-agent 0.19.0 (CLI, `hermes -z`), sandbox, 2026-10-03"
    tricks = [
        "tricks/context_monitor.py",
        "tricks/tool_monitor.py",
        "tricks/exportit.py",
    ]
    model_config: dict[str, Any] = {"url": "", "model": "", "key": ""}

    def installed(self) -> bool:
        return _config_path().exists() or super().installed()

    def detect(self) -> AgentResult:
        path = _config_path()
        if not path.exists():
            return AgentResult(status="missing_creds", missing_env=["config.yaml"], message="Not found")
        model = _load(path).get("model") or {}
        notes = [f"Found {path}"]
        if isinstance(model, dict):
            if model.get("provider"):
                notes.append(f"provider: {model['provider']}")
            if model.get("default"):
                notes.append(f"model: {model['default']}")
            if model.get("base_url"):
                notes.append(f"base_url: {model['base_url']}")
        return AgentResult(status="ready", message="; ".join(notes))

    def is_registered(self) -> bool:
        model = _load(_config_path()).get("model")
        return (isinstance(model, dict) and str(model.get("provider", "")).lower() == "custom"
                and str(model.get("base_url", "")).rstrip("/") == f"{petsitter_url()}/v1")

    def register(self, ctx: AgentContext) -> list[dict[str, str]]:
        log: list[dict[str, str]] = []
        path = _config_path()
        key = f"file::{path}"
        # The file exactly as it was, so disconnecting puts back every comment
        # and every key, not a re-serialization. "" means it didn't exist.
        original = path.read_text() if path.exists() else ""
        data = _load(path, strict=True)
        ctx.backup.setdefault("files", {})[key] = original
        model = data.get("model")
        if isinstance(model, str):            # `model: name` shorthand
            model = {"default": model}
        if not isinstance(model, dict):
            model = {}
        was = f"{model.get('provider') or 'default provider'}" + (f" at {model['base_url']}" if model.get("base_url") else "")
        model["provider"] = "custom"
        model["base_url"] = f"{petsitter_url()}/v1"
        model.pop("api_mode", None)           # petsitter speaks chat completions
        headers = model.get("extra_headers") if isinstance(model.get("extra_headers"), dict) else {}
        headers.setdefault("X-Title", self.title)
        model["extra_headers"] = headers
        data["model"] = model

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
        log.append({"level": "INFO", "message": f"Was {was}; set provider: custom, base_url: {petsitter_url()}/v1 in {path}"})
        log.append({"level": "INFO", "message": "Hermes is routed through petsitter from its next session"})
        return log

    def unregister(self, ctx: AgentContext) -> list[dict[str, str]]:
        log: list[dict[str, str]] = []
        path = _config_path()
        files = ctx.backup.get("files", {})
        key = f"file::{path}"
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
                # still pointed at petsitter: must not be reported as done
                raise RuntimeError(f"Could not restore {path}") from e
        elif self.is_registered():
            # No backup on record, but the file points at petsitter: take out
            # only what's ours. What the provider was before is unknown.
            data = _load(path)
            model = data.get("model") or {}
            model.pop("base_url", None)
            if str(model.get("provider", "")).lower() == "custom":
                model.pop("provider", None)
            data["model"] = model
            try:
                path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
            except OSError as e:
                raise RuntimeError(f"Could not write {path}") from e
            log.append({"level": "INFO", "message": f"Removed petsitter's base_url from {path} (no backup on record)"})
        log.append({"level": "INFO", "message": "Configuration restored"})
        return log

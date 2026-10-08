"""Agent harness for OpenClaw (openclaw.ai), the personal AI assistant.

OpenClaw keeps its config in ``~/.openclaw/openclaw.json`` (``$OPENCLAW_CONFIG_PATH``,
or ``openclaw.json`` under ``$OPENCLAW_STATE_DIR``, when set). The model in use
is ``agents.defaults.model.primary``, as ``provider/model``, and an entry in
``models.providers.<provider>`` overrides that provider's ``baseUrl`` and
``headers`` while keeping the rest of it (OpenClaw's own built-in providers
included). Connecting writes those on the provider in use, with an
``X-Title: OpenClaw`` header (OpenClaw sends no title to an endpoint of yours);
disconnecting puts the file back exactly as it was.

The file is JSON5. Petsitter edits it only when it's plain JSON, as OpenClaw
writes it; a hand-written one with comments is left alone and says so. The
Gateway watches the file and picks up the change; ``openclaw agent exec`` reads
it when it starts.

  https://github.com/openclaw/openclaw/blob/main/docs/concepts/model-providers/custom-providers.md
"""

import json
import os
from pathlib import Path
from typing import Any

from petsitter.agents import Agent, AgentContext, AgentResult, petsitter_url


def _path() -> Path:
    explicit = os.environ.get("OPENCLAW_CONFIG_PATH", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    state = os.environ.get("OPENCLAW_STATE_DIR", "").strip()
    return (Path(state).expanduser() if state else Path.home() / ".openclaw") / "openclaw.json"


def _read(path: Path, strict: bool = False) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        if strict:
            raise RuntimeError(f"Can't read {path} as plain JSON ({e}): OpenClaw allows JSON5 (comments, "
                               "trailing commas), which petsitter won't rewrite; not changing it") from e
        return {}
    if not isinstance(data, dict):
        if strict:
            raise RuntimeError(f"{path} isn't a JSON object; not changing it")
        return {}
    return data


def _primary(data: dict) -> str:
    model = ((data.get("agents") or {}).get("defaults") or {}).get("model")
    if isinstance(model, dict):
        model = model.get("primary")
    return model if isinstance(model, str) else ""


class OpenClawAgent(Agent):
    id = "openclaw"
    display_name = "OpenClaw"
    description = "Personal AI assistant across your chat apps (openclaw.ai)"
    icon = "/static/agents/openclaw.svg"   # bundled: gui/agents/ (from the openclaw package, MIT)
    required_env: list[str] = []
    provider_name = "its AI provider"
    title = "OpenClaw"
    trickset_filters = {"X-Title": "OpenClaw", "Model": "*"}
    config_paths = ["~/.openclaw/openclaw.json", "$OPENCLAW_CONFIG_PATH", "$OPENCLAW_STATE_DIR/openclaw.json"]
    checked = "openclaw 2026.9.9 (`openclaw agent exec`), sandbox, 2026-10-08"
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
            return AgentResult(status="missing_creds", missing_env=["openclaw.json"], message="Not found")
        notes = [f"Found {path}"]
        primary = _primary(_read(path))
        if primary:
            notes.append(f"model: {primary}")
        return AgentResult(status="ready", message="; ".join(notes))

    @staticmethod
    def _wanted() -> tuple[str, str]:
        base = petsitter_url()
        return base, f"{base}/v1"

    def is_registered(self) -> bool:
        providers = (_read(_path()).get("models") or {}).get("providers")
        return isinstance(providers, dict) and any(
            isinstance(p, dict) and str(p.get("baseUrl", "")).rstrip("/") in self._wanted()
            for p in providers.values())

    def register(self, ctx: AgentContext) -> list[dict[str, str]]:
        log: list[dict[str, str]] = []
        path = _path()
        data = _read(path, strict=True)
        primary = _primary(data)
        if "/" not in primary:
            raise RuntimeError("OpenClaw has no model chosen yet: run `openclaw onboard` first")
        provider = primary.split("/", 1)[0]
        ctx.backup.setdefault("files", {})[f"file::{path}"] = path.read_text()

        models = data.get("models") if isinstance(data.get("models"), dict) else {}
        providers = models.get("providers") if isinstance(models.get("providers"), dict) else {}
        entry = providers.get(provider) if isinstance(providers.get(provider), dict) else {}
        if entry.get("baseUrl"):
            log.append({"level": "INFO", "message": f"Saved existing {provider} baseUrl: {entry['baseUrl']}"})
        # Anthropic's API is rooted above /v1 (its client adds /v1/messages);
        # everything else OpenClaw speaks is OpenAI-style, rooted at /v1.
        anthropic = entry.get("api") == "anthropic-messages" or (not entry.get("api") and provider == "anthropic")
        base, v1 = self._wanted()
        entry["baseUrl"] = base if anthropic else v1
        headers = entry.get("headers") if isinstance(entry.get("headers"), dict) else {}
        headers.setdefault("X-Title", self.title)
        entry["headers"] = headers
        providers[provider] = entry
        models["providers"] = providers
        data["models"] = models

        path.write_text(json.dumps(data, indent=2) + "\n")
        log.append({"level": "INFO", "message": f"Set {provider} baseUrl → {entry['baseUrl']} in {path}"})
        log.append({"level": "INFO", "message": "OpenClaw is routed through petsitter (the Gateway picks it up)"})
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
            for entry in ((data.get("models") or {}).get("providers") or {}).values():
                if isinstance(entry, dict) and str(entry.get("baseUrl", "")).rstrip("/") in self._wanted():
                    entry.pop("baseUrl", None)
            try:
                path.write_text(json.dumps(data, indent=2) + "\n")
            except OSError as e:
                raise RuntimeError(f"Could not write {path}") from e
            log.append({"level": "INFO", "message": f"Removed petsitter's baseUrl from {path} (no backup on record)"})
        log.append({"level": "INFO", "message": "Configuration restored"})
        return log

"""Agent harness for OpenCode.

OpenCode config lives in ``~/.config/opencode/opencode.json`` (global) or
``./opencode.json`` (project).  To route through a proxy, set ``baseURL``
on the provider being used.

  https://opencode.ai/docs/providers/
"""

import json
import os
import re
from pathlib import Path
from typing import Any

from petsitter.agents import Agent, AgentContext, AgentResult, petsitter_url


GLOBAL_CONFIG = Path.home() / ".config" / "opencode" / "opencode.json"


def _strip_jsonc(text: str) -> str:
    """JSON with comments and trailing commas (.jsonc) as plain JSON."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':                                   # a string: copy as is
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):
            i = text.find("\n", i)
            i = n if i < 0 else i
        elif text.startswith("/*", i):
            i = text.find("*/", i + 2)
            i = n if i < 0 else i + 2
        else:
            out.append(c)
            i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def read_config(path: Path, strict: bool = False) -> dict:
    """An OpenCode-style config, .json or .jsonc; {} if missing. Unreadable:
    {} unless strict, when it raises, so connecting never writes over a file
    it can't read."""
    if not path.exists():
        return {}
    try:
        data = json.loads(_strip_jsonc(path.read_text()))
    except (OSError, ValueError) as e:
        if strict:
            raise RuntimeError(f"Can't read {path} ({e}); not changing it") from e
        return {}
    if not isinstance(data, dict):
        if strict:
            raise RuntimeError(f"{path} isn't a JSON object; not changing it")
        return {}
    return data


class OpenCodeAgent(Agent):
    id = "opencode"
    display_name = "OpenCode"
    description = "Open-source AI coding agent for the terminal"
    icon = "/static/agents/opencode.png"   # bundled: gui/agents/
    required_env: list[str] = []
    provider_name = "its AI provider"
    trickset_filters = {"X-Title": "opencode*", "Model": "*"}
    config_paths = ["~/.config/opencode/opencode.json"]
    # What a newly connected tool gets out of the box: see the traffic, keep
    # secrets out of it, carry your house rules, and be able to export a
    # conversation. Nothing here changes what the model is asked to do.
    tricks = [
        "tricks/context_monitor.py",
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
    file_label = "opencode.json"
    default_provider = "openai"     # patched when the config names none

    def config_file(self) -> Path:
        """The global config this tool reads (a subclass for a fork overrides it)."""
        return GLOBAL_CONFIG

    def installed(self) -> bool:
        return self.config_file().exists() or super().installed()

    def detect(self) -> AgentResult:
        path = self.config_file()
        if not path.exists():
            return AgentResult(status="missing_creds", missing_env=[self.file_label], message="Not found")
        notes = [f"Found {path}"]
        data = read_config(path)
        if data.get("model"):
            notes.append(f"Default model: {data['model']}")
        providers = data.get("provider", {})
        for pid, pcfg in providers.items() if isinstance(providers, dict) else []:
            if isinstance(pcfg, dict):
                burl = (pcfg.get("options") or {}).get("baseURL") or pcfg.get("baseURL")
                if burl:
                    notes.append(f"  {pid} baseURL: {burl}")
        return AgentResult(status="ready", message="; ".join(notes))

    def is_registered(self) -> bool:
        """Whether some provider's baseURL points at petsitter now.

        register() doesn't always patch the same provider id (it follows the
        default model, or falls back to the first configured provider), so
        this checks whether any provider currently points at petsitter rather
        than guessing which one register() would have chosen.
        """
        path = self.config_file()
        if not path.exists():
            return False
        wanted = f"{petsitter_url()}/v1"
        providers = read_config(path).get("provider", {})
        if not isinstance(providers, dict):
            return False
        return any(isinstance(p, dict) and isinstance(p.get("options"), dict)
                   and p["options"].get("baseURL") == wanted for p in providers.values())

    def register(self, ctx: AgentContext) -> list[dict[str, str]]:
        log: list[dict[str, str]] = []
        path = self.config_file()
        # The file exactly as it was ("" if it didn't exist), so disconnecting
        # puts back every comment, not a re-serialization.
        existing = read_config(path, strict=True)
        ctx.backup.setdefault("files", {})[f"file::{path}"] = path.read_text() if path.exists() else ""

        # The provider in use: the default model's, else the first configured one.
        model = existing.get("model", "")
        provider_id = model.split("/")[0] if "/" in model else ""
        providers = existing.get("provider", {})
        if not isinstance(providers, dict):
            providers = {}
        if not provider_id:
            provider_id = next(iter(providers), self.default_provider)

        provider_cfg = providers.get(provider_id, {})
        if not isinstance(provider_cfg, dict):
            provider_cfg = {}
        options = provider_cfg.get("options", {})
        if not isinstance(options, dict):
            options = {}
        if options.get("baseURL"):
            log.append({"level": "INFO", "message": f"Saved existing {provider_id} baseURL: {options['baseURL']}"})
        options["baseURL"] = f"{petsitter_url()}/v1"
        provider_cfg["options"] = options
        providers[provider_id] = provider_cfg
        existing["provider"] = providers

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(existing, indent=2) + "\n")
        log.append({"level": "INFO", "message": f"Set {provider_id} baseURL → {petsitter_url()}/v1 in {path}"})
        log.append({"level": "INFO", "message": f"{self.display_name} is now routed through petsitter"})
        return log

    def unregister(self, ctx: AgentContext) -> list[dict[str, str]]:
        log: list[dict[str, str]] = []
        path = self.config_file()
        key = f"file::{path}"
        files = ctx.backup.get("files", {})
        if key in files:
            original = files[key]
            try:
                if original:
                    path.write_text(original)
                    log.append({"level": "INFO", "message": f"Restored {path}"})
                elif path.exists():
                    # it didn't exist before we wrote it: removing it is restoring
                    path.unlink()
                    log.append({"level": "INFO", "message": f"Removed {path} (created by petsitter)"})
            except OSError as e:
                # still pointed at petsitter on disk: must not be reported as
                # done, so the caller keeps it "registered" and retries
                raise RuntimeError(f"Could not restore {path}") from e
        elif path.exists():
            # No backup on record (registry.json lost it) while the file points
            # at petsitter: clear only the baseURLs that are ours.
            data = read_config(path)
            if not data:
                raise RuntimeError(f"{path} is unreadable; not touching it")
            wanted = f"{petsitter_url()}/v1"
            removed = False
            for pcfg in (data.get("provider") or {}).values():
                if isinstance(pcfg, dict) and isinstance(pcfg.get("options"), dict) \
                        and pcfg["options"].get("baseURL") == wanted:
                    pcfg["options"].pop("baseURL", None)
                    removed = True
            if removed:
                try:
                    path.write_text(json.dumps(data, indent=2) + "\n")
                except OSError as e:
                    raise RuntimeError(f"Could not write {path}") from e
                log.append({"level": "INFO", "message": f"Removed petsitter's baseURL from {path} (no backup on record)"})
            else:
                log.append({"level": "INFO", "message": f"{path} did not point at petsitter"})
        log.append({"level": "INFO", "message": "Configuration restored"})
        return log

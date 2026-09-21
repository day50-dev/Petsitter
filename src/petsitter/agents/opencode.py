"""Agent harness for OpenCode.

OpenCode config lives in ``~/.config/opencode/opencode.json`` (global) or
``./opencode.json`` (project).  To route through a proxy, set ``baseURL``
on the provider being used.

  https://opencode.ai/docs/providers/
"""

import json
from pathlib import Path
from typing import Any

from petsitter.agents import Agent, AgentContext, AgentResult


GLOBAL_CONFIG = Path.home() / ".config" / "opencode" / "opencode.json"
from petsitter.agents import petsitter_url


class OpenCodeAgent(Agent):
    id = "opencode"
    display_name = "OpenCode"
    description = "Open-source AI coding agent for the terminal"
    icon = "https://opencode.ai/favicon.ico"
    required_env: list[str] = []
    provider_name = "its AI provider"
    trickset_filters = {"X-Title": "opencode*", "Model": "*"}
    config_paths = ["~/.config/opencode/opencode.json"]
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
        notes = []
        found: dict[str, str] = {}
        missing: list[str] = []

        if GLOBAL_CONFIG.exists():
            notes.append(f"Found {GLOBAL_CONFIG}")
            try:
                data = json.loads(GLOBAL_CONFIG.read_text())
                model = data.get("model", "")
                if model:
                    notes.append(f"Default model: {model}")
                # Check if any provider has a baseURL set already
                providers = data.get("provider", {})
                for pid, pcfg in providers.items() if isinstance(providers, dict) else []:
                    if isinstance(pcfg, dict):
                        burl = pcfg.get("options", {}).get("baseURL") or pcfg.get("baseURL")
                        if burl:
                            notes.append(f"  {pid} baseURL: {burl}")
            except (json.JSONDecodeError, OSError):
                notes.append("Found opencode.json (unreadable)")
        else:
            missing.append("opencode.json")

        return AgentResult(
            status="ready" if GLOBAL_CONFIG.exists() else "missing_creds",
            found_env=found,
            missing_env=missing,
            message="; ".join(notes) if notes else "Not found",
        )

    def is_registered(self) -> bool:
        """Read opencode.json and check some provider's baseURL is ours now.

        register() doesn't always patch the same provider id (it follows the
        default model, or falls back to the first configured provider), so
        this checks whether any provider currently points at petsitter rather
        than guessing which one register() would have chosen.
        """
        if not GLOBAL_CONFIG.exists():
            return False
        try:
            data = json.loads(GLOBAL_CONFIG.read_text())
        except (json.JSONDecodeError, OSError):
            return False
        wanted = f"{petsitter_url()}/v1"
        providers = data.get("provider", {})
        if not isinstance(providers, dict):
            return False
        for pcfg in providers.values():
            if not isinstance(pcfg, dict):
                continue
            options = pcfg.get("options", {})
            burl = options.get("baseURL") if isinstance(options, dict) else None
            if burl == wanted:
                return True
        return False

    def register(self, ctx: AgentContext) -> list[dict[str, str]]:
        log: list[dict[str, str]] = []
        backup: dict = ctx.backup

        existing: dict = {}
        if GLOBAL_CONFIG.exists():
            try:
                existing = json.loads(GLOBAL_CONFIG.read_text())
            except (json.JSONDecodeError, OSError):
                pass

        backup.setdefault("files", {})[f"file::{GLOBAL_CONFIG}"] = json.dumps(existing, indent=2) + "\n" if existing else ""

        # Determine which provider to patch — use first provider that has a key
        model = existing.get("model", "")
        provider_id = model.split("/")[0] if "/" in model else ""
        if not provider_id:
            providers = existing.get("provider", {})
            if isinstance(providers, dict):
                provider_id = next(iter(providers), "openai")

        providers = existing.get("provider", {})
        if not isinstance(providers, dict):
            providers = {}

        provider_cfg = providers.get(provider_id, {})
        if not isinstance(provider_cfg, dict):
            provider_cfg = {}

        existing_url = provider_cfg.get("options", {}).get("baseURL", "")
        if existing_url:
            log.append({"level": "INFO", "message": f"Saved existing {provider_id} baseURL: {existing_url}"})

        options = provider_cfg.get("options", {})
        if not isinstance(options, dict):
            options = {}
        options["baseURL"] = f"{petsitter_url()}/v1"
        provider_cfg["options"] = options
        providers[provider_id] = provider_cfg
        existing["provider"] = providers

        GLOBAL_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        GLOBAL_CONFIG.write_text(json.dumps(existing, indent=2) + "\n")
        log.append({"level": "INFO", "message": f"Set {provider_id} baseURL → {petsitter_url()}/v1 in opencode.json"})

        log.append({"level": "INFO", "message": "OpenCode is now routed through petsitter"})
        return log

    def unregister(self, ctx: AgentContext) -> list[dict[str, str]]:
        log: list[dict[str, str]] = []
        backup = ctx.backup

        key = f"file::{GLOBAL_CONFIG}"
        files = backup.get("files", {})
        have_backup = key in files
        original = files.get(key)
        if have_backup and original:
            try:
                GLOBAL_CONFIG.write_text(original)
                log.append({"level": "INFO", "message": "Restored opencode.json"})
            except OSError as e:
                # baseURL is still pointed at petsitter in the file on disk, so
                # this must not be reported as done -- propagate so the caller
                # keeps this agent marked "registered" and retries later.
                raise RuntimeError("Could not restore opencode.json") from e
        elif have_backup and not original and GLOBAL_CONFIG.exists():
            # We recorded that the file did not exist (or was empty) before
            # we wrote it, so deleting it is restoring, not destroying.
            try:
                GLOBAL_CONFIG.unlink()
                log.append({"level": "INFO", "message": "Removed opencode.json (created by petsitter)"})
            except OSError as e:
                raise RuntimeError("Could not remove opencode.json") from e
        elif not have_backup and GLOBAL_CONFIG.exists():
            # No backup to restore from -- e.g. registry.json lost track of
            # this registration while the file itself still points at
            # petsitter. We don't know what was here before, so the only
            # safe move is to clear the baseURL keys we recognize as ours,
            # never delete a file we didn't create and can't prove is empty
            # otherwise.
            try:
                data = json.loads(GLOBAL_CONFIG.read_text())
            except (json.JSONDecodeError, OSError) as e:
                raise RuntimeError("opencode.json is unreadable; not touching it") from e
            wanted = f"{petsitter_url()}/v1"
            providers = data.get("provider", {})
            removed = False
            if isinstance(providers, dict):
                for pcfg in providers.values():
                    if not isinstance(pcfg, dict):
                        continue
                    options = pcfg.get("options", {})
                    if isinstance(options, dict) and options.get("baseURL") == wanted:
                        options.pop("baseURL", None)
                        removed = True
            if removed:
                try:
                    GLOBAL_CONFIG.write_text(json.dumps(data, indent=2) + "\n")
                except OSError as e:
                    raise RuntimeError("Could not write opencode.json") from e
                log.append({"level": "INFO",
                            "message": "Removed baseURL from opencode.json (no backup on record)"})
            else:
                log.append({"level": "INFO", "message": "opencode.json did not point at petsitter"})

        log.append({"level": "INFO", "message": "Configuration restored"})
        return log

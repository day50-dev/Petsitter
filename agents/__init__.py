"""Agent base class and helpers for petsitter harness setup."""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class AgentResult:
    status: str                        # "ready" | "missing_creds" | "error"
    found_env: dict[str, str] = field(default_factory=dict)
    missing_env: list[str] = field(default_factory=list)
    message: str = ""


@dataclass
class AgentContext:
    trickset_name: str
    model_config: dict[str, Any]
    trick_paths: list[str]
    backup: dict[str, Any] = field(default_factory=dict)


REGISTRY_FILENAME = "registry.json"


def get_registry_path(config_dir: str) -> Path:
    return Path(config_dir) / REGISTRY_FILENAME


def load_registry(config_dir: str) -> dict[str, Any]:
    path = get_registry_path(config_dir)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"agents": {}}


def save_registry(config_dir: str, data: dict[str, Any]) -> None:
    path = get_registry_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


class Agent:
    """Base class for a tool harness agent.

    Subclasses define how to detect credentials for a given tool
    (e.g. Claude Code, Codex) and how to swap its configuration
    so requests route through petsitter.
    """

    id: str = ""
    display_name: str = ""
    description: str = ""
    icon: str = ""
    required_env: list[str] = []
    config_paths: list[str] = []
    tricks: list[str] = []
    model_config: dict[str, Any] = {}

    def detect(self) -> AgentResult:
        """Scan the system and return what credentials were found."""
        found: dict[str, str] = {}
        missing: list[str] = []
        for key in self.required_env:
            val = os.environ.get(key)
            if val:
                found[key] = val
            else:
                missing.append(key)
        if missing:
            return AgentResult(
                status="missing_creds",
                found_env=found,
                missing_env=missing,
                message=f"Missing: {', '.join(missing)}",
            )
        return AgentResult(
            status="ready",
            found_env=found,
            message="All credentials found",
        )

    def register(self, ctx: AgentContext) -> list[dict[str, str]]:
        """Swap the tool's config to point through petsitter.

        Returns a list of log entries (each with ``level`` and ``message``).
        Subclasses should call ``_save_env_var`` and ``_patch_config_file``
        to track originals for later restoration.
        """
        raise NotImplementedError

    def unregister(self, ctx: AgentContext) -> list[dict[str, str]]:
        """Restore the tool's original config.

        Returns a list of log entries.
        """
        raise NotImplementedError

    # -- helpers for subclasses --

    @staticmethod
    def _save_env_var(backup: dict, key: str) -> str | None:
        """Save current env var into backup dict, return current value."""
        current = os.environ.get(key)
        backup.setdefault("env", {})[key] = current
        return current

    @staticmethod
    def _set_env_var(key: str, value: str) -> None:
        """Set an environment variable in the current process."""
        os.environ[key] = value

    @staticmethod
    def _restore_env_var(backup: dict, key: str) -> None:
        saved = backup.get("env", {}).get(key)
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved

    @staticmethod
    def _patch_config_file(
        backup: dict,
        file_path: str,
        patch_fn,
    ) -> bool:
        """Read a config file, apply a patch, write back.

        ``patch_fn`` receives the parsed content and must return the
        modified content.  The original content is saved in backup.
        Returns True on success.
        """
        path = Path(file_path).expanduser()
        if not path.exists():
            return False
        try:
            original = path.read_text()
            key = f"file::{file_path}"
            backup.setdefault("files", {})[key] = original
            modified = patch_fn(original)
            path.write_text(modified)
            return True
        except OSError:
            return False

    @staticmethod
    def _restore_config_file(backup: dict, file_path: str) -> bool:
        key = f"file::{file_path}"
        original = backup.get("files", {}).get(key)
        if original is None:
            return False
        try:
            Path(file_path).expanduser().write_text(original)
            return True
        except OSError:
            return False

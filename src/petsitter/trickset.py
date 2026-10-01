"""Trickset — a named, filterable set of tricks."""

import json
import logging
import uuid
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from petsitter.loader import load_trick_from_path
from petsitter.observability import TRICKSET_LOG_DIR
from petsitter.trick import Trick

logger = logging.getLogger("petsitter")

SCHEMA = "0.8.0"


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _default_logfile(name: str) -> str:
    return str(TRICKSET_LOG_DIR / f"{name}.log")


class Trickset:
    def __init__(
        self,
        name: str,
        schema: str,
        filters: dict[str, str],
        trick_paths: list[str],
        file_path: str | None = None,
        parameters: dict[str, Any] | None = None,
        models: dict[str, str] | None = None,
        trick_enabled: list[bool] | None = None,
        trick_ids: list[str] | None = None,
        trick_keywords: list[str | None] | None = None,
        logfile: str | None = None,
        loglevel: str = "INFO",
        trick_configs: dict[str, dict[str, Any]] | None = None,
    ):
        self.name = name
        self.schema = schema
        self.filters = filters
        self.trick_paths = list(trick_paths)
        self.trick_enabled = list(trick_enabled) if trick_enabled else [True] * len(trick_paths)
        if trick_ids:
            self.trick_ids = list(trick_ids)
        else:
            self.trick_ids = [_new_id() for _ in range(len(trick_paths))]
        self.trick_keywords = list(trick_keywords) if trick_keywords else [None] * len(trick_paths)
        while len(self.trick_keywords) < len(self.trick_paths):
            self.trick_keywords.append(None)
        self.trick_configs: dict[str, dict[str, Any]] = dict(trick_configs or {})
        self.file_path = file_path
        self.parameters: dict[str, Any] = parameters or {}
        self.models: dict[str, str] = models or {}
        self.tricks: list[Trick] = []
        # A channel is on unless someone turned it off. Off is saved in the
        # file, so it survives restarts; an off trickset isn't loaded at all.
        self.enabled: bool = True
        self.logfile = logfile if logfile else _default_logfile(name)
        self.loglevel = (loglevel or "INFO").upper()

    def _trick_entries(self) -> list[dict]:
        entries = []
        for i, path in enumerate(self.trick_paths):
            override = self.trick_keywords[i] if i < len(self.trick_keywords) else None
            trick = self.tricks[i] if i < len(self.tricks) else None
            effective = override or (getattr(trick, "prompt_keyword", "") or "")
            entry = {
                "id": self.trick_ids[i] if i < len(self.trick_ids) else _new_id(),
                "file": path,
                "enabled": self.trick_enabled[i] if i < len(self.trick_enabled) else True,
                "config": self.trick_configs.get(self.trick_ids[i] if i < len(self.trick_ids) else "", {}),
            }
            if effective:
                entry["keyword"] = effective
            entries.append(entry)
        return entries

    def matches(self, x_title: str, model: str, user_agent: str = "") -> bool:
        for key, pattern in self.filters.items():
            if key == "X-Title":
                val = x_title
            elif key == "User-Agent":
                val = user_agent
            else:
                val = model
            # Case-insensitive on every OS. Plain fnmatch() follows the
            # platform, so "opencode*" caught "OpenCode" on Windows only.
            if not fnmatchcase((val or "").lower(), (pattern or "").lower()):
                return False
        return True

    def load_tricks(self) -> None:
        self.tricks = []
        for i, path in enumerate(self.trick_paths):
            cls = load_trick_from_path(path)
            trick = cls()
            tid = self.trick_ids[i] if i < len(self.trick_ids) else _new_id()
            if tid in self.trick_configs:
                try:
                    trick.configure(self.trick_configs[tid])
                except Exception:
                    logger.exception("trickset '%s': failed to configure trick %s", self.name, path)
            self.tricks.append(trick)
            self.get_logger().info("trickset '%s': loaded trick %s", self.name, path)

    def get_logger(self) -> logging.Logger:
        """Return a Python logger writing this trickset's activity to its logfile.

        The returned logger also propagates to the root ``petsitter`` handlers,
        so records still reach the dashboard/console. Its own level is
        ``loglevel``; the file handler is (re)configured to follow the current
        ``logfile``/``loglevel`` values on each call.
        """
        level = getattr(logging, self.loglevel, logging.INFO)
        log = logging.getLogger(f"petsitter.trickset.{self.name}")
        log.setLevel(level)
        logfile = self.logfile or _default_logfile(self.name)
        handler = next((h for h in log.handlers if isinstance(h, logging.FileHandler)), None)
        if handler is None or Path(handler.baseFilename).resolve() != Path(logfile).resolve():
            if handler is not None:
                log.removeHandler(handler)
                handler.close()
            Path(logfile).parent.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(logfile)
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            log.addHandler(handler)
        handler.setLevel(level)
        return log

    def reread_config(self, data: dict | None = None) -> dict:
        """Re-apply configuration in place.

        Applies structural/config changes — filters, parameters, models,
        logging, and trick membership/enabled/keywords/config — without
        hot-reloading trick behavior: tricks already loaded keep their
        runtime state, only their config values are re-applied via
        ``configure()``. Tricks that are new (or whose file path changed)
        are instantiated fresh. Returns an ``action`` describing the result:

        - ``"reloaded"`` — config re-applied (from *data* or the JSON file)
        - ``"kept"``     — no file path and no inline data, left untouched
        - ``"removed"``  — file is gone or the trickset was turned off; caller
          should unload it
        """
        if data is None:
            if not self.file_path:
                return {"name": self.name, "action": "kept", "reason": "no file"}
            p = Path(self.file_path).resolve()
            if not p.exists():
                if self.name == "_default":
                    return {"name": self.name, "action": "kept", "reason": "default has no file yet"}
                return {"name": self.name, "action": "removed", "reason": "file gone"}
            source = str(p)
            data = json.loads(p.read_text())
        else:
            source = "inline"
        self.enabled = data.get("enabled", True) is not False
        if not self.enabled and self.name != "_default":
            return {"name": self.name, "action": "removed", "reason": "turned off"}
        self.filters = data.get("filters", {"X-Title": "*", "Model": "*"})
        self.parameters = data.get("parameters", {})
        self.models = data.get("models", {})
        self.logfile = data.get("logfile") or _default_logfile(self.name)
        self.loglevel = (data.get("loglevel") or "INFO").upper()

        desired: list[dict] = []
        for entry in data.get("tricks", []):
            if isinstance(entry, str):
                desired.append({
                    "id": _new_id(), "file": entry, "enabled": True,
                    "keyword": None, "config": {},
                })
            elif isinstance(entry, dict):
                desired.append({
                    "id": entry.get("id") or _new_id(),
                    "file": entry.get("file", ""),
                    "enabled": entry.get("enabled", True),
                    "keyword": entry.get("keyword"),
                    "config": entry.get("config") or {},
                })

        existing_idx = {tid: i for i, tid in enumerate(self.trick_ids)}
        desired_ids = {d["id"] for d in desired}
        removed_ids = [tid for tid in self.trick_ids if tid not in desired_ids]

        new_tricks: list[Trick] = []
        new_paths: list[str] = []
        new_enabled: list[bool] = []
        new_ids: list[str] = []
        new_keywords: list[str | None] = []
        new_configs: dict[str, dict] = dict(self.trick_configs)
        for tid in removed_ids:
            new_configs.pop(tid, None)

        for entry in desired:
            tid = entry["id"]
            if not entry["file"]:
                continue
            idx = existing_idx.get(tid)
            if idx is not None and idx < len(self.tricks) and self.tricks[idx] is not None:
                trick = self.tricks[idx]
                if self.trick_paths[idx] != entry["file"]:
                    try:
                        trick = load_trick_from_path(entry["file"])()
                    except Exception:
                        logger.exception("trickset '%s': failed to reload trick %s", self.name, entry["file"])
                        continue
            else:
                try:
                    trick = load_trick_from_path(entry["file"])()
                except Exception:
                    logger.exception("trickset '%s': failed to load trick %s", self.name, entry["file"])
                    continue
            if entry["config"]:
                try:
                    trick.configure(dict(entry["config"]))
                except Exception:
                    logger.exception("trickset '%s': failed to apply config to %s", self.name, tid)
            new_tricks.append(trick)
            new_paths.append(entry["file"])
            new_enabled.append(entry["enabled"])
            new_ids.append(tid)
            new_keywords.append(entry["keyword"])
            new_configs[tid] = dict(entry["config"])

        self.tricks = new_tricks
        self.trick_paths = new_paths
        self.trick_enabled = new_enabled
        self.trick_ids = new_ids
        self.trick_keywords = new_keywords
        self.trick_configs = new_configs
        self.get_logger().info("trickset '%s': reread config from %s", self.name, source)
        return {
            "name": self.name,
            "action": "reloaded",
            "tricks": len(new_tricks),
            "removed_tricks": len(removed_ids),
        }

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "schema": self.schema,
            "filters": dict(self.filters),
            "tricks": self._trick_entries(),
            "file_path": self.file_path,
            "parameters": dict(self.parameters),
            "models": dict(self.models),
            "logfile": self.logfile,
            "loglevel": self.loglevel,
        }

    def to_file_dict(self) -> dict:
        return {
            "schema": self.schema,
            "name": self.name,
            "enabled": self.enabled,
            "filters": dict(self.filters),
            "tricks": self._trick_entries(),
            "parameters": dict(self.parameters),
            "models": dict(self.models),
            "logfile": self.logfile,
            "loglevel": self.loglevel,
        }

    def save(self) -> None:
        if not self.file_path:
            raise ValueError(f"Trickset '{self.name}' has no file path")
        data = self.to_file_dict()
        Path(self.file_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.file_path).write_text(json.dumps(data, indent=2) + "\n")
        self.get_logger().info("trickset '%s': saved to %s", self.name, self.file_path)

    @staticmethod
    def load_from_file(path: str) -> "Trickset":
        p = Path(path).resolve()
        if not p.exists():
            raise FileNotFoundError(f"Trickset file not found: {path}")
        data = json.loads(p.read_text())
        return Trickset.from_dict(data, file_path=str(p))

    @classmethod
    def from_dict(cls, data: dict, file_path: str | None = None) -> "Trickset":
        """Build a Trickset from a JSON object (inline in config.json or a file).

        When *data* comes from a config file, *file_path* should be ``None`` so
        the trickset is treated as inline and never re-read from disk.
        """
        name = data.get("name") or (Path(file_path).stem if file_path else "")
        if not name:
            raise ValueError("Trickset has no name")
        schema = data.get("schema", "unknown")
        filters = data.get("filters", {"X-Title": "*", "Model": "*"})
        raw_tricks = data.get("tricks", [])
        trick_paths: list[str] = []
        trick_enabled: list[bool] = []
        trick_ids: list[str] = []
        trick_keywords: list[str | None] = []
        trick_configs: dict[str, dict[str, Any]] = {}
        for entry in raw_tricks:
            if isinstance(entry, str):
                trick_paths.append(entry)
                trick_enabled.append(True)
                trick_ids.append(_new_id())
                trick_keywords.append(None)
            elif isinstance(entry, dict):
                eid = entry.get("id") or _new_id()
                trick_paths.append(entry.get("file", ""))
                trick_enabled.append(entry.get("enabled", True))
                trick_ids.append(eid)
                trick_keywords.append(entry.get("keyword"))
                cfg = entry.get("config")
                if isinstance(cfg, dict) and cfg:
                    trick_configs[eid] = dict(cfg)
        parameters = data.get("parameters", {})
        models = data.get("models", {})
        logfile = data.get("logfile")
        loglevel = data.get("loglevel", "INFO")
        ts = cls(name, schema, filters, trick_paths, file_path=file_path, parameters=parameters, models=models, trick_enabled=trick_enabled, trick_ids=trick_ids, trick_keywords=trick_keywords, logfile=logfile, loglevel=loglevel, trick_configs=trick_configs)
        ts.enabled = data.get("enabled", True) is not False
        ts.load_tricks()
        ts.get_logger().info("trickset '%s': loaded %d tricks", name, len(ts.tricks))
        return ts

    @staticmethod
    def from_legacy_tricks(name: str, tricks: list[Trick], trick_paths: list[str], parameters: dict[str, Any] | None = None, models: dict[str, str] | None = None) -> "Trickset":
        ts = Trickset(
            name=name,
            schema=SCHEMA,
            filters={"X-Title": "*", "Model": "*"},
            trick_paths=trick_paths,
            parameters=parameters,
            models=models,
        )
        ts.tricks = list(tricks)
        ts.trick_enabled = [True] * len(tricks)
        ts.trick_ids = [_new_id() for _ in range(len(tricks))]
        return ts

    def merge_tricks(self, entries: list[dict]) -> bool:
        """Merge incoming trick entries by id. Returns True if anything changed."""
        changed = False
        for entry in entries:
            eid = entry.get("id", "")
            if not eid:
                continue
            for i, tid in enumerate(self.trick_ids):
                if tid != eid:
                    continue
                if "file" in entry and entry["file"] != self.trick_paths[i]:
                    self.trick_paths[i] = entry["file"]
                    changed = True
                if "enabled" in entry:
                    val = entry["enabled"]
                    while len(self.trick_enabled) <= i:
                        self.trick_enabled.append(True)
                    if self.trick_enabled[i] != val:
                        self.trick_enabled[i] = val
                        changed = True
                if "keyword" in entry:
                    val = entry["keyword"]
                    while len(self.trick_keywords) <= i:
                        self.trick_keywords.append(None)
                    if self.trick_keywords[i] != val:
                        self.trick_keywords[i] = val
                        changed = True
                if "config" in entry:
                    val = entry["config"] or {}
                    if self.trick_configs.get(eid) != val:
                        self.trick_configs[eid] = dict(val)
                        changed = True
                    if i < len(self.tricks):
                        try:
                            self.tricks[i].configure(dict(val))
                        except Exception:
                            logger.exception("trickset '%s': failed to apply config to %s", self.name, eid)
        return changed

    def add_trick(self, path: str, enabled: bool = True, keyword: str | None = None) -> Trick:
        cls = load_trick_from_path(path)
        trick = cls()
        self.tricks.append(trick)
        self.trick_paths.append(path)
        self.trick_enabled.append(enabled)
        self.trick_ids.append(_new_id())
        self.trick_keywords.append(keyword)
        self.get_logger().info("trickset '%s': added trick %s", self.name, path)
        return trick

    def remove_trick(self, trick_id: str) -> bool:
        for i, tid in enumerate(self.trick_ids):
            if tid == trick_id:
                del self.tricks[i]
                del self.trick_paths[i]
                del self.trick_ids[i]
                if i < len(self.trick_enabled):
                    del self.trick_enabled[i]
                if i < len(self.trick_keywords):
                    del self.trick_keywords[i]
                self.trick_configs.pop(trick_id, None)
                self.get_logger().info("trickset '%s': removed trick %s", self.name, tid)
                return True
        return False

    def reorder_trick(self, trick_id: str, new_index: int) -> bool:
        for i, tid in enumerate(self.trick_ids):
            if tid == trick_id:
                t = self.tricks.pop(i)
                tp = self.trick_paths.pop(i)
                tid2 = self.trick_ids.pop(i)
                te = self.trick_enabled.pop(i) if i < len(self.trick_enabled) else True
                tk = self.trick_keywords.pop(i) if i < len(self.trick_keywords) else None
                new_index = max(0, min(new_index, len(self.tricks)))
                self.tricks.insert(new_index, t)
                self.trick_paths.insert(new_index, tp)
                self.trick_ids.insert(new_index, tid2)
                self.trick_enabled.insert(new_index, te)
                self.trick_keywords.insert(new_index, tk)
                return True
        return False

    def find_trick_id_by_class(self, class_name: str) -> str | None:
        for i, t in enumerate(self.tricks):
            if type(t).__name__ == class_name:
                return self.trick_ids[i] if i < len(self.trick_ids) else None
        return None
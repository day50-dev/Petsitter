"""Trickset — a named, filterable set of tricks."""

import json
import logging
import uuid
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from src.loader import load_trick_from_path
from src.trick import Trick

logger = logging.getLogger("petsitter")

SCHEMA = "0.7.0"


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


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
        self.file_path = file_path
        self.parameters: dict[str, Any] = parameters or {}
        self.models: dict[str, str] = models or {}
        self.tricks: list[Trick] = []

    def _trick_entries(self) -> list[dict]:
        return [
            {
                "id": self.trick_ids[i] if i < len(self.trick_ids) else _new_id(),
                "file": path,
                "enabled": self.trick_enabled[i] if i < len(self.trick_enabled) else True,
                "keyword": self.trick_keywords[i] if i < len(self.trick_keywords) else None,
            }
            for i, path in enumerate(self.trick_paths)
        ]

    def matches(self, x_title: str, model: str) -> bool:
        for key, pattern in self.filters.items():
            val = x_title if key == "X-Title" else model
            if not fnmatch(val, pattern):
                return False
        return True

    def load_tricks(self) -> None:
        self.tricks = []
        for path in self.trick_paths:
            cls = load_trick_from_path(path)
            self.tricks.append(cls())
            logger.info("Trickset %s: loaded trick %s", self.name, path)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "schema": self.schema,
            "filters": dict(self.filters),
            "tricks": self._trick_entries(),
            "file_path": self.file_path,
            "parameters": dict(self.parameters),
            "models": dict(self.models),
        }

    def to_file_dict(self) -> dict:
        return {
            "schema": self.schema,
            "name": self.name,
            "filters": dict(self.filters),
            "tricks": self._trick_entries(),
            "parameters": dict(self.parameters),
            "models": dict(self.models),
        }

    def save(self) -> None:
        if not self.file_path:
            raise ValueError(f"Trickset '{self.name}' has no file path")
        data = self.to_file_dict()
        Path(self.file_path).write_text(json.dumps(data, indent=2) + "\n")
        logger.info("Saved trickset %s to %s", self.name, self.file_path)

    @staticmethod
    def load_from_file(path: str) -> "Trickset":
        p = Path(path).resolve()
        if not p.exists():
            raise FileNotFoundError(f"Trickset file not found: {path}")
        data = json.loads(p.read_text())
        name = data.get("name", p.stem)
        schema = data.get("schema", "unknown")
        filters = data.get("filters", {"X-Title": "*", "Model": "*"})
        raw_tricks = data.get("tricks", [])
        trick_paths: list[str] = []
        trick_enabled: list[bool] = []
        trick_ids: list[str] = []
        trick_keywords: list[str | None] = []
        for entry in raw_tricks:
            if isinstance(entry, str):
                trick_paths.append(entry)
                trick_enabled.append(True)
                trick_ids.append(_new_id())
                trick_keywords.append(None)
            elif isinstance(entry, dict):
                trick_paths.append(entry.get("file", ""))
                trick_enabled.append(entry.get("enabled", True))
                trick_ids.append(entry.get("id") or _new_id())
                trick_keywords.append(entry.get("keyword"))
        parameters = data.get("parameters", {})
        models = data.get("models", {})
        ts = Trickset(name, schema, filters, trick_paths, file_path=str(p), parameters=parameters, models=models, trick_enabled=trick_enabled, trick_ids=trick_ids, trick_keywords=trick_keywords)
        ts.load_tricks()
        logger.info("Loaded trickset: %s (%d tricks)", name, len(ts.tricks))
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
        return changed

    def add_trick(self, path: str, enabled: bool = True, keyword: str | None = None) -> Trick:
        cls = load_trick_from_path(path)
        trick = cls()
        self.tricks.append(trick)
        self.trick_paths.append(path)
        self.trick_enabled.append(enabled)
        self.trick_ids.append(_new_id())
        self.trick_keywords.append(keyword)
        logger.info("Trickset %s: added trick %s", self.name, path)
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
                logger.info("Trickset %s: removed trick %s", self.name, tid)
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
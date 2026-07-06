"""Trickset — a named, filterable set of tricks."""

import json
import logging
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from src.loader import load_trick_from_path
from src.trick import Trick

logger = logging.getLogger("petsitter")

SCHEMA = "0.5.0"


class Trickset:
    def __init__(
        self,
        name: str,
        schema: str,
        filters: dict[str, str],
        trick_paths: list[str],
        file_path: str | None = None,
        parameters: dict[str, Any] | None = None,
    ):
        self.name = name
        self.schema = schema
        self.filters = filters
        self.trick_paths = list(trick_paths)
        self.file_path = file_path
        self.parameters: dict[str, Any] = parameters or {}
        self.tricks: list[Trick] = []

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
            "tricks": list(self.trick_paths),
            "file_path": self.file_path,
            "parameters": dict(self.parameters),
        }

    def to_file_dict(self) -> dict:
        return {
            "schema": self.schema,
            "name": self.name,
            "filters": dict(self.filters),
            "tricks": list(self.trick_paths),
            "parameters": dict(self.parameters),
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
        trick_paths = data.get("tricks", [])
        ts = Trickset(name, schema, filters, trick_paths, file_path=str(p))
        ts.load_tricks()
        logger.info("Loaded trickset: %s (%d tricks)", name, len(ts.tricks))
        return ts

    @staticmethod
    def from_legacy_tricks(name: str, tricks: list[Trick], trick_paths: list[str]) -> "Trickset":
        ts = Trickset(
            name=name,
            schema=SCHEMA,
            filters={"X-Title": "*", "Model": "*"},
            trick_paths=trick_paths,
        )
        ts.tricks = list(tricks)
        return ts

    def add_trick(self, path: str) -> Trick:
        cls = load_trick_from_path(path)
        trick = cls()
        self.tricks.append(trick)
        self.trick_paths.append(path)
        logger.info("Trickset %s: added trick %s", self.name, path)
        return trick

    def remove_trick(self, class_name: str) -> bool:
        for i, trick in enumerate(self.tricks):
            if type(trick).__name__ == class_name:
                del self.tricks[i]
                del self.trick_paths[i]
                logger.info("Trickset %s: removed trick %s", self.name, class_name)
                return True
        return False

    def reorder_trick(self, class_name: str, new_index: int) -> bool:
        for i, trick in enumerate(self.tricks):
            if type(trick).__name__ == class_name:
                t = self.tricks.pop(i)
                tp = self.trick_paths.pop(i)
                new_index = max(0, min(new_index, len(self.tricks)))
                self.tricks.insert(new_index, t)
                self.trick_paths.insert(new_index, tp)
                return True
        return False

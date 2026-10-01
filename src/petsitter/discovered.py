"""Programs petsitter has seen, kept across restarts.

Channels match on the X-Title header a program sends (and on the model), so
the useful question when a channel doesn't catch what you expected is "what
does this program actually send, and where did its requests go?". This keeps
that record: every X-Title value seen (including none at all), the models it
used, and which channel each request landed in. The dashboard offers these as
the values to choose from when making a channel, and lists them under
Discovered programs for troubleshooting.

Saved to ``discovered.json`` in the config directory. A store with no path
stays in memory (tests, or a handler built without a config directory).
"""

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("petsitter")


class DiscoveredPrograms:
    KEEP = 200            # programs remembered; the least recently seen go first
    SAVE_EVERY = 10.0     # seconds between saves when nothing new appeared

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._saved_at = 0.0
        self._dirty = False
        self.programs: dict[str, dict[str, Any]] = {}
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                for entry in data.get("programs", []):
                    if isinstance(entry, dict) and isinstance(entry.get("x_title"), str):
                        self.programs[entry["x_title"]] = entry
            except (OSError, ValueError) as e:
                logger.warning("couldn't read %s, starting fresh: %s", self.path, e)

    def note(self, x_title: str, model: str, channels: list[str]) -> None:
        """Record one request. Cheap; saves only when something is new or due."""
        now = time.time()
        x_title = x_title or ""
        with self._lock:
            entry = self.programs.get(x_title)
            fresh = entry is None
            if fresh:
                entry = {"x_title": x_title, "count": 0, "first": now, "last": now,
                         "models": {}, "channels": {}}
                self.programs[x_title] = entry
            entry["count"] += 1
            entry["last"] = now
            if model:
                fresh = fresh or model not in entry["models"]
                entry["models"][model] = entry["models"].get(model, 0) + 1
            for ch in channels:
                # A program landing somewhere new is exactly what you'd want
                # to see after a restart, so it's saved right away too.
                fresh = fresh or ch not in entry["channels"]
                entry["channels"][ch] = entry["channels"].get(ch, 0) + 1
            if len(self.programs) > self.KEEP:
                for stale in sorted(self.programs, key=lambda k: self.programs[k]["last"])[:len(self.programs) - self.KEEP]:
                    del self.programs[stale]
            self._dirty = True
            due = fresh or now - self._saved_at >= self.SAVE_EVERY
        if due:
            self.save()

    def forget(self, x_title: str) -> bool:
        with self._lock:
            gone = self.programs.pop(x_title, None) is not None
            self._dirty = self._dirty or gone
        if gone:
            self.save()
        return gone

    def snapshot(self) -> list[dict[str, Any]]:
        """Every program, most recently seen first, with 'ago' in seconds."""
        now = time.time()
        with self._lock:
            items = [dict(e, models=dict(e["models"]), channels=dict(e["channels"]))
                     for e in self.programs.values()]
        for e in items:
            e["ago"] = round(now - e["last"])
        return sorted(items, key=lambda e: -e["last"])

    def save(self) -> None:
        if not self.path:
            return
        with self._lock:
            if not self._dirty:
                return
            data = {"programs": list(self.programs.values())}
            self._dirty = False
            self._saved_at = time.time()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as e:
            logger.warning("couldn't save %s: %s", self.path, e)

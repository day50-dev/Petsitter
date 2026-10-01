"""Programs petsitter has seen, kept across restarts.

Channels match on the X-Title header a program sends, its User-Agent, and the
model, so
the useful question when a channel doesn't catch what you expected is "what
does this program actually send, and where did its requests go?". This keeps
that record: every program seen, the X-Title and User-Agent it sends, the
models it used, and which channel each request landed in. A program is
identified by its X-Title, or by its User-Agent when it sends no X-Title (many
tools don't), so two programs without one don't merge into a single row. The dashboard offers these as
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

# Headers whose values are credentials. The sample keeps the header (its being
# there is often the clue) but never the value, on screen or on disk.
_SECRET_HEADER = ("authorization", "proxy-authorization", "cookie", "set-cookie")
_SECRET_WORDS = ("key", "token", "secret", "password", "auth", "session")
SAMPLE_MAX_HEADERS = 60
SAMPLE_MAX_VALUE = 300


def sample_headers(pairs) -> dict[str, str]:
    """A request's headers, credentials masked, sized for display and disk."""
    out: dict[str, str] = {}
    for name, value in list(pairs or ())[:SAMPLE_MAX_HEADERS]:
        n = str(name).lower()
        v = str(value)
        if n in _SECRET_HEADER or any(w in n for w in _SECRET_WORDS):
            scheme = v.split(" ", 1)[0] if " " in v and len(v.split(" ", 1)[0]) < 12 else ""
            v = (scheme + " " if scheme else "") + "\u2022\u2022\u2022\u2022"
        elif len(v) > SAMPLE_MAX_VALUE:
            v = v[:SAMPLE_MAX_VALUE] + "\u2026"
        out[n] = v
    return out


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
                        entry.setdefault("user_agents", {})
                        entry.setdefault("key", self.key_for(entry["x_title"], next(iter(entry["user_agents"]), "")))
                        self.programs[entry["key"]] = entry
            except (OSError, ValueError) as e:
                logger.warning("couldn't read %s, starting fresh: %s", self.path, e)

    @staticmethod
    def key_for(x_title: str, user_agent: str) -> str:
        """X-Title when sent; otherwise the User-Agent's product name, so
        goose/1.4.0 and goose/1.5.2 are one program (versions are kept in
        its user_agents)."""
        if x_title:
            return x_title
        product = (user_agent or "").split("/")[0].split(" ")[0].strip()
        return "ua:" + product

    def note(self, x_title: str, model: str, channels: list[str], user_agent: str = "",
             headers=None) -> None:
        """Record one request. Cheap; saves only when something is new or due."""
        now = time.time()
        x_title = x_title or ""
        user_agent = (user_agent or "")[:300]
        key = self.key_for(x_title, user_agent)
        with self._lock:
            entry = self.programs.get(key)
            fresh = entry is None
            if fresh:
                entry = {"key": key, "x_title": x_title, "count": 0, "first": now, "last": now,
                         "user_agents": {}, "models": {}, "channels": {}}
                self.programs[key] = entry
            entry["count"] += 1
            entry["last"] = now
            if headers:
                # The most recent request's headers, for "what does it send?"
                entry["sample_headers"] = sample_headers(headers)
            if user_agent:
                uas = entry.setdefault("user_agents", {})
                fresh = fresh or user_agent not in uas
                uas[user_agent] = uas.get(user_agent, 0) + 1
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

    def forget(self, key: str) -> bool:
        with self._lock:
            gone = self.programs.pop(key, None) is not None
            self._dirty = self._dirty or gone
        if gone:
            self.save()
        return gone

    def snapshot(self) -> list[dict[str, Any]]:
        """Every program, most recently seen first, with 'ago' in seconds."""
        now = time.time()
        with self._lock:
            items = [dict(e, models=dict(e["models"]), channels=dict(e["channels"]),
                          user_agents=dict(e.get("user_agents") or {}))
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

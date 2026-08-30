"""Traffic logger trick.

Appends one JSON line per request to a JSONL file so you can inspect every
message going out to the model and every response coming back while debugging
a misbehaving harness.

Each request produces two records:

    {"timestamp": ..., "event": "request",  ...}   # what goes out (pre_hook)
    {"timestamp": ..., "event": "response", ...}   # what comes back (post_hook)

Hooks run in trickset order, so where this trick sits matters:

* first in the trickset — sees the messages *as they arrive* from the client
* last in the trickset — sees them after every other trick has rewritten them

A trick that short-circuits the pipeline (a prompt-keyword handler, or a
post_hook that replaces the answer) prevents anything after it from running,
so put the logger before those tricks to guarantee a record.

The ``path`` config field sets where the JSONL file is written.  If the path
is an existing directory (or ends with a slash) the file ``traffic.jsonl`` is
written inside it.
"""

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from petsitter.observability import LOG_DIR, request_meta
from petsitter.trick import Trick

DEFAULT_LOGGER_PATH = LOG_DIR / "traffic.jsonl"


def _json_default(value) -> str:
    """Fallback for anything json.dumps can't serialize natively."""
    return str(value)


class LoggerTrick(Trick):
    """Appends timestamped JSONL records of request/response traffic to a file."""

    __brief__ = "Writes every request/response as timestamped JSONL to a file"
    __display_name__ = "Traffic Logger"
    config_fields = [
        {
            "key": "path",
            "label": "JSONL log file",
            "description": (
                "Where to append one JSON line per request (and per response). "
                "A directory, or a path ending in '/', writes traffic.jsonl "
                "inside it. Blank uses ~/.cache/petsitter/traffic.jsonl."
            ),
            "type": "path",
            "default": str(DEFAULT_LOGGER_PATH),
        },
    ]

    def __init__(self, path: str = ""):
        self.path = path or str(DEFAULT_LOGGER_PATH)

    def configure(self, config: dict) -> None:
        super().configure(config)
        if not self.path:
            self.path = str(DEFAULT_LOGGER_PATH)

    # -- hooking -------------------------------------------------------------

    def pre_hook(self, context: list, params: dict) -> list:
        """Record the outbound request: full payload and message list."""
        meta = request_meta()
        tools = meta.get("tools") or (params or {}).get("tools") or []
        self._append({
            "timestamp": _now(),
            "trick": type(self).__name__,
            "event": "request",
            "direction": "out",
            "request_id": meta.get("request_id", ""),
            "x_title": meta.get("x_title", ""),
            "model": meta.get("model", (params or {}).get("model", "")),
            "stream": meta.get("stream", (params or {}).get("stream", False)),
            "tools": tools,
            "payload": params or {},
            "messages": context,
        })
        return context

    def post_hook(self, context: list) -> list:
        """Record the inbound response: the message list including the answer."""
        if not context:
            return context
        meta = request_meta()
        self._append({
            "timestamp": _now(),
            "trick": type(self).__name__,
            "event": "response",
            "direction": "in",
            "request_id": meta.get("request_id", ""),
            "x_title": meta.get("x_title", ""),
            "model": meta.get("model", ""),
            "messages": context,
            "answer": context[-1],
        })
        return context

    # -- helpers -------------------------------------------------------------

    def _log_path(self) -> Path:
        path = Path(self.path or str(DEFAULT_LOGGER_PATH)).expanduser()
        if path.is_dir() or str(self.path or "").endswith(("/", os.sep)) or not path.suffix:
            path = path / "traffic.jsonl"
        return path

    def _append(self, record: dict) -> None:
        path = self._log_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(record, default=_json_default) + "\n"
        except Exception:
            return
        try:
            with _LOCK:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line)
        except OSError:
            pass


_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
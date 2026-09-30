"""Records every request sent to the model and every response that comes back, one JSON line each, so you can see exactly what your AI tool is doing.

When an agent misbehaves ("why did it forget my instructions?", "what tools did it
actually send?"), the answer is usually in the raw traffic, which your tool
normally hides. Turn this on, reproduce the problem, and read the file: full
payloads, tool lists and messages going out, and the model's answer coming back.
It never changes what the model sees.

## How to use

By default it writes `~/.cache/petsitter/traffic.jsonl`; set the `path` setting to
change it. Each request produces two lines:

```
{"timestamp": "...", "event": "request",  "direction": "out", "request_id": "ab12cd34", "model": "qwen3:8b", "payload": {...}, "messages": [...]}
{"timestamp": "...", "event": "response", "direction": "in",  "request_id": "ab12cd34", "messages": [...], "answer": {"role": "assistant", "content": "hi!"}}
```

Where it sits in the trickset matters. First, it records messages as they arrive
from your tool; last, it records them after every other trick has rewritten them.
A trick that answers on its own (a prompt keyword like `(exportit)`) stops
everything after it, so put the logger first to be sure of a record.

## How it works

- `pre_hook` writes the `request` record (payload, tools, messages, plus
  `request_id`, `x_title`, `model`, `stream` from the request metadata);
  `post_hook` writes the `response` record. Both return the context untouched.
- If `path` is a directory, ends in `/`, or has no file extension, `traffic.jsonl`
  is written inside it. Parent directories are created on demand.
- Appends are serialized with a module-level lock. Unserializable values are
  written via `str()`; write errors are swallowed so logging never breaks a
  request.
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
    __category__ = "Diagnostics"
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
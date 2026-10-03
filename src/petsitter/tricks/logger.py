"""Records every request and reply in two JSONL files, before and after the extensions transform them, so you can see exactly what your AI tool sent and what changed.

When an agent misbehaves ("why did it forget my instructions?", "what tools did it
actually send?"), the answer is usually in the raw traffic, which your tool
normally hides. Turn this on, reproduce the problem, and read the file: full
payloads, tool lists and messages going out, and the model's answer coming back.
It never changes what the model sees.

## How to use

It writes two files in `~/.cache/petsitter/traffic/` (change the folder with the
`path` setting), one line per request in each:

- `before.jsonl`: the request before the extensions after the logger transform it
- `after.jsonl`: the conversation after they did, with the model's reply

Put it first in the channel and `before.jsonl` is exactly what your tool sent.

```
before.jsonl  {"timestamp": "...", "event": "request",  "stage": "before", "request_id": "ab12cd34", "model": "qwen3:8b", "payload": {...}, "messages": [...]}
after.jsonl   {"timestamp": "...", "event": "response", "stage": "after",  "request_id": "ab12cd34", "messages": [...], "answer": {"role": "assistant", "content": "hi!"}}
```

Its **Live** tab shows the last 40 requests without the payloads: the request
line, where it came from, the HTTP headers, and the parameters it asked for (stream, model,
temperature, max_tokens, tool_choice...), the tools offered, how many messages
and how big, and what came back. The files have everything else.

`request_id` ties the two together, so each file can be read on its own or the
two joined, which shows exactly what the extensions changed:

```
jq -c '{request_id, last: .messages[-1].content}' before.jsonl
jq -s 'group_by(.request_id)' before.jsonl after.jsonl
```

Where it sits in the trickset matters. First, it records messages as they arrive
from your tool; last, it records them after every other trick has rewritten them.
A trick that answers on its own (a prompt keyword like `(exportit)`) stops
everything after it, so put the logger first to be sure of a record.

## How it works

- `pre_hook` writes the `request` record (payload, tools, messages, plus
  `x_title`, `model`, `stream` from the request metadata);
  `post_hook` writes the `response` record. Both return the context untouched.
- `request_id` is the ID petsitter gives a request the moment it arrives and
  keeps until its reply goes back (`Trick.request_id`). Every line about one
  request carries the same one, so with more than one logger in a channel
  (one first, one last, to see what the tricks in between changed), or
  alongside petsitter's own log, lines can be lined up by it.
- `path` is a folder. If it names a `.jsonl` file instead (an older setting),
  the two files go beside it: `traffic.jsonl` becomes `traffic.before.jsonl`
  and `traffic.after.jsonl`. Folders are created on demand.
- Appends are serialized with a module-level lock. Unserializable values are
  written via `str()`; write errors are swallowed so logging never breaks a
  request.
- The Live tab's summaries are kept in memory (the last 40), and a restart
  drops them.
"""

import json
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from petsitter.observability import (LOG_DIR, current_client_addr, current_request_headers,
                                     current_request_line, current_trickset, current_user_agent,
                                     request_meta)
from petsitter.trick import Trick

DEFAULT_LOGGER_PATH = LOG_DIR / "traffic"
KEEP = 40                      # requests on the Live tab
def _size(messages) -> int:
    return len(json.dumps(messages, default=str)) if messages else 0


def _json_default(value) -> str:
    """Fallback for anything json.dumps can't serialize natively."""
    return str(value)


class LoggerTrick(Trick):
    """Appends timestamped JSONL records to before.jsonl (requests) and after.jsonl (transformed, with the reply)."""

    __brief__ = "Writes every request as JSONL, before and after the extensions transform it"
    __display_name__ = "Traffic Logger"
    __category__ = "Diagnostics"
    needs_window = 0   # only looks at replies, so they can stream
    ui_page = "logger.html"
    config_fields = [
        {
            "key": "path",
            "label": "Log folder",
            "description": (
                "Where to write before.jsonl (requests, before the extensions "
                "after the logger change them) and after.jsonl (the conversation "
                "after they did, with the reply). Blank uses ~/.cache/petsitter/traffic/."
            ),
            "type": "path",
            "default": str(DEFAULT_LOGGER_PATH),
        },
    ]

    def __init__(self, path: str = ""):
        self.path = path or str(DEFAULT_LOGGER_PATH)
        self._lock = threading.Lock()
        self._recent: deque = deque(maxlen=KEEP)   # summaries for the Live tab, newest last

    def configure(self, config: dict) -> None:
        super().configure(config)
        if not self.path:
            self.path = str(DEFAULT_LOGGER_PATH)

    # -- hooking -------------------------------------------------------------

    def pre_hook(self, context: list, params: dict) -> list:
        """Record the request in before.jsonl: full payload and message list."""
        meta = request_meta()
        tools = meta.get("tools") or (params or {}).get("tools") or []
        self._append("before", {
            "timestamp": _now(),
            "trick": type(self).__name__,
            "event": "request",
            "stage": "before",
            "request_id": self.request_id,
            "x_title": meta.get("x_title", ""),
            "model": meta.get("model", (params or {}).get("model", "")),
            "stream": meta.get("stream", (params or {}).get("stream", False)),
            "tools": tools,
            "payload": params or {},
            "messages": context,
        })
        self._remember(context, params or {}, meta, tools)
        return context

    def post_hook(self, context: list) -> list:
        """Record the transformed conversation and the reply in after.jsonl."""
        if not context:
            return context
        meta = request_meta()
        self._append("after", {
            "timestamp": _now(),
            "trick": type(self).__name__,
            "event": "response",
            "stage": "after",
            "request_id": self.request_id,
            "x_title": meta.get("x_title", ""),
            "model": meta.get("model", ""),
            "messages": context,
            "answer": context[-1],
        })
        self._remember_reply(context[-1])
        return context

    # -- the Live tab --------------------------------------------------------

    def _remember(self, context: list, params: dict, meta: dict, tools: list) -> None:
        try:
            ts = current_trickset()
            raw = meta.get("request_params")
            shown = raw if isinstance(raw, dict) else {k: v for k, v in params.items() if k not in ("messages", "tools")}
            ua = current_user_agent()
            entry = {
                "id": self.request_id, "at": time.time(),
                "line": current_request_line(), "from": current_client_addr(),
                "program": meta.get("x_title") or (ua.split("/")[0].split(" ")[0] if ua else ""),
                "api": meta.get("api") or "openai",
                "channel": ("Default" if ts.name == "_default" else ts.name) if ts is not None else "",
                "model": meta.get("model") or params.get("model", ""),
                "stream": bool(meta.get("stream", params.get("stream", False))),
                "headers": [[k, v] for k, v in current_request_headers()],
                "params": dict(shown),
                "tools": [(t.get("function") or {}).get("name") or t.get("name", "") for t in tools if isinstance(t, dict)],
                "messages": len(context), "chars": _size(context),
            }
        except Exception:
            return
        with self._lock:
            self._recent.append(entry)
        self.publish({"event": "traffic", "id": entry["id"]})

    def _remember_reply(self, reply) -> None:
        if not isinstance(reply, dict):
            return
        with self._lock:
            entry = next((e for e in reversed(self._recent) if e["id"] == self.request_id), None)
            if entry is None:
                return
            entry["reply"] = {
                "ms": round((time.time() - entry["at"]) * 1000),
                "chars": len(reply.get("content") or "") if isinstance(reply.get("content"), str) else _size(reply.get("content")),
                "tool_calls": [(c.get("function") or {}).get("name", "") for c in reply.get("tool_calls") or []
                               if isinstance(c, dict)],
            }
        self.publish({"event": "traffic", "id": self.request_id})

    def ui_action(self, data):
        action = (data or {}).get("action") if isinstance(data, dict) else None
        if action == "requests":
            with self._lock:
                return {"requests": list(reversed(self._recent)), "folder": str(self._log_path("before").parent)}
        if action == "clear":
            with self._lock:
                self._recent.clear()
            self.live_feed.clear()
            return {"ok": True}
        return super().ui_action(data)

    # -- helpers -------------------------------------------------------------

    def _log_path(self, stage: str) -> Path:
        """The file for "before" (requests) or "after" (the transformed
        conversation and the reply)."""
        name = "before" if stage == "before" else "after"
        path = Path(self.path or str(DEFAULT_LOGGER_PATH)).expanduser()
        if path.suffix == ".jsonl" and not path.is_dir():
            return path.with_name(f"{path.stem}.{name}.jsonl")
        return path / f"{name}.jsonl"

    def _append(self, stage: str, record: dict) -> bool:
        path = self._log_path(stage)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(record, default=_json_default) + "\n"
        except Exception:
            return False
        try:
            with _LOCK:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line)
        except OSError as e:
            self.report(f"Couldn't write to {path}: {e}")
            return False
        return True


_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
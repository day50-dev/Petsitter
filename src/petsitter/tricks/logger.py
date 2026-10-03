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

Its **Live** tab shows the last 40 requests and responses with everything but
the content: the request line as it arrived, where it came from, every header,
every parameter (stream, model, temperature, max_tokens, tool_choice...), the
tools' names; then each call petsitter made to the provider: its URL, status,
timing, the headers sent and received, and the response without its content
(id, model, finish or stop reason, usage, or the error). Each header block has
a copy button. The files have the content.

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
- The Live tab reads `get_raw()`, petsitter's record of the raw HTTP at both
  edges, and hears about each provider call as it finishes (a `raw_upstream`
  pipeline event), so a failed call shows up too. Its summaries are kept in
  memory (the last 40), and a restart drops them.
"""

import json
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from petsitter.observability import (LOG_DIR, current_client_addr, current_request_headers,
                                     current_trickset, current_user_agent, request_meta, subscribe,
                                     unsubscribe)
from petsitter.trick import Trick, get_raw

DEFAULT_LOGGER_PATH = LOG_DIR / "traffic"
KEEP = 40                      # requests on the Live tab
# The content of a request: the log files have it, the Live tab doesn't.
_CONTENT = ("messages", "system", "input", "prompt", "contents", "tools")


def _json(body: bytes):
    try:
        return json.loads(body.decode("utf-8", "replace")) if body else None
    except ValueError:
        return None


def _response_meta(body: bytes):
    """Everything in a provider's response but the content: id, model, usage,
    finish or stop reason, an error. Streams are read event by event."""
    obj = _json(body)
    if isinstance(obj, dict):
        if isinstance(obj.get("choices"), list):        # a chat completion
            return {**{k: v for k, v in obj.items() if k != "choices"},
                    "choices": [{k: v for k, v in c.items() if k not in ("message", "delta")}
                                for c in obj["choices"] if isinstance(c, dict)]}
        if obj.get("type") == "message":                # an Anthropic message
            return {k: v for k, v in obj.items() if k != "content"}
        return obj                                      # an error, or something else: all of it
    text = body.decode("utf-8", "replace")
    if "data:" not in text:
        return {"bytes": len(body)} if body else {}
    out: dict = {"events": 0}
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            continue
        try:
            ev = json.loads(data)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        out["events"] += 1
        if isinstance(ev.get("choices"), list):         # OpenAI chunks
            for k, v in ev.items():
                if k not in ("choices", "usage") and k not in out:
                    out[k] = v
            for c in ev["choices"]:
                if isinstance(c, dict) and c.get("finish_reason"):
                    out["finish_reason"] = c["finish_reason"]
            if ev.get("usage"):
                out["usage"] = ev["usage"]
        elif ev.get("type") == "message_start":         # Anthropic events
            out.update({k: v for k, v in (ev.get("message") or {}).items() if k != "content"})
        elif ev.get("type") == "message_delta":
            out.update(ev.get("delta") or {})
            if ev.get("usage"):
                out["usage"] = {**(out.get("usage") or {}), **ev["usage"]}
        elif ev.get("type") == "error" or "error" in ev:
            out["error"] = ev.get("error", ev)
    return out


def _exchange(ex: dict) -> dict:
    """One call to the provider, for the Live tab: everything but the content."""
    return {"method": ex.get("method"), "url": ex.get("url"), "status": ex.get("status"),
            "ms": ex.get("ms"), "first_byte_ms": ex.get("first_byte_ms"), "error": ex.get("error"),
            "request_headers": ex.get("request_headers") or [], "headers": ex.get("headers") or [],
            "response": _response_meta(ex.get("body") or b"")}


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

    def startup(self) -> None:
        # Hear about each call to the provider as it finishes, failed ones
        # included (no post_hook runs for those).
        subscribe(self._on_pipeline_event)

    def shutdown(self) -> None:
        unsubscribe(self._on_pipeline_event)

    def _on_pipeline_event(self, event: dict) -> None:
        if event.get("stage") != "raw_upstream":
            return
        raw = get_raw()
        if raw is None:
            return
        upstream = [_exchange(ex) for ex in raw["upstream"] if ex.get("done")]
        with self._lock:
            entry = next((e for e in reversed(self._recent) if e["id"] == event.get("request_id")), None)
            if entry is None:
                return
            entry["upstream"] = upstream
        self.publish({"event": "traffic", "id": entry["id"]})

    def _remember(self, context: list, params: dict, meta: dict, tools: list) -> None:
        try:
            ts = current_trickset()
            raw = get_raw()
            body = _json(raw["request"]["body"]) if raw else None
            if isinstance(body, dict):
                shown = {k: v for k, v in body.items() if k not in _CONTENT}
                tools = body.get("tools") or tools
            else:
                shown = {k: v for k, v in params.items() if k not in _CONTENT}
            ua = current_user_agent()
            entry = {
                "id": self.request_id, "at": time.time(),
                "line": raw["request"]["line"] if raw else "", "from": current_client_addr(),
                "program": meta.get("x_title") or (ua.split("/")[0].split(" ")[0] if ua else ""),
                "api": meta.get("api") or "openai",
                "channel": ("Default" if ts.name == "_default" else ts.name) if ts is not None else "",
                "model": meta.get("model") or params.get("model", ""),
                "stream": bool(meta.get("stream", params.get("stream", False))),
                "headers": raw["request"]["headers"] if raw else [[k, v] for k, v in current_request_headers()],
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
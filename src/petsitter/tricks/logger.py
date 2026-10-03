"""Records every request and reply in two JSONL files, before and after the extensions transform them, so you can see exactly what your AI tool sent and what changed.

When an agent misbehaves ("why did it forget my instructions?", "what tools did it
actually send?"), the answer is usually in the raw traffic, which your tool
normally hides. Turn this on, reproduce the problem, and read the file: full
payloads, tool lists and messages going out, and the model's answer coming back.
It never changes what the model sees.

## How to use

It writes three files in `~/.cache/petsitter/traffic/` (change the folder with the
`path` setting), one line per request in each:

- `before.jsonl`: the request before the extensions after the logger transform it
- `after.jsonl`: the conversation after they did, with the model's reply. Only
  for replies that finished: one that was cut short never gets here.
- `http.jsonl`: every exchange however it ended, written when it's over: the
  HTTP as the Live tab shows it, how each side's response ended, the verdict,
  and both response bodies whole (the provider's stream and petsitter's, event
  by event). This is the one to read when a reply went wrong.

Put it first in the channel and `before.jsonl` is exactly what your tool sent.

```
before.jsonl  {"timestamp": "...", "event": "request",  "stage": "before", "request_id": "ab12cd34", "model": "qwen3:8b", "payload": {...}, "messages": [...]}
after.jsonl   {"timestamp": "...", "event": "response", "stage": "after",  "request_id": "ab12cd34", "messages": [...], "answer": {"role": "assistant", "content": "hi!"}}
```

Its **Live** tab shows the last 40 requests as HTTP, in the order it happened:
the REQUEST your tool sent, the REQUEST petsitter sent the provider, the
RESPONSE that came back, and the RESPONSE petsitter sent your tool. Each is the
request or status line, every header with its real value, and the body with
only the content (messages, system prompt, the reply) swapped for a note of its
size; streams are summarised (events, heartbeats, whether [DONE] came, finish
or stop reason, usage, text and reasoning sizes, each tool call and whether its
arguments are valid JSON, and the last few events verbatim). Each response says
how it ended: the provider's "complete", "closed early" (petsitter stopped
reading) or "error"; petsitter's "complete", "client disconnected" (and when),
"error" (with the traceback) or "incomplete". When something went wrong,
**WHAT WENT WRONG** at the top says it in a sentence or two: the provider
stopped without finishing, the model hit its token limit, a tool call's
arguments aren't JSON, your tool hung up partway, a tool call went missing
between the two sides. **copy all** copies the whole exchange, verdict first.
The files have the content.

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
- The Live tab keeps each request's raw HTTP record (`petsitter.raw`), which
  fills in as the provider answers and petsitter replies, and turns it into
  text once the request is done, dropping the bodies. A restart clears it.
"""

import json
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from http import HTTPStatus

from petsitter import raw as raw_http
from petsitter.observability import LOG_DIR, current_client_addr, current_trickset, current_user_agent, request_meta
from petsitter.trick import Trick

DEFAULT_LOGGER_PATH = LOG_DIR / "traffic"
KEEP = 40                      # requests on the Live tab


def _json(body: bytes):
    try:
        return json.loads(body.decode("utf-8", "replace")) if body else None
    except ValueError:
        return None


def _chars(value) -> str:
    return f"{len(json.dumps(value, default=str)):,} chars"


def _message_note(msg) -> str:
    if not isinstance(msg, dict):
        return f"<{_chars(msg)}>"
    calls = [(c.get("function") or {}).get("name", "") for c in msg.get("tool_calls") or [] if isinstance(c, dict)]
    return f"<{msg.get('role', 'message')}, {_chars(msg.get('content'))}" + (f", tool calls: {', '.join(calls)}" if calls else "") + ">"


def _without_content(obj):
    """A request or response body with the content swapped for a note of its
    size: the messages, the system prompt, the reply. Everything else stays."""
    if not isinstance(obj, dict):
        return obj
    out = {}
    for k, v in obj.items():
        if k == "messages" and isinstance(v, list):
            out[k] = f"<{len(v)} messages, {_chars(v)}>"
        elif k in ("system", "prompt", "input", "contents", "instructions"):
            out[k] = f"<{k}, {_chars(v)}>"
        elif k == "tools" and isinstance(v, list):
            names = [(t.get("function") or {}).get("name") or t.get("name", "") for t in v if isinstance(t, dict)]
            out[k] = f"<{len(v)} tools, {_chars(v)}: {', '.join(n for n in names if n)}>"
        elif k == "choices" and isinstance(v, list):
            out[k] = [{ck: (_message_note(cv) if ck in ("message", "delta") else cv) for ck, cv in c.items()}
                      if isinstance(c, dict) else c for c in v]
        elif k == "content" and isinstance(v, list) and obj.get("type") == "message":
            out[k] = f"<{len(v)} content blocks, {_chars(v)}>"
        else:
            out[k] = v
    return out


def _stream_summary(text: str) -> dict:
    """An event stream, without its content: how many events, and what they
    say about the reply (id, model, finish or stop reason, usage, an error)."""
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
        if isinstance(ev.get("choices"), list):          # OpenAI chunks
            for k, v in ev.items():
                if k not in ("choices", "usage", "object", "created") and k not in out:
                    out[k] = v
            for c in ev["choices"]:
                if isinstance(c, dict) and c.get("finish_reason"):
                    out["finish_reason"] = c["finish_reason"]
            if ev.get("usage"):
                out["usage"] = ev["usage"]
        elif ev.get("type") == "message_start":          # Anthropic events
            out.update(_without_content(ev.get("message") or {}))
        elif ev.get("type") == "message_delta":
            out.update(ev.get("delta") or {})
            if ev.get("usage"):
                out["usage"] = {**(out.get("usage") or {}), **ev["usage"]}
        elif ev.get("type") == "error" or "error" in ev:
            out["error"] = ev.get("error", ev)
    return out


def _body(body: bytes, headers: list) -> str:
    """A body as the Live tab shows it: JSON pretty-printed without the content,
    an event stream summarised, anything else as text."""
    if not body:
        return ""
    ctype = next((v for k, v in headers if k.lower() == "content-type"), "").lower()
    text = body.decode("utf-8", "replace")
    if _is_stream(body, headers):
        summary = _stream_summary(text)
        summary.pop("events", None)
        report = raw_http.stream_report(text)
        last = report.pop("last_events")
        ending = "ended with [DONE]" if report["done"] else "NO [DONE]"
        head = (f"<event stream, {report.pop('events')} events, {report.pop('comments')} heartbeat comments, "
                f"{len(body):,} bytes, {ending}>")
        report.pop("done")
        shown = {**summary, **{k: v for k, v in report.items() if v not in (None, [], 0) or k == "finish_reason"}}
        return (head + "\n" + json.dumps(shown, indent=2, default=str) +
                "\n\nlast events:\n" + "\n".join(f"data: {e}" for e in last))
    obj = _json(body)
    if obj is not None:
        return json.dumps(_without_content(obj), indent=2, default=str)
    return text


def _is_stream(body: bytes, headers: list) -> bool:
    ctype = next((v for k, v in headers if k.lower() == "content-type"), "").lower()
    return "event-stream" in ctype or body.lstrip()[:6] in (b"data:", b"event:") or body[:1] == b":"


def _report(body: bytes, headers: list) -> dict | None:
    return raw_http.stream_report(body.decode("utf-8", "replace")) if body and _is_stream(body, headers) else None


def verdict(record: dict) -> list[str]:
    """Plain statements of what went wrong in this exchange, if anything: how
    each side's stream ended, and where the two don't match. Empty when the
    provider and petsitter both finished normally."""
    out: list[str] = []
    resp = record["response"]
    client = _report(bytes(resp["body"]), resp["headers"])
    up_ex = record["upstream"][-1] if record["upstream"] else None
    up = None
    if up_ex is not None:
        got = raw_http._decoded(bytes(up_ex["body"]), up_ex.get("headers") or [])
        up = _report(got, up_ex.get("headers") or [])
        ms = up_ex.get("ms")
        if up_ex.get("error"):
            out.append(f"The provider call failed after {ms:,} ms: {up_ex['error']}" if ms is not None
                       else f"The provider call failed: {up_ex['error']}")
        elif up_ex.get("ended") == "closed early" and not (up is not None and up["done"]):
            # (petsitter stops at [DONE], before the empty chunk that ends the
            # body, so a stream with [DONE] in it ended properly)
            out.append(f"petsitter stopped reading the provider's response at {up_ex.get('last_byte_ms') or 0:,} ms, "
                       f"before it ended.")
        if up is not None and up_ex.get("ended") == "complete" and not up["done"]:
            out.append("The provider's stream ended without [DONE]" +
                       (f" (finish_reason {up['finish_reason']})." if up["finish_reason"] else " and without a finish_reason."))
        if up is not None:
            if up["finish_reason"] == "length":
                out.append("The model stopped at its token limit (finish_reason length): the provider cut the reply off.")
            for c in up["tool_calls"]:
                if not c["arguments_json"]:
                    out.append(f"The provider's tool call {c['name']} has arguments that aren't valid JSON "
                               f"({c['arguments_chars']:,} chars).")
            for e in up["errors"]:
                out.append(f"The provider's stream carried an error: {e}")
    ended = resp.get("ended")
    if ended == "client disconnected":
        at = resp.get("client_gone_ms")
        if client is not None and client["done"]:
            pass   # it had everything
        elif client is not None and client["finish_reason"]:
            out.append(f"Your tool disconnected at {at:,} ms, after it got the finish_reason "
                       f"({client['finish_reason']}) but before [DONE].")
        else:
            got = f"after {client['events']:,} events" if client is not None else f"after {len(resp['body']):,} bytes"
            out.append(f"Your tool disconnected at {at:,} ms, {got}, before the reply finished.")
    elif ended == "error":
        first = (resp.get("error") or "").strip().splitlines()
        out.append("petsitter failed while answering: " + (first[-1] if first else "unknown error"))
    elif ended == "incomplete":
        out.append("petsitter's response stopped short, with no error and no disconnect recorded.")
    if up is not None and client is not None:
        n_up, n_cl = len(up["tool_calls"]), len(client["tool_calls"])
        if n_up != n_cl:
            out.append(f"The provider sent {n_up} tool call{'s' if n_up != 1 else ''}; your tool got {n_cl}.")
        if any(not c["arguments_json"] for c in client["tool_calls"]) and all(c["arguments_json"] for c in up["tool_calls"]):
            out.append("Your tool got a tool call whose arguments aren't valid JSON, though the provider's were.")
        if up["finish_reason"] and client["finish_reason"] and up["finish_reason"] != client["finish_reason"]:
            out.append(f"finish_reason was {up['finish_reason']} from the provider, {client['finish_reason']} to your tool.")
    return out


def _reason(status) -> str:
    try:
        return f"{status} {HTTPStatus(status).phrase}"
    except (ValueError, TypeError):
        return str(status)


def _http(record: dict) -> dict:
    """The record as four kinds of block (request and response, for the
    client and for each provider call), each as HTTP text without the content."""
    req, resp = record["request"], record["response"]
    version = req.get("http_version") or "1.1"
    blocks = {
        "client_request": "\n".join([f"{req['line']} HTTP/{version}",
                                     *(f"{k}: {v}" for k, v in req["headers"])]) +
                          "\n\n" + _body(bytes(req["body"]), req["headers"]),
        "client_response": None if resp["status"] is None else
            "\n".join([f"HTTP/{version} {_reason(resp['status'])}", *(f"{k}: {v}" for k, v in resp["headers"])]) +
            "\n\n" + _body(bytes(resp["body"]), resp["headers"]),
        "client_status": resp["status"], "done": resp["done"],
        "client": {k: resp.get(k) for k in ("ended", "error", "first_byte_ms", "last_byte_ms", "ms", "client_gone_ms")},
        "verdict": verdict(record) if resp["done"] else [],
        "upstream": [],
    }
    for ex in record["upstream"]:
        sent_body = ex.get("request_body") or b""
        got = raw_http._decoded(bytes(ex["body"]), ex.get("headers") or [])
        blocks["upstream"].append({
            "status": ex.get("status"), "error": ex.get("error"), "ms": ex.get("ms"),
            "first_byte_ms": ex.get("first_byte_ms"), "last_byte_ms": ex.get("last_byte_ms"),
            "ended": ex.get("ended"), "url": ex.get("url"),
            "request": "\n".join([f"{ex['method']} {ex['url']} HTTP/1.1",
                                  *(f"{k}: {v}" for k, v in ex.get("request_headers") or [])]) +
                       "\n\n" + _body(sent_body, ex.get("request_headers") or []),
            "response": (f"<no response: {ex.get('error')}>" if ex.get("status") is None else
                         "\n".join([f"{ex.get('http_version') or 'HTTP/1.1'} {_reason(ex['status'])}",
                                    *(f"{k}: {v}" for k, v in ex.get("headers") or [])]) +
                         "\n\n" + _body(got, ex.get("headers") or [])),
        })
    return blocks


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
        ts = current_trickset()
        ua = current_user_agent()
        entry = {
            "id": self.request_id, "at": time.time(), "from": current_client_addr(),
            "program": meta.get("x_title") or (ua.split("/")[0].split(" ")[0] if ua else ""),
            "api": meta.get("api") or "openai",
            "channel": ("Default" if ts.name == "_default" else ts.name) if ts is not None else "",
            "model": meta.get("model") or (params or {}).get("model", ""),
            "stream": bool(meta.get("stream", (params or {}).get("stream", False))),
            # the raw HTTP of this request, still filling in (the provider's
            # response, petsitter's own); turned into text when it's shown
            "_record": raw_http.current(),
        }
        if entry["_record"] is not None:
            raw_http.on_finish(entry["_record"], lambda record, e=dict(entry): self._save_http(e, record))
        with self._lock:
            self._recent.append(entry)
            self._settle()
        self.publish({"event": "traffic", "id": entry["id"]})

    def _save_http(self, entry: dict, record: dict) -> None:
        """When the exchange is over, however it ended, one line in http.jsonl:
        the HTTP as the Live tab shows it, the verdict, and both response
        bodies whole (the streams, event by event), so a reply that went wrong
        can still be taken apart after it has left the last 40."""
        line = {k: v for k, v in entry.items() if k != "_record"}
        line["timestamp"] = _now()
        line["http"] = _http(record)
        line["client_response_body"] = bytes(record["response"]["body"]).decode("utf-8", "replace")
        line["upstream_response_bodies"] = [
            raw_http._decoded(bytes(ex["body"]), ex.get("headers") or []).decode("utf-8", "replace")
            for ex in record["upstream"]]
        self._append("http", line)

    def _remember_reply(self, reply) -> None:
        self.publish({"event": "traffic", "id": self.request_id})

    def _settle(self) -> None:
        """Turn finished requests into their text, so their bodies (with the
        content) aren't held in memory. Called with the lock held."""
        for entry in self._recent:
            record = entry.get("_record")
            if record is not None and record["response"]["done"]:
                entry["http"] = _http(record)
                del entry["_record"]

    def _shown(self, entry: dict) -> dict:
        out = {k: v for k, v in entry.items() if k != "_record"}
        if entry.get("_record") is not None:
            out["http"] = _http(entry["_record"])
        return out

    def ui_action(self, data):
        action = (data or {}).get("action") if isinstance(data, dict) else None
        if action == "requests":
            with self._lock:
                self._settle()
                entries = [self._shown(e) for e in reversed(self._recent)]
            return {"requests": entries, "folder": str(self._log_path("before").parent)}
        if action == "clear":
            with self._lock:
                self._recent.clear()
            self.live_feed.clear()
            return {"ok": True}
        return super().ui_action(data)

    def _log_path(self, stage: str) -> Path:
        """The file for "before" (requests) or "after" (the transformed
        conversation and the reply)."""
        name = stage if stage in ("before", "http") else "after"
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
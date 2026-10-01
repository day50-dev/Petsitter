"""Streams a live feed of which tools your AI tool offered the model, which ones other tricks hid, and which ones the model actually called.

Agents hand the model a list of tools on every request, and some tricks narrow
that list step by step to walk the model through a process. When a tool call
goes wrong it helps to see, per request: what was offered, what was withheld (and
by which trick), and what fired. This trick publishes that, and the bundled
viewer draws it.

## How to use

Install it, then open its **Live** tab and use your AI tool as normal. Every
request shows up there as it happens. No AI tool connected yet? Press **demo**
on the Live tab to watch made-up traffic.

Put this extension **first** in the channel, so its "offered" list is what your
tool really sent, before other extensions change it. Turn on `include_schemas` to
also see each tool's full parameter schema (events get much larger).

Prefer a terminal? The same events go to a unix socket that
`./contrib/toolwatch.py` (curses) and `./contrib/toolwatch_web.py` (browser)
listen on.

## How it works

- Two datagrams per request: `request` from `pre_hook` (offered tools, pending
  tool results, conversation key) and `response` from `post_hook` (final tool
  list, `withheld`, `added`, `fired` calls with arguments, `by_trick`, `notes`).
- Attribution: `startup()` subscribes to pipeline events. After each trick's
  `pre_hook` the live tool list is re-sampled, so a change is credited to the
  trick that just ran. A trick can explain itself with
  `trace_event("gate", self, reason=...)`; non-pipeline events arrive as notes.
- Per-request state lives in `request_meta()`, not on the instance.
- Transport: every event goes to the Live tab (`self.publish`, the last 500
  kept in memory) and to a non-blocking unix datagram socket at `socket_path` (default
  `~/.cache/petsitter/toolmon.sock`). No listener or a full buffer means the event
  is dropped; it never blocks the request. Oversized events (over 60 KB) drop
  descriptions and are marked `truncated`. Swap `_emit` to use another transport.
- The conversation key hashes `x_title` plus the first user message, to stitch a
  tool call and its result (separate requests) together.
"""

import hashlib
import json
import socket
import threading
from datetime import datetime, timezone
from pathlib import Path

from petsitter.observability import LOG_DIR, request_meta, subscribe, unsubscribe
from petsitter.trick import Trick

DEFAULT_SOCKET_PATH = LOG_DIR / "toolmon.sock"

SCHEMA_VERSION = 1
MAX_DATAGRAM = 60_000
DESCRIPTION_LIMIT = 200
# Generous: this is what the viewer expands into when you click a fired call.
# 400 chars was too short to show a real tool invocation's arguments.
ARGUMENTS_LIMIT = 4000
MAX_ATTRIBUTIONS = 40
MAX_NOTES = 40

# Stages the proxy itself records.  Anything else on the wire was emitted by a
# trick that had something to say, and is passed through to the viewer as a note.
PIPELINE_STAGES = frozenset({
    "system_prompt", "pre_hook", "post_hook", "active", "dormant",
    "keyword", "prompt_keyword", "trickset", "upstream",
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _describe_tool(tool: dict, include_schema: bool = False) -> dict:
    """Reduce one OpenAI tool definition to the fields a viewer needs."""
    if not isinstance(tool, dict):
        return {"name": str(tool)}
    body = tool.get("function") if isinstance(tool.get("function"), dict) else tool
    entry = {"name": str(body.get("name", "") or "(unnamed)")}
    description = body.get("description")
    if description:
        text = str(description).strip().replace("\n", " ")
        entry["description"] = text[:DESCRIPTION_LIMIT]
    if include_schema and isinstance(body.get("parameters"), dict):
        entry["parameters"] = body["parameters"]
    return entry


def _tool_names(tools) -> list[str]:
    """The names in a tool list, order preserved, duplicates collapsed."""
    names: list[str] = []
    for tool in tools or []:
        name = _describe_tool(tool)["name"]
        if name not in names:
            names.append(name)
    return names


def _conversation_key(messages: list, x_title: str) -> str:
    """A stable-ish id for the conversation this request belongs to.

    Chat completions is stateless, so a tool call and the tool *result* that
    answers it arrive on two different requests with two different request ids.
    Hashing the opening of the conversation is enough to stitch them back
    together in a viewer.  It is a heuristic: editing the first user message
    starts a new key.
    """
    first_user = ""
    for message in messages or []:
        if isinstance(message, dict) and message.get("role") == "user":
            first_user = str(message.get("content", ""))[:512]
            break
    digest = hashlib.sha1(f"{x_title}\x00{first_user}".encode("utf-8", "replace"))
    return digest.hexdigest()[:8]


def _pending_results(messages: list) -> list[dict]:
    """Tool results sitting in the inbound messages, i.e. last turn's answers."""
    results = []
    for message in messages or []:
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        content = str(message.get("content", ""))
        results.append({
            "name": message.get("name", ""),
            "tool_call_id": message.get("tool_call_id", ""),
            "bytes": len(content),
            "preview": content[:DESCRIPTION_LIMIT].replace("\n", " "),
        })
    return results


class ToolMonitorTrick(Trick):
    """Publishes which tools were offered, withheld, added, and invoked."""

    __brief__ = "Shows live which tools were offered, hidden, and called"
    __display_name__ = "Tool Monitor"
    __category__ = "Diagnostics"
    needs_window = 0   # only looks at replies, so they can stream
    ui_page = "tool_monitor.html"
    config_fields = [
        {
            "key": "socket_path",
            "label": "Viewer socket",
            "description": (
                "Unix datagram socket a viewer binds to receive events. "
                "Events are dropped silently when nothing is listening. "
                "Blank uses ~/.cache/petsitter/toolmon.sock."
            ),
            "type": "path",
            "default": str(DEFAULT_SOCKET_PATH),
        },
        {
            "key": "include_schemas",
            "label": "Include parameter schemas",
            "description": (
                "Send each tool's full JSON parameter schema, not just its name "
                "and description. Useful for inspecting what the model was "
                "really shown; makes events much larger."
            ),
            "type": "boolean",
            "default": False,
        },
    ]

    def __init__(self, socket_path: str = "", include_schemas: bool = False):
        self.socket_path = socket_path or str(DEFAULT_SOCKET_PATH)
        self.include_schemas = include_schemas
        self._sock = None
        self._lock = threading.Lock()

    def configure(self, config: dict) -> None:
        super().configure(config)
        if not self.socket_path:
            self.socket_path = str(DEFAULT_SOCKET_PATH)

    def startup(self) -> None:
        """Start listening to the pipeline for as long as requests are in flight.

        Subscribing here rather than at import time is what keeps the monitor an
        opt-in diagnostic: with nothing subscribed the proxy skips assembling
        event detail entirely.
        """
        subscribe(self._on_pipeline_event)

    def shutdown(self) -> None:
        unsubscribe(self._on_pipeline_event)
        with self._lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None

    def _on_pipeline_event(self, event: dict) -> None:
        """Record what the other tricks did, attributed to whichever one did it.

        Called on the request's own task, so ``request_meta`` here is the meta of
        the request the event belongs to.  Events from requests this trick is not
        part of land in a meta whose hooks never fire, and are simply discarded.
        """
        meta = request_meta()
        if not meta:
            return
        stage = event.get("stage")
        if stage == "pre_hook":
            # The event says only that this trick's pre_hook just returned.
            # Sampling the payload now, before the next trick runs, is what
            # makes the change attributable - every pre_hook edits the same
            # dict, so a moment later it no longer belongs to anyone.
            seen = meta.get("toolmon_seen")
            if seen is None:
                return
            current = _tool_names((meta.get("payload") or {}).get("tools"))
            if current == seen:
                return
            meta["toolmon_seen"] = current
            attribution = meta.setdefault("toolmon_attribution", [])
            if len(attribution) < MAX_ATTRIBUTIONS:
                attribution.append({
                    "trick": event.get("trick", "?"),
                    "withheld": [n for n in seen if n not in current],
                    "added": [n for n in current if n not in seen],
                })
        elif stage not in PIPELINE_STAGES:
            notes = meta.setdefault("toolmon_notes", [])
            if len(notes) < MAX_NOTES:
                notes.append({
                    k: v for k, v in event.items() if k != "request_id"
                })

    # -- hooking -------------------------------------------------------------

    def pre_hook(self, context: list, params: dict) -> list:
        """Snapshot the tool list as it arrived, before other tricks touch it."""
        meta = request_meta()
        payload = params or {}
        incoming = payload.get("tools") or meta.get("tools") or []

        offered = [_describe_tool(t, self.include_schemas) for t in incoming]
        # Per-request scratch space: the instance is shared across concurrent
        # requests, so the baseline has to travel with the request.
        meta["toolmon_offered"] = _tool_names(incoming)
        meta["toolmon_seen"] = _tool_names(incoming)

        self._emit({
            "event": "request",
            "request_id": meta.get("request_id", ""),
            "conversation": _conversation_key(context, meta.get("x_title", "")),
            "x_title": meta.get("x_title", ""),
            "model": meta.get("model", payload.get("model", "")),
            "stream": bool(meta.get("stream", payload.get("stream", False))),
            "messages": len(context or []),
            "offered": offered,
            "results": _pending_results(context),
        })
        return context

    def post_hook(self, context: list) -> list:
        """Compare the final tool list against the baseline; report what fired."""
        meta = request_meta()
        offered = meta.get("toolmon_offered")
        if offered is None:
            return context

        # meta["payload"] is the same dict the pre_hooks were handed, so by now
        # it carries every edit they made to the tool list.
        final_tools = (meta.get("payload") or {}).get("tools") or []
        final = _tool_names(final_tools)

        fired = []
        last = context[-1] if context else None
        if isinstance(last, dict):
            for call in last.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") or {}
                arguments = str(function.get("arguments", ""))
                entry = {
                    "name": function.get("name", ""),
                    "id": call.get("id", ""),
                    "arguments": arguments[:ARGUMENTS_LIMIT],
                }
                # Cutting a JSON string at a fixed length almost always leaves it
                # unparseable. Say so explicitly rather than let the viewer guess
                # from a JSON.parse failure whether this was truncation or a
                # genuinely malformed payload.
                if len(arguments) > ARGUMENTS_LIMIT:
                    entry["arguments_truncated"] = True
                fired.append(entry)

        self._emit({
            "event": "response",
            "request_id": meta.get("request_id", ""),
            "conversation": _conversation_key(context, meta.get("x_title", "")),
            "model": meta.get("model", ""),
            "offered": offered,
            "final": final,
            "withheld": [n for n in offered if n not in final],
            "added": [n for n in final if n not in offered],
            "fired": fired,
            "finish": "tool_calls" if fired else "content",
            "by_trick": meta.get("toolmon_attribution") or [],
            "notes": meta.get("toolmon_notes") or [],
        })
        return context

    # -- live page -------------------------------------------------------------

    def ui_html(self) -> str | None:
        page = super().ui_html() or ""
        demo = ('<button id="demo-btn" title="play some made-up agent traffic '
                'through this page">demo</button>')
        hint = ("<br><br>No AI tool connected yet? Press <b>demo</b> to watch "
                "made-up traffic.")
        return (page.replace("__TAG__", "live").replace("__DEMO_BTN__", demo)
                .replace("__EMPTY_HINT__", hint))

    def ui_action(self, data):
        action = (data or {}).get("action") if isinstance(data, dict) else None
        if action == "clear":
            self.live_feed.clear()
        elif action == "demo":
            threading.Thread(target=self._demo, daemon=True).start()
        return {"ok": True}

    DEMO_TOOLS = [
        ("read_file", "Read a file from the workspace"),
        ("write_file", "Write a file in the workspace"),
        ("run_tests", "Run the project's test suite"),
        ("search", "Search the codebase"),
        ("git_commit", "Commit staged changes"),
    ]
    DEMO_PHASES = [
        ("explore", ["read_file", "search"]),
        ("edit", ["read_file", "write_file"]),
        ("verify", ["run_tests", "read_file"]),
        ("ship", ["git_commit"]),
    ]

    def _demo(self) -> None:
        """A few rounds of made-up agent traffic, published to the Live page only.

        A phase-gating trick is imagined hiding tools each round, so every
        column of the view has something in it.
        """
        import random
        import time
        names = [n for n, _ in self.DEMO_TOOLS]
        conversation = "demo%04x" % random.getrandbits(16)
        for phase, allowed in self.DEMO_PHASES * 2:
            rid = "demo%04x" % random.getrandbits(16)
            self.publish({
                "v": SCHEMA_VERSION, "ts": _now(), "event": "request",
                "request_id": rid, "conversation": conversation,
                "x_title": f"demo/{phase}", "model": "demo-model", "stream": True,
                "messages": 4, "results": [], "demo": True,
                "offered": [{"name": n, "description": d} for n, d in self.DEMO_TOOLS],
            })
            time.sleep(0.5)
            withheld = [n for n in names if n not in allowed]
            fired = random.sample(allowed, k=1)
            self.publish({
                "v": SCHEMA_VERSION, "ts": _now(), "event": "response",
                "request_id": rid, "conversation": conversation, "model": "demo-model",
                "offered": names, "final": allowed, "withheld": withheld, "added": [],
                "fired": [{"name": n, "id": "call_%04x" % random.getrandbits(16),
                           "arguments": json.dumps({"path": "src/app.py"})} for n in fired],
                "by_trick": [{"trick": "PhaseGateTrick", "withheld": withheld, "added": []}],
                "notes": [{"stage": "gate", "trick": "PhaseGateTrick", "reason": f"phase={phase}"}],
                "finish": "tool_calls", "demo": True,
            })
            time.sleep(0.4)

    # -- transport -----------------------------------------------------------

    def _emit(self, record: dict) -> None:
        """Fire-and-forget one event. Never raises, never blocks, never retries."""
        record = {"v": SCHEMA_VERSION, "ts": _now(), **record}
        try:
            line = json.dumps(record, default=str)
        except (TypeError, ValueError):
            return
        if len(line) > MAX_DATAGRAM:
            trimmed = dict(record)
            trimmed["truncated"] = True
            for key in ("offered", "results"):
                if isinstance(trimmed.get(key), list):
                    trimmed[key] = [
                        {"name": e.get("name", "")} if isinstance(e, dict) else e
                        for e in trimmed[key]
                    ]
            line = json.dumps(trimmed, default=str)[:MAX_DATAGRAM]
        try:
            # The same event, for the dashboard's Live tab (kept in memory).
            self.publish(json.loads(line))
        except ValueError:
            pass  # cut mid-string by the size cap: only the socket gets it
        self._send(line.encode("utf-8", "replace"))

    def _send(self, blob: bytes) -> None:
        with self._lock:
            if self._sock is None:
                try:
                    self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                    self._sock.setblocking(False)
                except OSError:
                    self._sock = None
                    return
            try:
                self._sock.sendto(blob, self.socket_path)
            except (FileNotFoundError, ConnectionRefusedError, BlockingIOError):
                # No viewer bound, or its buffer is full. Dropping is correct:
                # diagnostics must never slow the request path.
                pass
            except OSError:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None

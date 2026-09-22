"""Tool monitor trick.

Publishes a datagram per request describing the tool traffic around a model
call, so an external viewer can show what the model was actually offered and
what it did with it.

Two records go out per request::

    {"event": "request",  ...}   # pre_hook: what arrived from the client
    {"event": "response", ...}   # post_hook: what went upstream, what fired

The interesting field is ``withheld``.  Sophisticated agentic flows do not hand
the model every tool it owns; they gate the list per step, so the model is
walked through a flow chart rather than turned loose.  When another trick does
that gating, this trick reports the difference between the list the client sent
and the list that actually went upstream.

It reports *which* trick withheld each tool, not just that it vanished.  The
framework only says that a given trick's ``pre_hook`` has run; this trick
subscribes to that event and samples the tool list itself each time one
arrives, so a change is attributed to whichever trick had just finished.  All of
that lives here rather than in the pipeline, because only a trick that cares
about tools has any reason to look at them.

A gating trick that also wants to explain *why* can say so itself with
``trace_event("gate", self, reason=...)``; anything a trick emits that way is
forwarded to the viewer as a note.

**Position this trick first in the trickset.**  ``pre_hook`` snapshots the
incoming tool list before any other trick has rewritten it, and ``post_hook``
re-reads the live payload to see the end state.  Placed later, the "offered"
baseline is whatever the tricks ahead of it already did.

Transport is a unix datagram socket, chosen so that publishing costs nothing
when nobody is listening: if no viewer has bound the path, ``sendto`` fails
immediately and the event is dropped.  The socket is non-blocking, so a slow or
wedged viewer can never apply back-pressure to the request path.  Nothing about
the events is specific to this transport -- swap ``_emit`` for Redis, D-Bus, or
whatever your viewer speaks.
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

    __brief__ = "Publishes tool offered/withheld/fired events to a unix socket"
    __display_name__ = "Tool Monitor"
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

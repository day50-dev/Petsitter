"""Request-scoped observability for petsitter.

Provides per-request correlation ids and routing of request pipeline logs to
the trickset that handled the request.

Logs emitted during a chat request are written to the active trickset's own
log file (see ``Trickset.get_logger``) and are prefixed with a short request
id so they can be correlated across files:

    [ab12cd34] trickset 'gemma4' matched (X-Title='*' Model='gemma4*')

Lifecycle logs (install/uninstall/startup/shutdown) go to the trickset's log
file even when no request is running.
"""

import contextvars
import logging
import threading
import uuid
from pathlib import Path
from typing import Any

LOG_DIR = Path.home() / ".cache" / "petsitter"
TRICKSET_LOG_DIR = LOG_DIR / "tricksets"

_base = logging.getLogger("petsitter")

_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("petsitter_request_id", default="")
_current_trickset: contextvars.ContextVar[Any] = contextvars.ContextVar("petsitter_current_trickset", default=None)


def new_request_id() -> str:
    return uuid.uuid4().hex[:8]


def set_request_id(rid: str) -> contextvars.Token:
    return _request_id.set(rid)


def reset_request_id(token: contextvars.Token) -> None:
    _request_id.reset(token)


def set_current_trickset(trickset: Any) -> contextvars.Token:
    return _current_trickset.set(trickset)


def reset_current_trickset(token: contextvars.Token) -> None:
    _current_trickset.reset(token)


_trace: contextvars.ContextVar[Any] = contextvars.ContextVar("petsitter_trace", default=None)


def start_trace() -> contextvars.Token:
    """Begin collecting a structured trace of hook activity for this request.

    The playground turns this on to collect a trace it returns with the reply.
    It is not the only way events flow: see ``subscribe`` for observers that
    want them live.  When no trace is active and nothing has subscribed,
    ``trace_event`` is a no-op, so the normal request path is unaffected.
    """
    return _trace.set([])


def reset_trace(token: contextvars.Token) -> None:
    _trace.reset(token)


def get_trace() -> list[dict] | None:
    return _trace.get()


_subscribers: list = []
_subscribers_lock = threading.Lock()
_dispatching: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "petsitter_trace_dispatching", default=False
)


def subscribe(callback) -> Any:
    """Receive every pipeline event, for as long as the callback stays registered.

    A hook only ever sees its own slice of a request: ``pre_hook`` is handed the
    messages and the payload, and learns nothing about what the tricks ahead of
    it did to either.  A trick that exists to *report* on the pipeline - a tool
    monitor, an inspector - needs the rest, so it subscribes here and is called
    with each event as the proxy records it.

    The callback receives one dict per event: ``stage``, usually ``trick`` (the
    class name of whichever trick the stage belongs to), ``request_id``, and
    whatever detail that stage carries.  It is called synchronously on the
    request's own task, so it must be quick and must not block; anything slow or
    failure-prone belongs behind a queue the callback only feeds.  Exceptions
    raised by a callback are logged and swallowed - a broken observer must never
    take down the request it was watching.

    Tricks normally subscribe in ``startup()`` and unsubscribe in ``shutdown()``.
    Returns the callback, so it can be used as a decorator.
    """
    with _subscribers_lock:
        if callback not in _subscribers:
            _subscribers.append(callback)
    return callback


def unsubscribe(callback) -> bool:
    """Stop delivering events to a callback. True if it was registered."""
    with _subscribers_lock:
        try:
            _subscribers.remove(callback)
            return True
        except ValueError:
            return False


def tracing_active() -> bool:
    """Whether anything is collecting events right now.

    Building the detail for an event is not always free, so the proxy checks
    this before assembling anything beyond the basics.  When no trace is running
    and nothing has subscribed, ``trace_event`` is a no-op and the pipeline pays
    nothing for being observable.
    """
    if _trace.get() is not None:
        return True
    with _subscribers_lock:
        return bool(_subscribers)


def trace_event(stage: str, trick: Any = None, **detail) -> None:
    """Record one pipeline step: which stage, which trick, what changed.

    Tricks may call this too, and should when they do something a viewer could
    not otherwise infer.  A trick that narrows the tool list can report *what*
    it removed simply by removing it, but only the trick itself knows *why*::

        trace_event("gate", self, withheld=dropped, reason=f"phase={phase}")
    """
    events = _trace.get()
    with _subscribers_lock:
        subscribers = list(_subscribers)
    if events is None and not subscribers:
        return

    entry: dict[str, Any] = {"stage": stage}
    if trick is not None:
        # The class name is what the dashboard puts in data-name, so it is
        # enough to light up the right row without threading ids through.
        entry["trick"] = trick if isinstance(trick, str) else type(trick).__name__
    entry.update(detail)

    if events is not None:
        events.append(entry)

    if not subscribers or _dispatching.get():
        # A subscriber that emits its own events would otherwise recurse.
        return

    delivered = dict(entry)
    delivered.setdefault("request_id", _request_id.get())
    token = _dispatching.set(True)
    try:
        for callback in subscribers:
            try:
                callback(delivered)
            except Exception:
                _base.exception("trace subscriber %r failed", callback)
    finally:
        _dispatching.reset(token)


_request_meta: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "petsitter_request_meta", default=None
)


def start_request_meta(**initial) -> contextvars.Token:
    """Open the metadata channel that travels alongside a request's payload.

    Hooks see the conversation, but not everything about the request that
    produced it: ``post_hook`` is handed a message list with no way back to the
    tools, headers, or model that came with it.  Rather than have each trick
    stash that on ``self`` - which is shared across concurrent requests and so
    races - the proxy opens one dict per request here.  Being a contextvar it
    is per-task, so two requests in flight cannot see each other's.

    Tricks may also use it as scratch space to carry their own state between
    hooks within a single request.
    """
    return _request_meta.set(dict(initial))


def reset_request_meta(token: contextvars.Token) -> None:
    _request_meta.reset(token)


def request_meta() -> dict:
    """The current request's metadata, or an inert dict outside a request."""
    meta = _request_meta.get()
    return meta if meta is not None else {}


def request_tag() -> str:
    rid = _request_id.get()
    return f"[{rid}] " if rid else ""


def get_logger() -> logging.Logger:
    """Return the logger for the currently active trickset.

    Falls back to the base ``petsitter`` logger when no request is running or
    no trickset has been selected yet.
    """
    ts = _current_trickset.get()
    if ts is not None:
        return ts.get_logger()
    return _base

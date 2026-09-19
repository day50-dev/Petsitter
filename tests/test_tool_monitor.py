"""End-to-end tests for the tool monitor trick and pipeline event subscription.

The point of these is the attribution: when one trick narrows the tool list,
the monitor must be able to say *which* trick did it, which is information no
hook can recover on its own because every pre_hook edits the same payload dict.
"""

import json
import socket
import tempfile
from pathlib import Path

import pytest

from petsitter.observability import (
    reset_request_meta,
    start_request_meta,
    subscribe,
    trace_event,
    tracing_active,
    unsubscribe,
)
from petsitter.proxy import ProxyHandler
from petsitter.trick import Trick
from petsitter.tricks.tool_monitor import ToolMonitorTrick


def _tool(name):
    return {"type": "function", "function": {"name": name, "description": f"do {name}"}}


ALL_TOOLS = [_tool(n) for n in
             ("read_file", "list_dir", "grep", "write_file", "apply_patch", "run_tests")]


class GateTrick(Trick):
    """Narrows the tool list, and says why it did."""

    allowed = ("read_file", "grep")

    def pre_hook(self, context, params):
        before = [t["function"]["name"] for t in params.get("tools") or []]
        params["tools"] = [
            t for t in params.get("tools") or []
            if t["function"]["name"] in self.allowed
        ]
        dropped = [n for n in before if n not in self.allowed]
        trace_event("gate", self, withheld=dropped, reason="phase=explore")
        return context


class QuietTrick(Trick):
    """Touches nothing; must not show up as an attribution."""

    def pre_hook(self, context, params):
        return context


@pytest.fixture
def listener():
    """A bound datagram socket standing in for the viewer."""
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "toolmon.sock")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.bind(path)
        sock.settimeout(2.0)
        try:
            yield sock, path
        finally:
            sock.close()


def _drain(sock):
    events = []
    while True:
        try:
            events.append(json.loads(sock.recvfrom(65536)[0].decode()))
        except socket.timeout:
            break
    return events


def _run_pipeline(handler, tricks, payload, messages):
    """Drive one request's worth of hooks, as chat_completions would."""
    token = start_request_meta(
        request_id="testreq1",
        payload=payload,
        x_title="test",
        tools=list(payload.get("tools") or []),
        model=payload.get("model", ""),
        stream=False,
    )
    try:
        handler._start_tricks(tricks)
        ctx = handler._apply_pre_hooks(messages, payload, tricks)
        ctx = ctx + [{"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "read_file", "arguments": '{"path":"x"}'}}]}]
        ctx = handler._apply_post_hooks(ctx, tricks)
        return ctx
    finally:
        handler._stop_tricks(tricks)
        reset_request_meta(token)


def test_withheld_tools_are_attributed_to_the_trick_that_withheld_them(listener):
    sock, path = listener
    monitor = ToolMonitorTrick(socket_path=path)
    gate = GateTrick()
    handler = ProxyHandler("http://localhost:1", "m", tricks=[monitor, gate])

    payload = {"model": "m", "tools": list(ALL_TOOLS), "messages": []}
    _run_pipeline(handler, [monitor, gate], payload, [{"role": "user", "content": "hi"}])

    events = _drain(sock)
    response = [e for e in events if e["event"] == "response"]
    assert len(response) == 1
    event = response[0]

    # what survived the gate
    assert event["final"] == ["read_file", "grep"]
    assert sorted(event["withheld"]) == sorted(
        ["list_dir", "write_file", "apply_patch", "run_tests"])

    # and *who* did it -- the part that needs pipeline events
    assert len(event["by_trick"]) == 1
    attribution = event["by_trick"][0]
    assert attribution["trick"] == "GateTrick"
    assert sorted(attribution["withheld"]) == sorted(
        ["list_dir", "write_file", "apply_patch", "run_tests"])
    assert attribution["added"] == []

    # the gate's own explanation rides along
    reasons = [n for n in event["notes"] if n.get("stage") == "gate"]
    assert reasons and reasons[0]["reason"] == "phase=explore"
    assert reasons[0]["trick"] == "GateTrick"

    assert [c["name"] for c in event["fired"]] == ["read_file"]


def test_tricks_that_change_nothing_are_not_attributed(listener):
    sock, path = listener
    monitor = ToolMonitorTrick(socket_path=path)
    handler = ProxyHandler("http://localhost:1", "m", tricks=[monitor, QuietTrick()])
    quiet = handler.tricks[1]

    payload = {"model": "m", "tools": list(ALL_TOOLS), "messages": []}
    _run_pipeline(handler, [monitor, quiet], payload, [{"role": "user", "content": "hi"}])

    event = [e for e in _drain(sock) if e["event"] == "response"][0]
    assert event["by_trick"] == []
    assert event["withheld"] == []
    assert event["final"] == [t["function"]["name"] for t in ALL_TOOLS]


def test_nothing_is_published_when_no_viewer_is_listening(tmp_path):
    """A dead socket path must not raise, and must not stall the pipeline."""
    monitor = ToolMonitorTrick(socket_path=str(tmp_path / "absent.sock"))
    handler = ProxyHandler("http://localhost:1", "m", tricks=[monitor, GateTrick()])

    payload = {"model": "m", "tools": list(ALL_TOOLS), "messages": []}
    ctx = _run_pipeline(handler, handler.tricks, payload,
                        [{"role": "user", "content": "hi"}])
    assert ctx[-1]["tool_calls"]          # pipeline completed normally


def test_subscription_is_scoped_to_the_trick_being_in_use(listener):
    """Nothing is subscribed before startup or after shutdown."""
    sock, path = listener
    monitor = ToolMonitorTrick(socket_path=path)
    handler = ProxyHandler("http://localhost:1", "m", tricks=[monitor])

    assert tracing_active() is False
    handler._start_tricks([monitor])
    assert tracing_active() is True
    handler._stop_tricks([monitor])
    assert tracing_active() is False


def test_a_failing_subscriber_cannot_break_the_request():
    def explode(event):
        raise RuntimeError("subscriber is broken")

    subscribe(explode)
    try:
        assert tracing_active() is True
        trace_event("pre_hook", "SomeTrick", changed=True)   # must not raise
    finally:
        unsubscribe(explode)
    assert tracing_active() is False


def test_a_subscriber_emitting_events_does_not_recurse():
    seen = []

    def echo(event):
        seen.append(event["stage"])
        if len(seen) < 50:
            trace_event("echoed", "Echo")   # would recurse without the guard

    subscribe(echo)
    try:
        trace_event("pre_hook", "SomeTrick")
    finally:
        unsubscribe(echo)
    assert seen == ["pre_hook"]

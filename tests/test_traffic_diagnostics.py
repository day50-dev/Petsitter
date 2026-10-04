"""Traffic Logger's evidence for a reply that went wrong: how each side's
stream ended (raw.py) and the verdict that compares them (tricks/logger.py)."""

import json
import time

from petsitter import raw
from petsitter.tricks.logger import verdict


def ch(delta, fr=None):
    return "data: " + json.dumps({"choices": [{"index": 0, "delta": delta, "finish_reason": fr}]}) + "\n\n"


OK = ch({"content": "hi"}) + ch({}, "stop") + "data: [DONE]\n\n"
SSE = [["content-type", "text/event-stream"]]


def record(up_body: str, client_body: str, up_ended="complete", client_ended="complete", **resp):
    rec = {"request": {}, "response": {"status": 200, "headers": SSE, "body": bytearray(client_body.encode()),
                                       "done": True, "ended": client_ended, "error": None,
                                       "client_gone_ms": None, **resp},
           "upstream": [{"status": 200, "headers": SSE, "body": bytearray(up_body.encode()),
                         "ended": up_ended, "error": None, "ms": 100, "last_byte_ms": 90}]}
    return rec


class TestStreamReport:
    def test_a_normal_stream(self):
        r = raw.stream_report(": ping\n\n" + OK)
        assert r["done"] and r["finish_reason"] == "stop" and r["events"] == 2
        assert r["comments"] == 1 and r["content_chars"] == 2
        assert r["last_events"][-1] == "[DONE]"

    def test_tool_calls_rebuilt_by_name_and_checked(self):
        body = (ch({"tool_calls": [{"index": 0, "id": "a", "function": {"name": "read", "arguments": '{"p":'}}]}) +
                ch({"tool_calls": [{"index": 0, "function": {"arguments": ' 1}'}}]}) +
                ch({"tool_calls": [{"index": 0, "id": "b", "function": {"name": "write", "arguments": '{"x"'}}]}) +
                ch({}, "tool_calls"))
        calls = raw.stream_report(body)["tool_calls"]
        assert [(c["name"], c["arguments_json"]) for c in calls] == [("read", True), ("write", False)]

    def test_anthropic_events(self):
        body = ('data: {"type": "content_block_start", "content_block": {"type": "tool_use", "name": "bash", "id": "t"}}\n\n'
                'data: {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": "{}"}}\n\n'
                'data: {"type": "message_delta", "delta": {"stop_reason": "tool_use"}}\n\n'
                'data: {"type": "message_stop"}\n\n')
        r = raw.stream_report(body)
        assert r["done"] and r["finish_reason"] == "tool_use" and r["tool_calls"][0]["name"] == "bash"


class TestVerdict:
    def test_nothing_wrong(self):
        # petsitter stops at [DONE], before the body's last empty chunk: not an early close
        assert verdict(record(OK, OK, up_ended="closed early")) == []

    def test_client_hung_up(self):
        v = verdict(record(OK, ch({"content": "hi"}), client_ended="client disconnected", client_gone_ms=1500))
        assert any("Your tool disconnected at 1,500 ms" in x for x in v)

    def test_client_hung_up_after_the_finish_reason(self):
        v = verdict(record(OK, ch({"content": "hi"}) + ch({}, "stop"), client_ended="client disconnected", client_gone_ms=9))
        assert any("after it got the finish_reason (stop) but before [DONE]" in x for x in v)

    def test_provider_stopped_without_finishing(self):
        v = verdict(record(ch({"content": "hi"}), ch({"content": "hi"})))
        assert any("without a finish_reason or [DONE]" in x for x in v)

    def test_finish_reason_without_done_is_fine(self):
        body = ch({"content": "hi"}) + ch({}, "tool_calls")
        assert verdict(record(body, body + "data: [DONE]\n\n")) == []

    def test_token_limit(self):
        body = ch({"content": "hi"}) + ch({}, "length") + "data: [DONE]\n\n"
        assert any("token limit" in x for x in verdict(record(body, body)))

    def test_petsitter_error(self):
        v = verdict(record(OK, OK, client_ended="error", error="Traceback...\nValueError: boom"))
        assert v == ["petsitter failed while answering: ValueError: boom"]

    def test_lost_tool_call(self):
        up = ch({"tool_calls": [{"index": 0, "id": "a", "function": {"name": "read", "arguments": "{}"}}]}) + ch({}, "tool_calls") + "data: [DONE]\n\n"
        assert any("The provider sent 1 tool call; your tool got 0." in x for x in verdict(record(up, OK)))


class TestEndings:
    def test_client_edge(self):
        token, rec = raw.begin("POST /v1/chat/completions", [], "")
        raw.add_response_body(rec, b"data: x\n\n", more_body=True)
        raw.client_gone(rec)
        seen = []
        raw.on_finish(rec, seen.append)
        raw.finish(rec)
        raw.end(token)
        assert rec["response"]["ended"] == "client disconnected" and seen == [rec]

    def test_complete_wins_over_a_late_disconnect(self):
        token, rec = raw.begin("POST /v1/chat/completions", [], "")
        raw.add_response_body(rec, b"data: [DONE]\n\n", more_body=False)
        raw.client_gone(rec)
        raw.finish(rec)
        raw.end(token)
        assert rec["response"]["ended"] == "complete"

    def test_upstream_edge(self):
        ex = {"done": False, "_t0": time.monotonic(), "url": "u", "status": 200}
        raw._finish(dict(ex))
        early = dict(ex); raw._finish(early)
        full = dict(ex, _eof=True); raw._finish(full)
        bad = dict(ex); raw._finish(bad, "ReadError: reset")
        assert (early["ended"], full["ended"], bad["ended"]) == ("closed early", "complete", "error")


class TestRequestChanges:
    def test_headers_and_fields(self):
        from petsitter.tricks.logger import request_changes
        rec = {"request": {"headers": [["User-Agent", "llcat"], ["X-Title", "llcat"], ["Host", "a"]],
                           "body": bytearray(json.dumps({"model": "m", "stream": True, "temperature": 0.2,
                                                         "messages": [{"role": "user", "content": "hi"}]}).encode())},
               "upstream": [{"request_headers": [["user-agent", "python-httpx"], ["host", "b"]],
                             "request_body": json.dumps({"messages": [{"role": "user", "content": "hi"}],
                                                         "model": "m", "stream": True, "max_tokens": 9}).encode()}]}
        assert request_changes(rec) == [
            "header changed: user-agent: llcat → python-httpx",
            "header dropped: x-title: llcat",
            "field dropped: temperature = 0.2",
            "field added: max_tokens = 9",
        ]

"""The Live page container: a trick's page plus its events/action pipes."""

import json

from starlette.applications import Starlette
from starlette.testclient import TestClient

from petsitter.gui_routes import register_gui_routes
from petsitter.proxy import ProxyHandler
from petsitter.trick import Trick
from petsitter.tricks.secrets_protector import SecretsProtectorTrick
from petsitter.tricks.tool_monitor import ToolMonitorTrick


class Echo(Trick):
    def ui_html(self):
        return "<p>hello</p>"

    def ui_action(self, data):
        self.publish({"got": data})
        return {"echo": data}


def _client(trick):
    handler = ProxyHandler("http://unused", "m", tricks=[trick])
    ts = handler.tricksets["_default"]
    ts.trick_ids = ["t1"]
    app = Starlette()
    register_gui_routes(app, handler, api_key="")
    return TestClient(app)


def test_publish_is_kept_for_late_viewers():
    t = Echo()
    t.publish({"a": 1})
    t.publish({"a": 2})
    assert [e for _, e in t.live_feed.since(0)] == [{"a": 1}, {"a": 2}]
    last = t.live_feed.since(0)[0][0]
    assert [e for _, e in t.live_feed.since(last)] == [{"a": 2}]


def test_every_trick_has_a_live_page():
    assert Trick.has_ui() and Echo.has_ui()
    assert ToolMonitorTrick.has_custom_ui() and SecretsProtectorTrick.has_custom_ui()
    assert not Trick.has_custom_ui()


def test_standard_page_and_report():
    class Plain(Trick):
        __display_name__ = "Plain <One>"
    t = Plain()
    page = t.ui_html()
    assert "Plain &lt;One&gt;" in page and "__TRICK_NAME__" not in page
    t.report("Replaced 2 em-dashes", count=2)
    (_, e), = t.live_feed.since(0)
    assert e["event"] == "report" and e["message"] == "Replaced 2 em-dashes" and e["details"] == {"count": 2}
    t.ui_action({"action": "clear"})
    assert t.live_feed.since(0) == []


def test_no_emdash_reports():
    from petsitter.tricks.no_emdash import NoEmDashTrick
    t = NoEmDashTrick()
    ctx = [{"role": "assistant", "content": "a\u2014b\u2014c"}]
    t.post_hook(ctx)
    assert ctx[-1]["content"] == "a-b-c"
    assert t.live_feed.since(0)[0][1]["message"] == "Replaced 2 em-dashes in a reply"


def test_politeify_rewrites_once_and_reuses_for_history(monkeypatch):
    from petsitter.tricks import politeify
    calls = []

    def fake_callmodel(ctx, msg, **kw):
        calls.append(msg)
        return ctx + [{"role": "user", "content": msg}, {"role": "assistant", "content": "POLITE: " + msg}]
    monkeypatch.setattr(politeify, "callmodel_sync", fake_callmodel)
    monkeypatch.setattr(politeify.PoliteifyTrick, "_model_config", staticmethod(lambda: {}))
    t = politeify.PoliteifyTrick()
    turn1 = [{"role": "user", "content": "fix this crap code now"}]
    t.pre_hook(turn1, {})
    assert turn1[0]["content"] == "POLITE: fix this crap code now"
    # next turn: the client resends the ORIGINAL first message plus a new one
    turn2 = [{"role": "user", "content": "fix this crap code now"},
             {"role": "assistant", "content": "ok"},
             {"role": "user", "content": "still broken, idiot machine"}]
    t.pre_hook(turn2, {})
    assert turn2[0]["content"] == "POLITE: fix this crap code now"     # from the cache
    assert turn2[2]["content"] == "POLITE: still broken, idiot machine"
    assert calls == ["fix this crap code now", "still broken, idiot machine"]   # no repeat call
    assert [e["message"] for _, e in t.live_feed.since(0)] == ["Rephrased a message to be more polite"] * 2


def test_page_and_action_routes():
    c = _client(Echo())
    r = c.get("/api/tricks/ui/t1/")
    assert r.status_code == 200 and "hello" in r.text
    r = c.post("/api/tricks/ui/t1/action", json={"x": 1})
    assert r.json() == {"echo": {"x": 1}}
    assert c.get("/api/tricks/ui/nope/").status_code == 404


def test_tool_monitor_page_is_wired_for_petsitter():
    page = ToolMonitorTrick().ui_html()
    assert 'EventSource("events")' in page
    assert "__TAG__" not in page and "__DEMO_BTN__" not in page and 'id="demo-btn"' in page


def test_secrets_live_events_never_carry_the_secret():
    t = SecretsProtectorTrick()
    context = [{"role": "user", "content": "mail alice@example.com, pw (secret: hunter2)"}]
    t.pre_hook(context, {})
    reply = [{"role": "assistant", "content": context[0]["content"]}]
    t.post_hook(reply)
    events = [e for _, e in t.live_feed.since(0)]
    blob = json.dumps(events)
    assert "alice@example.com" not in blob and "hunter2" not in blob
    kinds = [e["event"] for e in events]
    assert kinds.count("hidden") == 2 and "restored" in kinds


def test_json_mode_reports_fence_removal():
    from petsitter.tricks.json_mode import JsonModeTrick
    t = JsonModeTrick()
    ctx = [{"role": "assistant", "content": '```json\n{"ok": true}\n```'}]
    t.post_hook(ctx)
    assert ctx[-1]["content"] == '{"ok": true}'
    assert t.live_feed.since(0)[0][1]["message"] == "Removed a code fence so the reply parses as JSON"


def test_tool_call_reports_conversion():
    from petsitter.tricks.tool_call import ToolCallTrick
    t = ToolCallTrick()
    ctx = [{"role": "assistant", "content": '{"name": "read_file", "arguments": {"path": "a.py"}}'}]
    t.post_hook(ctx)
    if ctx[-1].get("tool_calls"):
        assert "read_file" in t.live_feed.since(0)[0][1]["message"]


def test_prompt_keyword_use_shows_on_live_page():
    from petsitter.proxy import ProxyHandler

    class Kw(Trick):
        prompt_keyword = "kw"
        def handle_prompt_keyword(self, request, messages=None, payload=None):
            return {"role": "assistant", "content": "done"}

    t = Kw()
    h = ProxyHandler("http://unused", "m", tricks=[t])
    _, resp = h._filter_prompt_keywords([{"role": "user", "content": "(kw: do the thing)"}])
    assert resp["content"] == "done"
    assert t.live_feed.since(0)[-1][1]["message"] == "Ran (kw: do the thing) and answered it directly"


def test_keyword_report_not_doubled_when_trick_reports():
    from petsitter.proxy import ProxyHandler

    class Kw(Trick):
        prompt_keyword = "kw"
        def handle_prompt_keyword(self, request, messages=None, payload=None):
            self.report("did my own thing")
            return {"role": "assistant", "content": "ok"}

    t = Kw()
    ProxyHandler("http://unused", "m", tricks=[t])._filter_prompt_keywords([{"role": "user", "content": "(kw)"}])
    assert [e["message"] for _, e in t.live_feed.since(0)] == ["did my own thing"]


def test_traffic_logger_reports_once_per_exchange(tmp_path):
    from petsitter.tricks.logger import LoggerTrick
    t = LoggerTrick(path=str(tmp_path))
    ctx = [{"role": "user", "content": "hi"}]
    t.pre_hook(ctx, {})
    t.post_hook(ctx + [{"role": "assistant", "content": "yo"}])
    msgs = [e["message"] for _, e in t.live_feed.since(0)]
    assert msgs == [f"Logged a request to {tmp_path}"]     # one line per exchange


def test_problems_reach_the_dashboard():
    from petsitter.proxy import ProxyHandler

    class Broken(Trick):
        def problems(self):
            return ["needs a thing"]

    class Crashes(Trick):
        def problems(self):
            raise RuntimeError("boom")

    h = ProxyHandler(model_url="http://x", model_name="m", tricks=[Broken(), Crashes(), Trick()])
    info = {t["name"]: t["problems"] for t in h.get_tricks_info()}
    assert info["Broken"] == ["needs a thing"]
    assert info["Crashes"] == ["Couldn't check its setup: boom"]
    assert info["Trick"] == []


def test_secrets_protector_says_when_detect_secrets_is_missing(monkeypatch):
    import petsitter.secret_scan as ss
    monkeypatch.setattr(ss, "_ds_ready", False)
    assert any("detect-secrets" in p for p in SecretsProtectorTrick().problems())
    monkeypatch.setattr(ss, "_ds_ready", True)
    assert SecretsProtectorTrick().problems() == []


def test_playground_passes_its_tools_and_tool_turns():
    seen = {}

    async def fake(payload, x_title=""):
        seen.update(payload)
        return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c", "type": "function", "function": {"name": "get_table", "arguments": "{}"}}]}}]}

    client = _client(Trick())
    import petsitter.proxy as proxy_mod
    orig = proxy_mod.ProxyHandler.chat_completions
    proxy_mod.ProxyHandler.chat_completions = lambda self, payload, x_title="": fake(payload, x_title)
    try:
        tools = [{"type": "function", "function": {"name": "get_table", "parameters": {"type": "object"}}}]
        msgs = [{"role": "user", "content": "hi"},
                {"role": "assistant", "content": None, "tool_calls": [{"id": "a", "type": "function",
                 "function": {"name": "get_table", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "a", "content": "{}"}]
        r = client.post("/api/playground", json={"messages": msgs, "tools": tools})
    finally:
        proxy_mod.ProxyHandler.chat_completions = orig
    assert r.status_code == 200
    assert seen["tools"] == tools and seen["messages"] == msgs
    assert r.json()["tool_calls"][0]["function"]["name"] == "get_table"

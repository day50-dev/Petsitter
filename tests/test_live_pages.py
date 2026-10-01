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


def test_has_ui():
    assert Echo.has_ui()
    assert ToolMonitorTrick.has_ui()
    assert SecretsProtectorTrick.has_ui()
    assert not Trick.has_ui()


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

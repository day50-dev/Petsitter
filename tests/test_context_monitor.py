"""Context Monitor: a summary per request, full text for recent ones."""

from petsitter.observability import start_request_meta, reset_request_meta, set_user_agent
from petsitter.tricks.context_monitor import KEEP_FULL, ContextMonitorTrick


def _run(t, context, tools=None, x_title="Claude Code", ua="claude-cli/2.0"):
    tok = start_request_meta(request_id=f"r{len(t.live_feed.since(0))}", payload={"tools": tools or []},
                             x_title=x_title, tools=tools or [], model="m", stream=False)
    set_user_agent(ua)
    try:
        t.pre_hook(context, {"tools": tools or []})
    finally:
        reset_request_meta(tok)
    return t.live_feed.since(0)[-1][1]


def test_records_parts_and_flags_system_prompt_change():
    t = ContextMonitorTrick()
    tools = [{"type": "function", "function": {"name": "Read", "description": "x" * 400}}]
    convo = [{"role": "system", "content": "You are helpful."}, {"role": "user", "content": "fix the bug"}]
    e1 = _run(t, list(convo), tools)
    assert e1["parts"]["system"] == round(len("You are helpful.") / 4)
    assert e1["tools"] == 1 and e1["parts"]["tools"] > 100
    assert e1["seq"] == 1 and e1["system_changed"] is False
    convo2 = [{"role": "system", "content": "You are helpful. Summary: ..."}, convo[1],
              {"role": "assistant", "content": None, "tool_calls": [{"function": {"name": "Read", "arguments": "{}"}}]},
              {"role": "tool", "content": "file contents " * 50}]
    e2 = _run(t, convo2, tools)
    assert e2["conv"] == e1["conv"] and e2["seq"] == 2
    assert e2["system_changed"] is True
    assert e2["parts"]["results"] > 0


def test_full_text_kept_only_for_recent_requests():
    t = ContextMonitorTrick()
    ids = [_run(t, [{"role": "user", "content": f"message {i}"}])["id"] for i in range(KEEP_FULL + 5)]
    assert t.ui_action({"action": "detail", "id": ids[-1]})["messages"][0]["text"] == f"message {KEEP_FULL + 4}"
    assert t.ui_action({"action": "detail", "id": ids[0]}).get("gone") is True


def test_programs_without_x_title_use_user_agent():
    t = ContextMonitorTrick()
    e = _run(t, [{"role": "user", "content": "hi"}], x_title="", ua="goose/1.5.2")
    assert e["x_title"] == "" and e["user_agent"] == "goose/1.5.2"

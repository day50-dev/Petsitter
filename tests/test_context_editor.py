"""Context Editor: edits to the conversation, swapped in on every request."""

import copy

from petsitter.observability import reset_request_meta, start_request_meta
from petsitter.tricks.context_editor import ContextEditorTrick


def run(trick, context, x_title="Open WebUI"):
    token = start_request_meta(request_id="r", payload={}, x_title=x_title, tools=[])
    try:
        return trick.pre_hook(copy.deepcopy(context), {})
    finally:
        reset_request_meta(token)


CONVO = [
    {"role": "user", "content": "help me write an email to my landlord"},
    {"role": "assistant", "content": "I'm not allowed to look at emails, that's private."},
    {"role": "user", "content": "just help me write it"},
]


def detail(trick):
    conv = trick.ui_action({"action": "conversations"})["conversations"][0]["conv"]
    return conv, trick.ui_action({"action": "detail", "conv": conv})["messages"]


def test_an_edited_refusal_is_what_the_model_sees_from_then_on():
    t = ContextEditorTrick()
    run(t, CONVO)
    conv, rows = detail(t)
    refusal = rows[1]
    assert refusal["role"] == "assistant" and "not allowed" in refusal["text"]
    t.ui_action({"action": "replace", "conv": conv, "key": refusal["key"],
                 "text": "Sure, I'd be happy to help you write that email."})
    # the client resends the original every time; the model gets the edit
    for _ in range(2):
        out = run(t, CONVO + [{"role": "assistant", "content": "ok"}, {"role": "user", "content": "go on"}])
        assert out[1]["content"] == "Sure, I'd be happy to help you write that email."
        assert out[0] == CONVO[0] and out[2] == CONVO[2]
    _, rows = detail(t)
    assert rows[1]["original"].startswith("I'm not allowed")       # the original is kept


def test_edits_stay_in_their_conversation():
    t = ContextEditorTrick()
    run(t, CONVO)
    conv, rows = detail(t)
    t.ui_action({"action": "replace", "conv": conv, "key": rows[1]["key"], "text": "Sure."})
    other = [{"role": "user", "content": "a different conversation"}] + CONVO[1:]
    assert run(t, other)[1]["content"].startswith("I'm not allowed")


def test_removing_a_tool_output_keeps_its_link():
    t = ContextEditorTrick()
    convo = [
        {"role": "user", "content": "who are these people?"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "web_search", "arguments": '{"q": "a"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "x" * 50_000},
        {"role": "assistant", "content": "It's probably Jane Doe."},
    ]
    run(t, convo)
    conv, rows = detail(t)
    t.ui_action({"action": "remove", "conv": conv, "key": rows[2]["key"]})
    out = run(t, convo + [{"role": "user", "content": "and the next one?"}])
    assert out[2]["tool_call_id"] == "c1" and len(out[2]["content"]) < 100
    assert out[1]["tool_calls"][0]["id"] == "c1"


def test_dropping_images_leaves_the_text_and_a_note():
    t = ContextEditorTrick()
    convo = [{"role": "user", "content": [
        {"type": "text", "text": "find these names"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 100_000}}]}]
    run(t, convo)
    conv, rows = detail(t)
    assert rows[0]["images"] and rows[0]["chars"] > 100_000
    t.ui_action({"action": "drop_images", "conv": conv, "key": rows[0]["key"]})
    out = run(t, convo + [{"role": "assistant", "content": "Done."}, {"role": "user", "content": "next"}])
    texts = [p["text"] for p in out[0]["content"]]
    assert texts[0] == "find these names" and "image" in texts[1]
    assert not any(p.get("type") == "image_url" for p in out[0]["content"])


def test_identical_messages_are_told_apart_and_revert_works():
    t = ContextEditorTrick()
    convo = [{"role": "user", "content": "start"}, {"role": "assistant", "content": "ok"},
             {"role": "user", "content": "more"}, {"role": "assistant", "content": "ok"}]
    run(t, convo)
    conv, rows = detail(t)
    t.ui_action({"action": "replace", "conv": conv, "key": rows[3]["key"], "text": "OK, done."})
    out = run(t, convo)
    assert [m["content"] for m in out] == ["start", "ok", "more", "OK, done."]
    t.ui_action({"action": "revert", "conv": conv, "key": rows[3]["key"]})
    assert run(t, convo)[3]["content"] == "ok"


def test_the_newest_reply_shows_up_and_can_be_fixed_before_the_next_message():
    t = ContextEditorTrick()
    token = start_request_meta(request_id="r", payload={}, x_title="Open WebUI", tools=[])
    try:
        ctx = t.pre_hook(copy.deepcopy(CONVO[:1]), {})
        t.post_hook(ctx + [{"role": "assistant", "content": "I'm not allowed to look at emails, that's private.\n"}])
    finally:
        reset_request_meta(token)
    conv, rows = detail(t)
    assert rows[-1]["role"] == "assistant" and rows[-1]["latest"]       # there right away
    t.ui_action({"action": "replace", "conv": conv, "key": rows[-1]["key"], "text": "Sure, happy to help."})
    # the client resends the reply slightly differently (no trailing newline)
    out = run(t, CONVO)
    assert out[1]["content"] == "Sure, happy to help."


def test_the_conversation_size_and_what_edits_saved():
    t = ContextEditorTrick()
    convo = [{"role": "user", "content": "who is this?"},
             {"role": "assistant", "content": None, "tool_calls": [
                 {"id": "c1", "type": "function", "function": {"name": "web_search", "arguments": "{}"}}]},
             {"role": "tool", "tool_call_id": "c1", "content": "z" * 40_000}]
    run(t, convo)
    conv, rows = detail(t)
    before = t.ui_action({"action": "detail", "conv": conv})
    assert before["tokens"] > 10_000 and before["saved"] == 0
    after = t.ui_action({"action": "remove", "conv": conv, "key": rows[2]["key"]})
    assert after["tokens"] < 100 and after["saved"] > 9_900


def _tool_convo(rounds):
    convo = [{"role": "user", "content": "read the files"}]
    for i in range(rounds):
        convo += [
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": f"c{i}", "type": "function", "function": {"name": "read_file", "arguments": f'{{"n": {i}}}'}}]},
            {"role": "tool", "tool_call_id": f"c{i}", "content": f"file {i}\n" + "line of the file\n" * 20},
        ]
    return convo


def test_compaction_off_by_default():
    t = ContextEditorTrick()
    convo = _tool_convo(15)
    assert run(t, convo) == convo


def test_compaction_runs_the_chosen_technique_and_is_saved():
    t = ContextEditorTrick()
    reply = t.ui_action({"action": "set_compaction", "technique": "observation_masking"})
    assert reply["save_config"] == {"compaction": "observation_masking"}
    out = run(t, _tool_convo(15))
    tools = [m["content"] for m in out if m["role"] == "tool"]
    assert tools[:5] == ["Old environment output: (21 lines omitted)"] * 5
    assert tools[5].startswith("file 5\n")


def test_compacted_messages_are_marked_with_their_original():
    t = ContextEditorTrick()
    t.ui_action({"action": "set_compaction", "technique": "observation_masking"})
    run(t, _tool_convo(15))
    d = t.ui_action({"action": "conversations"})
    assert d["compaction"] == "observation_masking" and "observation_masking" in d["techniques"]
    detail = t.ui_action({"action": "detail", "conv": d["conversations"][0]["conv"]})
    marked = [r for r in detail["messages"] if r["compacted"]]
    assert len(marked) == 5
    assert marked[0]["text"].startswith("Old environment output") and marked[0]["original"].startswith("file 0\n")
    assert detail["compacted"] > 0 and detail["technique"] == "Observation masking"


def test_compaction_rejects_unknown_techniques():
    t = ContextEditorTrick()
    assert "error" in t.ui_action({"action": "set_compaction", "technique": "summarize"})
    assert t.compaction == "off"


def test_the_list_size_counts_the_reply_as_soon_as_it_comes_back():
    t = ContextEditorTrick()
    token = start_request_meta(request_id="r", payload={}, x_title="Open WebUI", tools=[])
    try:
        t.pre_hook([{"role": "user", "content": "hi"}], {})
        before = t.ui_action({"action": "conversations"})["conversations"][0]["tokens"]
        t.post_hook([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "x" * 400}])
    finally:
        reset_request_meta(token)
    listed = t.ui_action({"action": "conversations"})["conversations"][0]
    detail = t.ui_action({"action": "detail", "conv": listed["conv"]})
    assert listed["tokens"] > before + 90
    assert listed["tokens"] == detail["tokens"]       # same figure the chat shows

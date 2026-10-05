"""Expose Petsitter: the model sees, and if allowed changes, the channel's extensions."""

import json

import pytest

from petsitter.observability import (reset_current_trickset, reset_request_meta, set_current_trickset,
                                     start_request_meta)
from petsitter.trickset import Trickset
from petsitter.tricks.expose_petsitter import GET, SET, ExposePetsitterTrick


def _channel(tmp_path):
    ts = Trickset("_default", "0.3.0", {"X-Title": "*", "Model": "*"},
                  ["tricks/expose_petsitter.py", "tricks/tool_monitor.py", "tricks/context_editor.py"],
                  file_path=str(tmp_path / "_default.json"))
    ts.load_tricks()
    ts.save()
    return ts


def _call(name, args=None, cid="c1"):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args or {})}}]}


class _Request:
    """A request in a channel, with the model's follow-up answers scripted."""

    def __init__(self, ts, answers, model="qwen3", x_title="Open WebUI"):
        self.ts, self.answers, self.sent = ts, list(answers), []
        self.meta = dict(request_id="r", payload={}, x_title=x_title, tools=[], model=model,
                         resend=self._resend)

    def _resend(self, messages, tools=None):
        self.sent.append((json.loads(json.dumps(messages)), tools))
        return self.answers.pop(0)

    def __enter__(self):
        self.tokens = (start_request_meta(**self.meta), set_current_trickset(self.ts))
        return self

    def __exit__(self, *a):
        reset_current_trickset(self.tokens[1])
        reset_request_meta(self.tokens[0])


def test_tells_the_model_and_offers_the_read_tool_only_by_default(tmp_path):
    ts = _channel(tmp_path)
    t = ts.tricks[0]
    assert "petsitter" in t.system_prompt("")
    params = {"tools": [{"type": "function", "function": {"name": "web_search"}}]}
    with _Request(ts, []):
        t.pre_hook([], params)
    assert [x["function"]["name"] for x in params["tools"]] == ["web_search", GET]


def test_answers_its_tool_and_returns_the_models_next_reply(tmp_path):
    ts = _channel(tmp_path)
    t = ts.tricks[0]
    convo = [{"role": "user", "content": "what is petsitter doing?"}]
    with _Request(ts, [{"role": "assistant", "content": "Tool Dashboard and Context Editor are on."}]) as req:
        t.pre_hook(convo, {"tools": []})
        out = t.post_hook(convo + [_call(GET)])
    assert out[-1] == {"role": "assistant", "content": "Tool Dashboard and Context Editor are on."}
    sent, tools = req.sent[0]
    assert sent[-2]["tool_calls"][0]["function"]["name"] == GET
    answer = json.loads(sent[-1]["content"])
    assert [e["name"] for e in answer["extensions"]] == ["Expose Petsitter", "Tool Dashboard", "Context Editor"]
    editor = answer["extensions"][2]
    assert {"key": "compaction", "value": "off"}.items() <= editor["settings"][0].items()
    assert "observation_masking" in editor["settings"][0]["options"]
    assert answer["can_change"] is False


def test_cannot_change_anything_unless_allowed(tmp_path):
    ts = _channel(tmp_path)
    t = ts.tricks[0]
    with _Request(ts, [{"role": "assistant", "content": "ok"}]) as req:
        out = t.post_hook([_call(SET, {"extension": "Context Editor", "setting": "compaction",
                                       "value": "observation_masking"})])
    # not its tool when changes are off: passed through untouched, nothing changed
    assert out[-1]["tool_calls"][0]["function"]["name"] == SET and not req.sent
    assert ts.tricks[2].compaction == "off"


def test_a_change_is_saved_and_marked_as_the_models(tmp_path):
    ts = _channel(tmp_path)
    t = ts.tricks[0]
    t.configure({"allow_changes": True})
    with _Request(ts, [{"role": "assistant", "content": "Done."}]):
        t.post_hook([_call(SET, {"extension": "context editor", "setting": "compaction",
                                 "value": "only_n_most_recent_images"})])
    assert ts.tricks[2].compaction == "only_n_most_recent_images"
    entry = json.loads((tmp_path / "_default.json").read_text())["tricks"][2]
    assert entry["config"] == {"compaction": "only_n_most_recent_images"}
    mark = entry["changed_by"]["compaction"]
    assert mark["by"] == "qwen3" and mark["program"] == "Open WebUI" and mark["at"] > 0
    # and it survives a restart
    again = Trickset.load_from_file(str(tmp_path / "_default.json"))
    assert again.tricks[2].compaction == "only_n_most_recent_images"
    assert again.trick_changed_by[again.trick_ids[2]]["compaction"]["by"] == "qwen3"


def test_your_own_change_clears_the_models_mark(tmp_path):
    ts = _channel(tmp_path)
    tid = ts.trick_ids[2]
    ts.set_trick_config(tid, {"compaction": "observation_masking"}, changed_by={"by": "qwen3", "at": 1})
    assert "compaction" in ts.trick_changed_by[tid]
    ts.set_trick_config(tid, {"compaction": "off"})
    assert tid not in ts.trick_changed_by


def test_turning_an_extension_off_and_bad_values(tmp_path):
    ts = _channel(tmp_path)
    t = ts.tricks[0]
    t.configure({"allow_changes": True})
    with _Request(ts, []):
        assert t.change("Tool Dashboard", "enabled", "false")["enabled"] is False
        assert "must be one of" in t.change("Context Editor", "compaction", "summarize")["error"]
        assert "no setting" in t.change("Context Editor", "nonsense", 1)["error"]
        assert t.change("Secrets Protector", "enabled", "false")["error"].startswith("no extension")  # not in this channel
    assert ts.trick_enabled[1] is False
    assert ts.trick_changed_by[ts.trick_ids[1]]["enabled"]["by"] == "qwen3"


def test_your_tools_calls_in_the_final_reply_go_through(tmp_path):
    ts = _channel(tmp_path)
    t = ts.tricks[0]
    final = _call("web_search", {"q": "nauru"}, cid="c9")
    with _Request(ts, [final]):
        out = t.post_hook([_call(GET)])
    assert out[-1]["tool_calls"][0]["function"]["name"] == "web_search"


def test_gives_up_after_five_rounds(tmp_path):
    ts = _channel(tmp_path)
    t = ts.tricks[0]
    with _Request(ts, [_call(GET, cid=f"c{n}") for n in range(5)]) as req:
        out = t.post_hook([_call(GET)])
    assert len(req.sent) == 5 and "tool_calls" not in out[-1]


# -- end to end, through a server, on both APIs --------------------------------

def _server(tmp_path, monkeypatch, upstream):
    from petsitter import server
    monkeypatch.setattr(server, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(server, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(server, "TRICKSETS_DIR", tmp_path / "tricksets")
    monkeypatch.setattr("petsitter.proxy._tricksets_dir", lambda: tmp_path / "tricksets")
    return server.create_app(model_url=upstream, model_name="m", api_key="",
                             trick_paths=["tricks/expose_petsitter.py", "tricks/context_editor.py"])


class _Upstream:
    """Answers the first call with a call to get_petsitter_configuration, then text."""

    def __init__(self, anthropic=False):
        self.anthropic, self.bodies = anthropic, []

    def handler(self, request):
        import httpx
        body = json.loads(request.content)
        self.bodies.append(body)
        first = len(self.bodies) == 1
        if self.anthropic:
            content = ([{"type": "tool_use", "id": "toolu_1", "name": GET, "input": {}}] if first
                       else [{"type": "text", "text": "Context Editor is on, compaction off."}])
            return httpx.Response(200, json={"id": "m", "type": "message", "role": "assistant", "model": "claude",
                                             "content": content, "stop_reason": "tool_use" if first else "end_turn",
                                             "usage": {"input_tokens": 1, "output_tokens": 1}})
        msg = (_call(GET) if first else {"role": "assistant", "content": "Context Editor is on, compaction off."})
        return httpx.Response(200, json={"choices": [{"index": 0, "message": msg,
                                                      "finish_reason": "tool_calls" if first else "stop"}]})


@pytest.mark.parametrize("anthropic", [False, True])
def test_end_to_end(tmp_path, monkeypatch, anthropic):
    import httpx
    from starlette.testclient import TestClient
    up = _Upstream(anthropic)
    real = httpx.Client
    transport = httpx.MockTransport(up.handler)
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: real(*a, **{**k, "transport": transport}))
    real_async = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: real_async(*a, **{**k, "transport": transport}))
    client = TestClient(_server(tmp_path, monkeypatch, "http://upstream.test/v1"))
    if anthropic:
        r = client.post("/use/upstream.test/v1/messages", headers={"X-Title": "Claude Code"},
                        json={"model": "claude", "max_tokens": 100, "messages": [{"role": "user", "content": "what is petsitter doing?"}]})
        assert r.status_code == 200, r.text
        assert r.json()["content"] == [{"type": "text", "text": "Context Editor is on, compaction off."}]
        assert r.json()["stop_reason"] == "end_turn"
        second = up.bodies[1]["messages"]
        assert second[-2]["content"][0]["type"] == "tool_use"
        assert second[-1]["content"][0]["type"] == "tool_result"
    else:
        r = client.post("/v1/chat/completions", headers={"X-Title": "Open WebUI"},
                        json={"model": "m", "messages": [{"role": "user", "content": "what is petsitter doing?"}]})
        assert r.status_code == 200, r.text
        choice = r.json()["choices"][0]
        assert choice["message"]["content"] == "Context Editor is on, compaction off."
        assert choice["finish_reason"] == "stop" and not choice["message"].get("tool_calls")
        assert up.bodies[1]["messages"][-1]["role"] == "tool"
    assert "petsitter" in json.dumps(up.bodies[0])          # told, in the system prompt
    assert len(up.bodies) == 2                              # asked again, once


def test_takes_values_the_way_models_send_them(tmp_path):
    ts = _channel(tmp_path)
    t = ts.tricks[0]
    t.configure({"allow_changes": True})
    with _Request(ts, []):
        for sent in ("observation_masking", {"compaction": "observation_masking"}, {"observation_masking": True}):
            t.change("Context Editor", "compaction", "off")
            assert t.change("Context Editor", "compaction", sent).get("ok"), sent
            assert ts.tricks[2].compaction == "observation_masking"
        err = t.change("Context Editor", "compaction", {"type": "string", "const": "x"})["error"]
        assert '"const": "x"' in err          # says what it got, so the model can fix it
    # the tool asks for text, which every model manages
    from petsitter.tricks.expose_petsitter import SET_TOOL
    assert SET_TOOL["function"]["parameters"]["properties"]["value"]["type"] == "string"


def test_never_returns_an_empty_reply(tmp_path):
    ts = _channel(tmp_path)
    t = ts.tricks[0]
    with _Request(ts, [_call(GET, cid=f"c{n}") for n in range(5)]):
        out = t.post_hook([_call(GET)])
    assert "5 rounds" in out[-1]["content"]


class _StreamingUpstream(_Upstream):
    """Streams "Let me check." and a call to get_petsitter_configuration; the
    follow-up (not streamed) answers in text."""

    def handler(self, request):
        import httpx
        body = json.loads(request.content)
        if not body.get("stream"):
            return super().handler(request)      # the follow-up: logged there, answered in text
        self.bodies.append(body)
        if self.anthropic:
            def ev(kind, data):
                return f"event: {kind}\ndata: {json.dumps(dict(data, type=kind))}\n\n"
            sse = "".join([
                ev("message_start", {"message": {"id": "m", "type": "message", "role": "assistant", "model": "claude",
                                                 "content": [], "usage": {"input_tokens": 1, "output_tokens": 0}}}),
                ev("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
                ev("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "Let me check."}}),
                ev("content_block_stop", {"index": 0}),
                ev("content_block_start", {"index": 1, "content_block": {"type": "tool_use", "id": "toolu_1", "name": GET, "input": {}}}),
                ev("content_block_delta", {"index": 1, "delta": {"type": "input_json_delta", "partial_json": "{}"}}),
                ev("content_block_stop", {"index": 1}),
                ev("message_delta", {"delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 5}}),
                ev("message_stop", {}),
            ])
        else:
            def chunk(delta, finish=None):
                return "data: " + json.dumps({"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"
            sse = (chunk({"role": "assistant", "content": "Let me check."})
                   + chunk({"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                                            "function": {"name": GET, "arguments": "{}"}}]})
                   + chunk({}, "tool_calls") + "data: [DONE]\n\n")
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})


@pytest.mark.parametrize("anthropic", [False, True])
def test_streams_and_answers_its_tool_at_the_end(tmp_path, monkeypatch, anthropic):
    import httpx
    from starlette.testclient import TestClient
    up = _StreamingUpstream(anthropic)
    transport = httpx.MockTransport(up.handler)
    real, real_async = httpx.Client, httpx.AsyncClient
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: real(*a, **{**k, "transport": transport}))
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: real_async(*a, **{**k, "transport": transport}))
    client = TestClient(_server(tmp_path, monkeypatch, "http://upstream.test/v1"))
    answer = "Context Editor is on, compaction off."
    if anthropic:
        r = client.post("/use/upstream.test/v1/messages", json={
            "model": "claude", "max_tokens": 100, "stream": True,
            "messages": [{"role": "user", "content": "what is petsitter doing?"}]})
        events = [json.loads(l[5:]) for l in r.text.splitlines() if l.startswith("data:")]
        starts = [e for e in events if e["type"] == "content_block_start"]
        assert [s["content_block"]["type"] for s in starts] == ["text", "text"]      # no tool_use reaches the client
        assert [s["index"] for s in starts] == [0, 1]
        text = "".join(e["delta"]["text"] for e in events if e["type"] == "content_block_delta")
        assert text == "Let me check." + answer
        assert [e["delta"]["stop_reason"] for e in events if e["type"] == "message_delta"] == ["end_turn"]
    else:
        r = client.post("/v1/chat/completions", json={
            "model": "m", "stream": True, "messages": [{"role": "user", "content": "what is petsitter doing?"}]})
        chunks = [json.loads(l[6:]) for l in r.text.splitlines() if l.startswith("data: {")]
        deltas = [c["choices"][0]["delta"] for c in chunks if c.get("choices")]
        assert not any(d.get("tool_calls") for d in deltas)
        text = "".join(d.get("content") or "" for d in deltas)
        assert text.startswith("Let me check") and text.endswith(answer)
        assert deltas[0].get("content")        # the text came first, before the answer
        finishes = [c["choices"][0]["finish_reason"] for c in chunks if c.get("choices") and c["choices"][0].get("finish_reason")]
        assert finishes == ["stop"]
    assert len(up.bodies) == 2 and not up.bodies[1].get("stream")


def test_try_it_leaves_out_extensions_that_are_switched_off(tmp_path, monkeypatch):
    """Try it pins a channel (model "trickset/<name>"); a switched-off extension
    mustn't run there either. Switched off, Expose Petsitter's tool is gone."""
    import httpx
    from starlette.testclient import TestClient
    up = _Upstream()
    transport = httpx.MockTransport(lambda r: (up.bodies.append(json.loads(r.content)), httpx.Response(
        200, json={"choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}]}))[1])
    real_async = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: real_async(*a, **{**k, "transport": transport}))
    client = TestClient(_server(tmp_path, monkeypatch, "http://upstream.test/v1"))
    r = client.post("/api/playground", json={"messages": [{"role": "user", "content": "hi"}], "trickset": "_default"})
    assert GET in json.dumps(up.bodies[-1])                     # on: offered
    assert client.post("/api/tricks/ExposePetsitterTrick/toggle",
                       json={"enabled": False, "trickset": "_default"}).status_code == 200
    r = client.post("/api/playground", json={"messages": [{"role": "user", "content": "hi"}], "trickset": "_default"})
    assert r.status_code == 200, r.text
    assert GET not in json.dumps(up.bodies[-1]) and "petsitter" not in json.dumps(up.bodies[-1].get("messages"))


def test_sudo_removes_settings_and_runs_actions(tmp_path, monkeypatch):
    import sys
    ts = Trickset("_default", "0.3.0", {"X-Title": "*", "Model": "*"},
                  ["tricks/expose_petsitter.py", "tricks/context_editor.py", "tricks/exportit.py",
                   "tricks/secrets_protector.py"], file_path=str(tmp_path / "_default.json"))
    ts.load_tricks()
    # the channel loads extensions by path: patch the module it actually loaded
    monkeypatch.setattr(sys.modules[type(ts.tricks[2]).__module__], "EXPORT_DIR", str(tmp_path / "exports"))
    t = ts.tricks[0]
    t.configure({"allow_changes": True})
    with _Request(ts, []):
        conf = t.configuration()
        export = next(e for e in conf["extensions"] if e["name"] == "Export It")
        assert export["actions"][0]["name"] == "exportit"
        assert "actions" not in next(e for e in conf["extensions"] if e["name"] == "Context Editor")
        # set, then remove: back to the default
        t.change("Context Editor", "compaction", "observation_masking")
        assert t.remove("Context Editor", "compaction")["value"] == "off"
        assert ts.tricks[1].compaction == "off" and "compaction" not in ts.trick_configs[ts.trick_ids[1]]
        # an action, as if typed: (exportit: both)
        convo = [{"role": "user", "content": "hello"}]
        result = t.run_action("Export It", "exportit", "both", convo)
        assert result["ok"] and "before" in result["answer"].lower()
        assert len(list((tmp_path / "exports").glob("convo-*.json"))) == 2
        assert "no action" in t.run_action("Export It", "nope", "", convo)["error"]
        # sudo is everything: the controls too
        assert t.change("Secrets Protector", "enabled", "false")["enabled"] is False


def test_always_hide_list_is_never_shown_or_changed(tmp_path):
    """Secrets Protector's Always hide list is secrets: even under sudo, the
    model can't read it or change it."""
    ts = Trickset("_default", "0.3.0", {"X-Title": "*", "Model": "*"},
                  ["tricks/expose_petsitter.py", "tricks/secrets_protector.py"], file_path=str(tmp_path / "_default.json"))
    ts.load_tricks()
    t, sp = ts.tricks
    t.configure({"allow_changes": True})
    sp.configure({"always_hide": "Project Nightjar"})
    with _Request(ts, []):
        conf = json.dumps(t.configuration())
        assert "Nightjar" not in conf and "always_hide" not in conf
        assert "error" in t.change("Secrets Protector", "always_hide", "")
    assert sp.always_hide == "Project Nightjar"

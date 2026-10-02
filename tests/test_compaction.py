"""Tests for the compaction techniques Context Editor offers (petsitter.compaction)."""

import json

from petsitter import compaction
from petsitter.compaction import (clear_tool_uses, compact, observation_masking,
                                  only_n_most_recent_images)


def _round(i: int, output: str = "line\nline\nline", name: str = "read_file", images: int = 0) -> list:
    """One tool round: the call, then its output."""
    content = output if not images else (
        [{"type": "text", "text": output}]
        + [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{i}-{k}"}} for k in range(images)])
    return [
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": f"call_{i}", "type": "function",
            "function": {"name": name, "arguments": json.dumps({"n": i})}}]},
        {"role": "tool", "tool_call_id": f"call_{i}", "content": content},
    ]


def _conversation(rounds: int, **kw) -> list:
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "do the thing"}]
    for i in range(rounds):
        msgs += _round(i, **kw)
    return msgs


def _tool_contents(msgs: list) -> list:
    return [m["content"] for m in msgs if m["role"] == "tool"]


class TestObservationMasking:
    def test_keeps_last_ten(self):
        out, removed = observation_masking(_conversation(15))
        contents = _tool_contents(out)
        assert removed == 5
        assert contents[:5] == ["Old environment output: (3 lines omitted)"] * 5
        assert contents[5:] == ["line\nline\nline"] * 10

    def test_ten_or_fewer_untouched(self):
        msgs = _conversation(10)
        out, removed = observation_masking(msgs)
        assert removed == 0 and out is msgs

    def test_images_counted(self):
        out, _ = observation_masking(_conversation(11, images=2))
        assert _tool_contents(out)[0] == "Old environment output: (3 lines omitted) (2 images omitted)"

    def test_reasoning_and_actions_kept(self):
        msgs = _conversation(15)
        out, _ = observation_masking(msgs)
        assert [m for m in out if m["role"] != "tool"] == [m for m in msgs if m["role"] != "tool"]

    def test_input_not_mutated(self):
        msgs = _conversation(15)
        before = json.dumps(msgs)
        observation_masking(msgs)
        assert json.dumps(msgs) == before


class TestClearToolUses:
    def test_under_trigger_untouched(self):
        msgs = _conversation(20)
        out, removed = clear_tool_uses(msgs)
        assert removed == 0 and out is msgs

    def test_over_trigger_keeps_three(self):
        big = "x" * 50_000      # ~12.5k tokens a result, so ten go over 100k
        out, removed = clear_tool_uses(_conversation(10, output=big))
        contents = _tool_contents(out)
        assert removed == 7
        assert contents[:7] == [compaction.CLEARED_PLACEHOLDER] * 7
        assert contents[7:] == [big] * 3

    def test_tool_inputs_kept_by_default(self):
        msgs = _conversation(10, output="x" * 50_000)
        out, _ = clear_tool_uses(msgs)
        calls = [m["tool_calls"] for m in out if m["role"] == "assistant"]
        assert calls == [m["tool_calls"] for m in msgs if m["role"] == "assistant"]

    def test_clear_tool_inputs(self):
        out, _ = clear_tool_uses(_conversation(10, output="x" * 50_000), clear_tool_inputs=True)
        args = [m["tool_calls"][0]["function"]["arguments"] for m in out if m["role"] == "assistant"]
        assert args[:7] == ["{}"] * 7
        assert args[7:] == [json.dumps({"n": i}) for i in range(7, 10)]

    def test_exclude_tools(self):
        msgs = _conversation(10, output="x" * 50_000)
        msgs += _round(99, output="y" * 1000, name="web_search")
        msgs += _round(98, output="z")
        out, _ = clear_tool_uses(msgs, exclude_tools=["read_file"])
        assert all(c == "x" * 50_000 for c in _tool_contents(out)[:10])

    def test_clear_at_least(self):
        msgs = _conversation(10, output="x" * 50_000)
        out, removed = clear_tool_uses(msgs, clear_at_least_input_tokens=10_000_000)
        assert removed == 0 and out is msgs


class TestOnlyNMostRecentImages:
    def test_removes_in_chunks_of_three(self):
        # 8 images: 5 over the limit, rounded down to a chunk of 3.
        out, removed = only_n_most_recent_images(_conversation(8, images=1))
        assert removed == 3
        counts = [sum(1 for p in c if p.get("type") == "image_url") for c in _tool_contents(out)]
        assert counts == [0, 0, 0, 1, 1, 1, 1, 1]

    def test_under_a_chunk_untouched(self):
        msgs = _conversation(5, images=1)
        out, removed = only_n_most_recent_images(msgs)
        assert removed == 0 and out is msgs

    def test_user_images_left_alone(self):
        msgs = _conversation(2) + [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:{k}"}} for k in range(10)]}]
        out, removed = only_n_most_recent_images(msgs)
        assert removed == 0 and out is msgs

    def test_text_kept(self):
        out, _ = only_n_most_recent_images(_conversation(8, images=1))
        assert _tool_contents(out)[0] == [{"type": "text", "text": "line\nline\nline"}]


def test_off_and_unknown():
    msgs = _conversation(20)
    assert compact(msgs, "off") == (msgs, 0)
    assert compact(msgs, "nonsense") == (msgs, 0)


def test_every_listed_technique_runs():
    for technique in compaction.TECHNIQUES:
        compact(_conversation(20, images=1), technique)

"""Tests for the Politeify trick."""

import re

import pytest

from petsitter.tricks import politeify as pf
from petsitter.tricks.politeify import PoliteifyTrick
from petsitter.trick import configure_modelset, remove_model_config


def _convo(content: str) -> list:
    return [{"role": "user", "content": content}]


def rephraser(rewrite, seen=None):
    """A fake callmodel_sync that behaves like the rewrite model: it takes the
    draft from the code block in the last message and sends rewrite(draft)
    back in one."""
    def fake(context, message="", **kw):
        last = context[-1]["content"]
        draft = re.search(r"(`{3,})\n(.*)\n\1", last, re.S).group(2)
        if seen is not None:
            seen.append({"draft": draft, "context": context, **kw})
        return list(context) + [{"role": "assistant", "content": "```\n" + rewrite(draft) + "\n```"}]
    return fake


class TestPoliteify:
    def test_rewrites_the_last_user_message(self, monkeypatch):
        seen = []
        monkeypatch.setattr(pf, "callmodel_sync", rephraser(lambda d: "Could you please help me with X?", seen))
        result = PoliteifyTrick().pre_hook(_convo("just fix this shit now"), {})
        assert result[-1]["content"] == "Could you please help me with X?"
        assert seen[0]["draft"] == "just fix this shit now"

    def test_the_conversation_has_already_agreed_to_a_code_block(self, monkeypatch):
        seen = []
        monkeypatch.setattr(pf, "callmodel_sync", rephraser(str.upper, seen))
        PoliteifyTrick().pre_hook(_convo("fix this damn thing"), {})
        ctx = seen[0]["context"]
        assert [m["role"] for m in ctx] == ["user", "assistant", "user"]
        assert "sentiments, intentions and urgency" in ctx[0]["content"]
        assert "not allowed to generate profane language" in ctx[1]["content"]
        assert ctx[2]["content"].endswith("```\nfix this damn thing\n```")

    def test_code_in_the_draft_gets_a_longer_fence(self):
        last = pf._rewriter_conversation("run ```ls``` damn it")[-1]["content"]
        assert "````\nrun ```ls``` damn it\n````" in last

    def test_a_reply_in_words_is_never_sent_on(self, monkeypatch):
        """If it answers ("Yes, he played Bert") instead of rewriting, the message goes as written."""
        monkeypatch.setattr(pf, "callmodel_sync", lambda ctx, msg="", **kw: ctx + [
            {"role": "assistant", "content": "Yes, Dick Van Dyke played Bert."}])
        t = PoliteifyTrick()
        out = t.pre_hook(_convo("Dick Van Dyke was in Mary Poppins, right?"), {})
        assert out[0]["content"] == "Dick Van Dyke was in Mary Poppins, right?"
        assert "didn't rewrite" in [e["message"] for _, e in t.live_feed.since(0)][-1]

    def test_only_the_last_user_message_is_rewritten(self, monkeypatch):
        monkeypatch.setattr(pf, "callmodel_sync", rephraser(lambda d: "polite version"))
        context = [
            {"role": "user", "content": "first message, long enough, and damn rude"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "second message, also long enough and shit"},
        ]
        result = PoliteifyTrick().pre_hook(context, {})
        assert result[0]["content"] == "first message, long enough, and damn rude"   # older: left
        assert result[2]["content"] == "polite version"

    def test_short_messages_are_left_alone(self, monkeypatch):
        def fake(*a, **kw):
            raise AssertionError("should not be called for a short message")
        monkeypatch.setattr(pf, "callmodel_sync", fake)
        assert PoliteifyTrick(min_length=12).pre_hook(_convo("hi"), {})[0]["content"] == "hi"

    def test_no_user_message_is_a_no_op(self, monkeypatch):
        def fake(*a, **kw):
            raise AssertionError("should not be called with no user message")
        monkeypatch.setattr(pf, "callmodel_sync", fake)
        context = [{"role": "system", "content": "be terse"}]
        assert PoliteifyTrick().pre_hook(context, {}) == context

    def test_rewrite_failure_passes_the_original_through(self, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("upstream down")
        monkeypatch.setattr(pf, "callmodel_sync", boom)
        result = PoliteifyTrick().pre_hook(_convo("this is fucking broken, fix it now"), {})
        assert result[0]["content"] == "this is fucking broken, fix it now"

    def test_empty_rewrite_passes_the_original_through(self, monkeypatch):
        monkeypatch.setattr(pf, "callmodel_sync", rephraser(lambda d: "   "))
        result = PoliteifyTrick().pre_hook(_convo("original wording, damn it, stays"), {})
        assert result[0]["content"] == "original wording, damn it, stays"

    def test_uses_the_rephraser_model_when_configured(self, monkeypatch):
        configure_modelset({"rephraser": {"url": "http://polite-host", "model": "polite-model", "key": "pk"}})
        try:
            seen = []
            monkeypatch.setattr(pf, "callmodel_sync", rephraser(lambda d: "fine", seen))
            PoliteifyTrick().pre_hook(_convo("please rewrite this crappy message"), {})
            assert (seen[0]["model_url"], seen[0]["model_name"], seen[0]["api_key"]) == \
                ("http://polite-host", "polite-model", "pk")
        finally:
            remove_model_config("rephraser")

    def test_falls_back_to_default_model_when_no_rephraser(self, monkeypatch):
        configure_modelset({"default": {"url": "http://default-host", "model": "default-model", "key": ""}})
        try:
            seen = []
            monkeypatch.setattr(pf, "callmodel_sync", rephraser(lambda d: "fine", seen))
            PoliteifyTrick().pre_hook(_convo("please rewrite this crappy message"), {})
            assert seen[0]["model_url"] == "http://default-host"
        finally:
            remove_model_config("default")

    def test_info_declares_capability(self):
        assert PoliteifyTrick().info({})["politeify"] is True


class TestOnlyRudeMessages:
    def test_civil_messages_go_as_written_with_no_call(self, monkeypatch):
        def fake(*a, **kw):
            raise AssertionError("a civil message shouldn't be rewritten")
        monkeypatch.setattr(pf, "callmodel_sync", fake)
        for text in ["great! it's working! fantastic news!",
                     "you aren't supposed to know about it!",
                     "free the garbage collector and drop the useless variable"]:
            assert PoliteifyTrick().pre_hook(_convo(text), {})[0]["content"] == text

    def test_a_possibly_rude_message_is_offered_to_the_rewriter(self, monkeypatch):
        """The word list only says "worth a look": the rewriter may keep it as is."""
        seen = []
        monkeypatch.setattr(pf, "callmodel_sync", rephraser(lambda d: d, seen))
        out = PoliteifyTrick().pre_hook(_convo("Dick Van Dyke was in Mary Poppins"), {})
        assert seen[0]["draft"] == "Dick Van Dyke was in Mary Poppins"
        assert out[0]["content"] == "Dick Van Dyke was in Mary Poppins"

    def test_the_instruction(self):
        i = pf.REWRITE_INSTRUCTION
        assert "sentiments, intentions and urgency" in i and "professional language" in i
        assert "displayed" not in i

    def test_cache_keeps_the_200_most_recent(self, monkeypatch):
        monkeypatch.setattr(pf, "callmodel_sync", rephraser(lambda d: "nice: " + d))
        t = PoliteifyTrick()
        for i in range(205):
            t.pre_hook(_convo(f"message {i} is shit"), {})
        assert len(t._cache) == 200 and "message 0 is shit" not in t._cache

    def test_code_is_left_out_of_the_check(self):
        assert not pf.is_rude("run `rm -rf shit/` please")
        assert not pf.is_rude("```\nassert hell == 1\n```")
        assert pf.is_rude("you fucking idiot")


def test_shouting_is_worth_a_rewrite():
    assert pf.is_rude("WHY IS THIS STILL BROKEN, fix it")
    assert pf.is_rude("IT IS NOT WORKING")
    assert not pf.is_rude("please DO NOT touch the tests")
    assert not pf.is_rude("set the API URL in the README")
    assert not pf.is_rude("```\nRUN THIS NOW PLEASE\n```")

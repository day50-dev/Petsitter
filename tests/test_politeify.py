"""Tests for the Politeify trick."""

import pytest

from petsitter.tricks import politeify as pf
from petsitter.tricks.politeify import PoliteifyTrick
from petsitter.trick import configure_modelset, remove_model_config


def _convo(content: str) -> list:
    return [{"role": "user", "content": content}]


class TestPoliteify:
    def test_rewrites_the_last_user_message(self, monkeypatch):
        calls = []

        def fake(context, message="", model_url="", model_name="", api_key=""):
            calls.append((message, model_url, model_name, api_key))
            return list(context) + [{"role": "user", "content": message},
                                     {"role": "assistant", "content": "Could you please help me with X?"}]

        monkeypatch.setattr(pf, "callmodel_sync", fake)
        t = PoliteifyTrick()
        result = t.pre_hook(_convo("just fix this shit now"), {})

        assert result[-1]["content"] == "Could you please help me with X?"
        assert calls[0][0] == "just fix this shit now"

    def test_only_the_last_user_message_is_rewritten(self, monkeypatch):
        def fake(context, message="", **kw):
            return list(context) + [{"role": "assistant", "content": "polite version"}]

        monkeypatch.setattr(pf, "callmodel_sync", fake)
        t = PoliteifyTrick()
        context = [
            {"role": "user", "content": "first message, long enough, and damn rude"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "second message, also long enough and shit"},
        ]
        result = t.pre_hook(context, {})
        assert result[0]["content"] == "first message, long enough, and damn rude"   # older: left
        assert result[2]["content"] == "polite version"

    def test_short_messages_are_left_alone(self, monkeypatch):
        def fake(*a, **kw):
            raise AssertionError("should not be called for a short message")

        monkeypatch.setattr(pf, "callmodel_sync", fake)
        t = PoliteifyTrick(min_length=12)
        result = t.pre_hook(_convo("hi"), {})
        assert result[0]["content"] == "hi"

    def test_no_user_message_is_a_no_op(self, monkeypatch):
        def fake(*a, **kw):
            raise AssertionError("should not be called with no user message")

        monkeypatch.setattr(pf, "callmodel_sync", fake)
        t = PoliteifyTrick()
        context = [{"role": "system", "content": "be terse"}]
        assert t.pre_hook(context, {}) == context

    def test_rewrite_failure_passes_the_original_through(self, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("upstream down")

        monkeypatch.setattr(pf, "callmodel_sync", boom)
        t = PoliteifyTrick()
        original = _convo("this is fucking broken, fix it now")
        result = t.pre_hook(original, {})
        assert result[0]["content"] == "this is fucking broken, fix it now"   # sent as written

    def test_empty_rewrite_passes_the_original_through(self, monkeypatch):
        def fake(context, message="", **kw):
            return list(context) + [{"role": "assistant", "content": "   "}]

        monkeypatch.setattr(pf, "callmodel_sync", fake)
        t = PoliteifyTrick()
        result = t.pre_hook(_convo("original wording, damn it, stays"), {})
        assert result[0]["content"] == "original wording, damn it, stays"

    def test_uses_a_dedicated_politeify_model_when_configured(self, monkeypatch):
        configure_modelset({
            "politeify": {"url": "http://polite-host", "model": "polite-model", "key": "pk"},
        })
        try:
            seen = {}

            def fake(context, message="", model_url="", model_name="", api_key=""):
                seen["url"] = model_url
                seen["model"] = model_name
                seen["key"] = api_key
                return list(context) + [{"role": "assistant", "content": "fine, rewritten"}]

            monkeypatch.setattr(pf, "callmodel_sync", fake)
            t = PoliteifyTrick()
            t.pre_hook(_convo("please rewrite this crappy message"), {})
            assert seen["url"] == "http://polite-host"
            assert seen["model"] == "polite-model"
            assert seen["key"] == "pk"
        finally:
            remove_model_config("politeify")

    def test_falls_back_to_default_model_when_no_dedicated_entry(self, monkeypatch):
        configure_modelset({
            "default": {"url": "http://default-host", "model": "default-model", "key": ""},
        })
        try:
            seen = {}

            def fake(context, message="", model_url="", model_name="", api_key=""):
                seen["url"] = model_url
                return list(context) + [{"role": "assistant", "content": "fine, rewritten"}]

            monkeypatch.setattr(pf, "callmodel_sync", fake)
            t = PoliteifyTrick()
            t.pre_hook(_convo("please rewrite this crappy message"), {})
            assert seen["url"] == "http://default-host"
        finally:
            remove_model_config("default")

    def test_info_declares_capability(self):
        t = PoliteifyTrick()
        caps = t.info({})
        assert caps["politeify"] is True


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

        def fake(context, message="", **kw):
            seen.append(message)
            return list(context) + [{"role": "assistant", "content": message}]
        monkeypatch.setattr(pf, "callmodel_sync", fake)
        out = PoliteifyTrick().pre_hook(_convo("Dick Van Dyke was in Mary Poppins"), {})
        assert seen == ["Dick Van Dyke was in Mary Poppins"]
        assert out[0]["content"] == "Dick Van Dyke was in Mary Poppins"

    def test_the_instruction_covers_quoted_swearing(self):
        assert "inside quotation marks" in pf.REWRITE_INSTRUCTION

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
    assert "normal case" in pf.REWRITE_INSTRUCTION

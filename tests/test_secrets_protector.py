"""Tests for SecretsProtectorTrick."""

import pytest

from petsitter.tricks.secrets_protector import SecretsProtectorTrick


class TestSecretsProtectorTrick:

    def test_detects_openai_key(self):
        trick = SecretsProtectorTrick()
        sanitized = trick._sanitize("My key is sk-proj-AbcDefGhiJklMnoPqrStuVwxYz1234567890")
        assert "sk-proj-" in sanitized
        assert "AbcDefGhiJklMnoPqrStuVwxYz1234567890" not in sanitized

    def test_detects_email(self):
        trick = SecretsProtectorTrick()
        sanitized = trick._sanitize("Email me at alice@example.com")
        assert "@sanitized.local" in sanitized
        assert "alice@example.com" not in sanitized

    def test_consistent_pseudonym_for_same_secret(self):
        trick = SecretsProtectorTrick()
        s1 = trick._sanitize("alice@example.com")
        s2 = trick._sanitize("alice@example.com")
        assert s1 == s2

    def test_different_pseudonyms_for_different_secrets(self):
        trick = SecretsProtectorTrick()
        s1 = trick._sanitize("alice@example.com")
        s2 = trick._sanitize("bob@example.com")
        assert s1 != s2

    def test_restores_after_sanitize(self):
        trick = SecretsProtectorTrick()
        sanitized = trick._sanitize("My email is alice@example.com")
        restored = trick._restore(sanitized)
        assert "alice@example.com" in restored

    def test_restores_exact_original(self):
        trick = SecretsProtectorTrick()
        original = "My email is alice@example.com and key is sk-proj-AbcDefGhiJklMnoPqrStuVwxYz1234567890"
        sanitized = trick._sanitize(original)
        restored = trick._restore(sanitized)
        assert restored == original

    def test_detects_aws_key(self):
        trick = SecretsProtectorTrick()
        sanitized = trick._sanitize("AWS key: AKIAIOSFODNN7EXAMPLE")
        assert "AKIA" in sanitized
        assert "AKIAIOSFODNN7EXAMPLE" not in sanitized

    def test_detects_jwt(self):
        trick = SecretsProtectorTrick()
        sanitized = trick._sanitize("token: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8")
        assert "eyJ" not in sanitized

    def test_detects_phone(self):
        trick = SecretsProtectorTrick()
        sanitized = trick._sanitize("Call me at 555-123-4567")
        assert "555-" in sanitized
        assert "555-123-4567" not in sanitized

    def test_detects_ssn(self):
        trick = SecretsProtectorTrick()
        sanitized = trick._sanitize("My SSN is 123-45-6789")
        assert "123-45-6789" not in sanitized

    def test_detects_credit_card(self):
        trick = SecretsProtectorTrick()
        sanitized = trick._sanitize("Card: 4111-1111-1111-1111")
        assert "4111-1111-1111-1111" not in sanitized

    def test_detects_ip_address(self):
        trick = SecretsProtectorTrick()
        sanitized = trick._sanitize("Server at 192.168.1.1")
        assert "192.168.1.1" not in sanitized
        assert "10." in sanitized

    def test_pre_hook_sanitizes_user_messages(self):
        trick = SecretsProtectorTrick()
        context = [
            {"role": "user", "content": "My email is alice@example.com"},
            {"role": "user", "content": "My key is sk-proj-AbcDefGhiJklMnoPqrStuVwxYz1234567890"},
        ]
        result = trick.pre_hook(context, {})
        assert "alice@example.com" not in result[0]["content"]
        assert "sk-proj-" not in result[0]["content"]
        assert "AbcDefGhiJklMnoPqrStuVwxYz1234567890" not in result[1]["content"]
        assert "sk-proj-" in result[1]["content"]

    def test_pre_hook_leaves_safe_text_unchanged(self):
        trick = SecretsProtectorTrick()
        context = [{"role": "user", "content": "What is the weather today?"}]
        result = trick.pre_hook(context, {})
        assert result[0]["content"] == "What is the weather today?"

    def test_post_hook_restores_content(self):
        trick = SecretsProtectorTrick()
        trick._vault[("email", "alice@example.com")] = "user.0001@sanitized.local"
        trick._reverse["user.0001@sanitized.local"] = "alice@example.com"
        context = [
            {"role": "assistant", "content": "I will email user.0001@sanitized.local"}
        ]
        result = trick.post_hook(context)
        assert "alice@example.com" in result[-1]["content"]

    def test_post_hook_restores_tool_call_args(self):
        trick = SecretsProtectorTrick()
        trick._vault[("email", "alice@example.com")] = "user.0001@sanitized.local"
        trick._reverse["user.0001@sanitized.local"] = "alice@example.com"
        context = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_001",
                        "type": "function",
                        "function": {
                            "name": "send_email",
                            "arguments": '{"to": "user.0001@sanitized.local", "subject": "Hello"}',
                        },
                    }
                ],
            }
        ]
        result = trick.post_hook(context)
        args = result[-1]["tool_calls"][0]["function"]["arguments"]
        assert "alice@example.com" in args

    def test_info_declares_capability(self):
        trick = SecretsProtectorTrick()
        caps = trick.info({})
        assert caps.get("secrets_protection") is True

    def test_no_false_positive_on_normal_text(self):
        trick = SecretsProtectorTrick()
        text = "Hello, I would like to know about machine learning models."
        sanitized = trick._sanitize(text)
        assert sanitized == text

    def test_pseudonyms_are_format_preserving(self):
        """Pseudonyms should keep the same general format as the original."""
        trick = SecretsProtectorTrick()
        sanitized = trick._sanitize("alice@example.com")
        assert "@" in sanitized
        assert ".local" in sanitized

        sanitized2 = trick._sanitize("555-123-4567")
        assert sanitized2.count("-") >= 2


class TestMarkedSecrets:
    """(secret: value) swaps a hand-marked value for an opaque stand-in."""

    @staticmethod
    def _marker_for_trick(trick, value):
        return trick._mark(f"(secret: {value})")

    def _proxy(self, trick):
        from petsitter.proxy import ProxyHandler
        return ProxyHandler("http://unused", "m", tricks=[trick])

    def test_substitution_is_opaque_and_stable(self):
        from petsitter.tricks.secrets_protector import _MARKER_RE
        trick = SecretsProtectorTrick()
        a = self._marker_for_trick(trick, "hunter2")
        assert _MARKER_RE.fullmatch(a)
        assert "hunter2" not in a
        assert self._marker_for_trick(trick, "hunter2") == a
        assert self._marker_for_trick(trick, "other") != a
        assert trick._mark("(secret: )") == ""

    def test_proxy_swaps_in_place_on_every_turn(self):
        trick = SecretsProtectorTrick()
        proxy = self._proxy(trick)
        messages = [
            {"role": "user", "content": "Username: (secret: realuser) Password: (secret: realpass)"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "log in again with (secret: realpass)"},
        ]
        out, short = proxy._filter_prompt_keywords(messages)
        assert short is None
        # The proxy leaves the pattern where it stood, with no "unrecognized" note...
        assert out[0]["content"] == "Username: (secret: realuser) Password: (secret: realpass)"
        assert out[0]["role"] == "user"
        # ...so the trick's pre_hook can swap it in place.
        out = trick.pre_hook(out, {})
        joined = " ".join(m["content"] for m in out)
        assert "realuser" not in joined and "realpass" not in joined
        assert "secret" not in joined
        pw = self._marker_for_trick(trick, "realpass")
        user = self._marker_for_trick(trick, "realuser")
        assert out[0]["content"] == f"Username: {user} Password: {pw}"
        assert out[2]["content"] == f"log in again with {pw}"

    def test_round_trip_through_tool_call(self):
        trick = SecretsProtectorTrick()
        pw = self._marker_for_trick(trick, 'pa"ss')
        context = [{
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "c1", "type": "function", "function": {
                "name": "login", "arguments": '{"password": "%s"}' % pw}}],
        }]
        trick.post_hook(context)
        args = context[-1]["tool_calls"][0]["function"]["arguments"]
        import json
        assert json.loads(args) == {"password": 'pa"ss'}

        # Resent next turn with the real value: it's hidden again before the model.
        history = [context[-1], {"role": "tool", "tool_call_id": "c1", "content": 'logged in as pa"ss'}]
        trick.pre_hook(history, {})
        assert 'pa\\"ss' not in history[0]["tool_calls"][0]["function"]["arguments"]
        assert pw in history[0]["tool_calls"][0]["function"]["arguments"]
        assert history[1]["content"] == f"logged in as {pw}"

    def test_restores_in_reply_text(self):
        trick = SecretsProtectorTrick()
        pw = self._marker_for_trick(trick, "s3cr3t")
        context = [{"role": "assistant", "content": f"Using {pw} now"}]
        trick.post_hook(context)
        assert context[-1]["content"] == "Using s3cr3t now"

    def test_pattern_sanitizer_leaves_stand_ins_alone(self):
        trick = SecretsProtectorTrick()
        pw = self._marker_for_trick(trick, "x")
        text = f"{pw} and alice@example.com"
        out = trick._sanitize(text)
        assert out.startswith(pw)
        assert "alice@example.com" not in out


class TestDelimitedForm:
    """(secret=DvalueD): sed-style, for values with parens or edge whitespace."""

    @pytest.mark.parametrize("typed,value", [
        ("(secret=|ab)cd|)", "ab)cd"),
        ("(secret=|ab(cd|)", "ab(cd"),
        ("(secret = ^:-( ^)", ":-( "),
        ("(secret= # spaced #)", " spaced "),
        ("(secret=|a|b|)", "a|b"),
    ])
    def test_value_taken_verbatim(self, typed, value):
        trick = SecretsProtectorTrick()
        out = trick._mark(f"pw {typed} done")
        marker = trick._mark(f"(secret=\x00{value}\x00)")
        assert out == f"pw {marker} done"
        assert trick._marked[marker] == value

    def test_colon_form_unchanged(self):
        trick = SecretsProtectorTrick()
        assert trick._mark("(secret: abc)") == trick._mark("(secret=|abc|)")

    def test_proxy_leaves_unrecognized_delimited_text_alone(self):
        from petsitter.proxy import ProxyHandler
        proxy = ProxyHandler("http://unused", "m", tricks=[SecretsProtectorTrick()])
        text = "why does print(end='') and f(x = 'a') fail"
        out, _ = proxy._filter_prompt_keywords([{"role": "user", "content": text}])
        assert out == [{"role": "user", "content": text}]

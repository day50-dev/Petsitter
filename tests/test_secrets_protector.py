"""Tests for SecretsProtectorTrick."""

import pytest

from tricks.secrets_protector import SecretsProtectorTrick


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

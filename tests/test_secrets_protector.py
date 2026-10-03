"""Tests for SecretsProtectorTrick."""

import json

import pytest

from petsitter.tricks.secrets_protector import SecretsProtectorTrick


import re

from petsitter.trick import get_prefix

STAND_IN = re.compile(re.escape(get_prefix()) + r"[0-9a-f-]{36}")


def hides(text: str, value: str) -> bool:
    """value is gone from the sanitized text, replaced by a stand-in."""
    out = SecretsProtectorTrick()._sanitize(text)
    return value not in out and bool(STAND_IN.search(out))


class TestSecretsProtectorTrick:

    @pytest.mark.parametrize("text, value", [
        # by the name it sits under (detect-secrets' keyword detector)
        ('{"emporia_vue": {"username": "bogus@yahoo.com", "password": "magical9bjyX"}}', "magical9bjyX"),
        ('DB_PASSWORD="hunter2xyz"', "hunter2xyz"),
        ("api_key = 'abc123def456'", "abc123def456"),
        # unquoted .env / YAML (our own pattern)
        ("DB_PASSWORD=hunter2xyz", "hunter2xyz"),
        ("export API_TOKEN=tok_9f8e7d", "tok_9f8e7d"),
        ("db:\n  password: s3cretValue\n", "s3cretValue"),
        ("password: What color is a banana", "color is a banana"),
        ("DB_PASSWORD=hunter2xyz  # prod", "hunter2xyz"),
        ('{"password":"What color is a banana"}', "color is a banana"),
        # the name and the value as sibling fields (a get_value() result, k8s env)
        ('{\n  "key": "password",\n  "value": "what-color-is-a-banana"\n}', "what-color-is-a-banana"),
        ('{"value": "what-color-is-a-banana", "key": "password"}', "what-color-is-a-banana"),
        ('[{"name": "DB_PASSWORD", "value": "s3cr3tpw"}]', "s3cr3tpw"),
        ("env:\n  - name: DB_PASSWORD\n    value: s3cr3tpw\n", "s3cr3tpw"),
        # vendor keys (gitleaks' rules, and ours)
        ('token = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"', "ghp_abcdefghijklmnopqrstuvwxyz0123456789"),
        # assembled here so the source holds no token-shaped literal (push protection)
        ("xox" + "b-1234567890-1234567890123-abcdefghijABCDEFGHIJabcd", "abcdefghijABCDEFGHIJabcd"),
        ("My key is sk-proj-AbcDefGhiJklMnoPqrStuVwxYz1234567890", "AbcDefGhiJklMnoPqrStuVwxYz1234567890"),
        ("AWS key: AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
        ("token: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8", "eyJhbGci"),
        ("postgres://admin:Tr0ub4dor@db.example.com:5432/app", "Tr0ub4dor"),
        # personal details
        ("Email me at alice@example.com", "alice@example.com"),
        ("Call me at 555-123-4567", "555-123-4567"),
        ("My SSN is 123-45-6789", "123-45-6789"),
        ("Card: 4111-1111-1111-1111", "4111-1111-1111-1111"),
    ])
    def test_hides(self, text, value):
        assert hides(text, value)

    @pytest.mark.parametrize("text", [
        "Hello, I would like to know about machine learning models.",
        "max_tokens = 4096",
        'password = os.environ["DB_PASSWORD"]',
        "password = getpass()",
        'self.api_key = config.get("api_key")',
        'password: str = field(default="")',
        "token: ${GITHUB_TOKEN}",
        "Server at 192.168.1.1, gateway 10.0.0.1/24",
        '{"token": "${GITHUB_TOKEN}"}',
        '{"max_tokens": "4096"}',
        '{"key": "theme", "value": "dark"}',
        '[{"name": "LOG_LEVEL", "value": "debug"}]',
        '{"name": "API_TOKEN", "value": "${API_TOKEN}"}',
        "const tokenizer = new Tokenizer(vocabulary_size)",
        'sha256 = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"',
        "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==",
    ])
    def test_leaves_alone(self, text):
        assert SecretsProtectorTrick()._sanitize(text) == text

    def test_same_value_same_stand_in(self):
        trick = SecretsProtectorTrick()
        assert trick._sanitize("alice@example.com") == trick._sanitize("alice@example.com")
        assert trick._sanitize("alice@example.com") != trick._sanitize("bob@example.com")

    def test_restores_exact_original(self):
        trick = SecretsProtectorTrick()
        original = '{"user": "alice@example.com", "password": "magical9bjyX", "key": "sk-proj-AbcDefGhiJklMnoPqrStuVwxYz1234567890"}'
        sanitized = trick._sanitize(original)
        assert STAND_IN.search(sanitized) and "magical9bjyX" not in sanitized
        assert trick._restore(sanitized) == original

    def test_pre_hook_sanitizes_user_messages(self):
        trick = SecretsProtectorTrick()
        context = [
            {"role": "user", "content": "My email is alice@example.com"},
            {"role": "user", "content": "My key is sk-proj-AbcDefGhiJklMnoPqrStuVwxYz1234567890"},
        ]
        result = trick.pre_hook(context, {})
        assert "alice@example.com" not in result[0]["content"]
        assert "AbcDefGhiJklMnoPqrStuVwxYz1234567890" not in result[1]["content"]

    def test_pre_hook_leaves_safe_text_unchanged(self):
        trick = SecretsProtectorTrick()
        context = [{"role": "user", "content": "What is the weather today?"}]
        result = trick.pre_hook(context, {})
        assert result[0]["content"] == "What is the weather today?"

    def test_post_hook_restores_content(self):
        trick = SecretsProtectorTrick()
        stand_in = trick._sanitize("alice@example.com")
        result = trick.post_hook([{"role": "assistant", "content": f"I will email {stand_in}"}])
        assert result[-1]["content"] == "I will email alice@example.com"

    def test_post_hook_restores_tool_call_args(self):
        trick = SecretsProtectorTrick()
        stand_in = trick._sanitize("alice@example.com")
        context = [{"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_001", "type": "function",
            "function": {"name": "send_email", "arguments": f'{{"to": "{stand_in}", "subject": "Hello"}}'},
        }]}]
        result = trick.post_hook(context)
        args = result[-1]["tool_calls"][0]["function"]["arguments"]
        assert '"to": "alice@example.com"' in args

    def test_only_the_stand_in_format_is_swapped_back(self):
        """Something that merely resembles a hidden value is never touched."""
        trick = SecretsProtectorTrick()
        trick._sanitize("alice@example.com")
        reply = "user.0001@sanitized.local and __96178c403fd9__00000000-0000-4000-8000-000000000000"
        assert trick.post_hook([{"role": "assistant", "content": reply}])[-1]["content"] == reply

    def test_info_declares_capability(self):
        trick = SecretsProtectorTrick()
        caps = trick.info({})
        assert caps.get("secrets_protection") is True


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


class TestToolCalls:
    """Secrets that travel through tools: results coming back from a tool,
    and arguments the model sends to one."""

    CREDS = '{"emporia_vue": {"username": "bogus@yahoo.com", "password": "magical9bjyX"}}'

    def _turn(self, result_content):
        return [
            {"role": "user", "content": "read the credentials from the table"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "get_table", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": result_content},
        ]

    def test_tool_result_text_is_hidden(self):
        trick = SecretsProtectorTrick()
        out = trick.pre_hook(self._turn(self.CREDS), {})[-1]["content"]
        assert "magical9bjyX" not in out and "bogus@yahoo.com" not in out
        assert STAND_IN.search(out)

    def test_tool_result_as_parts_is_hidden(self):
        trick = SecretsProtectorTrick()
        out = trick.pre_hook(self._turn([{"type": "text", "text": self.CREDS}]), {})[-1]["content"]
        assert "magical9bjyX" not in out[0]["text"]

    def test_user_message_as_parts_is_hidden(self):
        trick = SecretsProtectorTrick()
        ctx = [{"role": "user", "content": [{"type": "text", "text": "use " + self.CREDS}]}]
        assert "magical9bjyX" not in trick.pre_hook(ctx, {})[0]["content"][0]["text"]

    def test_round_trip_through_a_tool_call(self):
        """The model reads the password from a tool result and passes it to
        another tool: the tool gets the real value, and when the client
        resends that call in the history, the model sees the stand-in again."""
        trick = SecretsProtectorTrick()
        ctx = trick.pre_hook(self._turn(self.CREDS), {})
        stand_in = next(m for m in STAND_IN.findall(ctx[-1]["content"])
                        if trick._marked[m] == "magical9bjyX")
        call = {"id": "c2", "type": "function",
                "function": {"name": "login", "arguments": f'{{"password": "{stand_in}"}}'}}
        out = trick.post_hook(ctx + [{"role": "assistant", "content": None, "tool_calls": [call]}])
        assert out[-1]["tool_calls"][0]["function"]["arguments"] == '{"password": "magical9bjyX"}'

        resent = self._turn(self.CREDS) + [
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c2", "type": "function", "function": {"name": "login",
                 "arguments": '{"password": "magical9bjyX"}'}}]},
            {"role": "tool", "tool_call_id": "c2", "content": "logged in"},
        ]
        seen = trick.pre_hook(resent, {})
        assert "magical9bjyX" not in seen[-2]["tool_calls"][0]["function"]["arguments"]
        assert stand_in in seen[-2]["tool_calls"][0]["function"]["arguments"]


@pytest.mark.parametrize("text, value", [
    ('{"password":"What color is a banana"}', "color is a banana"),
    ('{\n  "emporia_password": "the-first-us-president"\n}', "the-first-us-president"),
    ('{"db_password": "a \\"quoted\\" one"}', "quoted"),
    ("password: What color is a banana", "color is a banana"),
])
def test_named_passwords_are_caught_without_detect_secrets(monkeypatch, text, value):
    """JSON, .env and YAML don't depend on the optional detector."""
    import petsitter.secret_scan as ss
    monkeypatch.setattr(ss, "_ds_ready", False)
    monkeypatch.setattr(ss, "_cache", type(ss._cache)())
    out = SecretsProtectorTrick()._sanitize(text)
    assert value not in out and STAND_IN.search(out)


class TestEscapedJson:
    """Secrets inside JSON that's inside a JSON string, as tool results often are."""

    # A get_value() result whose value is itself a JSON object (from a real export).
    RESULT = '{\n  "key": "password",\n  "value": "{\\"password\\":\\"What color is a banana\\"}"\n}'

    @pytest.mark.parametrize("detect_secrets", [True, False])
    def test_escaped_json_in_a_tool_result(self, monkeypatch, detect_secrets):
        import petsitter.secret_scan as ss
        if not detect_secrets:
            monkeypatch.setattr(ss, "_ds_ready", False)
            monkeypatch.setattr(ss, "_cache", type(ss._cache)())
        trick = SecretsProtectorTrick()
        ctx = trick.pre_hook([{"role": "tool", "tool_call_id": "c", "content": self.RESULT}], {})
        out = ctx[0]["content"]
        assert "banana" not in out and STAND_IN.search(out)
        json.loads(out)   # still valid JSON
        assert trick._restore(out) == self.RESULT

    def test_twice_escaped(self):
        inner = json.dumps({"api_key": "s3cr3t-value-123"})
        twice = json.dumps({"payload": json.dumps({"config": inner})})
        out = SecretsProtectorTrick()._sanitize(twice)
        assert "s3cr3t-value-123" not in out and STAND_IN.search(out)

    def test_short_detected_values_are_not_swapped_everywhere(self):
        """password: test hides that value, not every "test" in the conversation."""
        trick = SecretsProtectorTrick()
        ctx = trick.pre_hook([{"role": "user", "content": "password: test"},
                              {"role": "assistant", "content": "I ran the test suite."}], {})
        assert "test" not in ctx[0]["content"]
        assert ctx[1]["content"] == "I ran the test suite."

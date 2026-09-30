"""Hides API keys, passwords and personal details from the model by swapping in stand-ins, then puts the real values back in the reply.

It's easy to paste a config file, a stack trace or a `.env` into a chat without
noticing the API key or database password in it. This trick finds those before
the request leaves petsitter and replaces them with harmless look-alikes (for
example `alice@example.com` becomes `user.0001@sanitized.local`). When the model
mentions or uses a stand-in, in its reply or in a tool call, the real value is
put back, so your tool still gets working output.

Detected automatically: OpenAI, Anthropic, AWS, Google and Stripe keys; JWT,
GitHub, Slack and Bearer tokens; database URLs and private keys; emails, phone
numbers, SSNs, credit card numbers and IP addresses.

## How to use

For anything the patterns can't recognize, mark it yourself:

```
Username: (secret: realusername) Password: (secret: realpassword)
```

With `(secret: value)`, spaces around the value are trimmed and parentheses in it
must balance. For values with unbalanced parentheses or meaningful spaces, use
the sed-style form: pick a delimiter character and wrap the value in it.
Everything between the delimiters is taken exactly as typed:

```
Password: (secret=|ab)c( |)
```

Any delimiter works (`|`, `^`, `#`, ...) as long as the value doesn't contain it
directly before a `)`.

## How it works

- `pre_hook` runs on every request, over the whole history, since your tool
  resends it each time. Marked values become opaque stand-ins like
  `__96178c403fd9__d4360d48-...`; the model is never told a swap happened. The
  proxy leaves the keyword in place for this (`strip_prompt_keyword = False`).
- Stand-ins are an HMAC of the value under a per-process key: the same value
  always gets the same stand-in, and it reveals nothing. Marked values that
  reappear in clear (restored replies, tool calls, tool results that echo them)
  are swapped out again, including their JSON-escaped form.
- Auto-detected values get a random, format-preserving pseudonym from a vault
  (same value, same pseudonym, for the life of the process). Only `user` and
  `tool` messages are scanned; overlapping matches keep the earliest, longest.
- `post_hook` restores both kinds in the reply text and in tool call arguments
  (JSON-escaped there).
- The patterns are broad: a 10-digit number reads as a phone number and a
  dotted version string like `1.2.3.4` as an IP address.
"""

import hashlib
import hmac
import json
import re
import secrets
import time
import uuid
from typing import Callable

from petsitter.trick import Trick, find_prompt_keyword_patterns

# (compiled_pattern, type_label, pseudonym_generator(counter) -> str)
# Order is by specificity — more specific patterns first reduces false positives.
_PATTERNS: list[tuple[re.Pattern, str, Callable[[int], str]]] = [
    # --- API Keys ---
    (re.compile(r'sk-proj-[A-Za-z0-9]{20,}'), "openai_proj_key",
     lambda c: f"sk-proj-{secrets.token_urlsafe(32)}"),
    (re.compile(r'(?<!proj-)sk-[A-Za-z0-9]{20,}'), "openai_key",
     lambda c: f"sk-{secrets.token_urlsafe(32)}"),
    (re.compile(r'sk-ant-[A-Za-z0-9]{20,}'), "anthropic_key",
     lambda c: f"sk-ant-{secrets.token_urlsafe(32)}"),
    (re.compile(r'AKIA[0-9A-Z]{16}'), "aws_key",
     lambda c: f"AKIA{secrets.token_hex(8).upper()}"),
    (re.compile(r'eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}'), "jwt",
     lambda c: f"{secrets.token_urlsafe(12)}.{secrets.token_urlsafe(32)}.{secrets.token_urlsafe(27)}"),
    (re.compile(r'(?:ghp_|gho_|ghu_|ghs_|ghr_)[A-Za-z0-9]{36}'), "github_token",
     lambda c: f"ghp_{secrets.token_urlsafe(27)}"),
    (re.compile(r'AIza[0-9A-Za-z_-]{35}'), "google_api_key",
     lambda c: f"AIza{secrets.token_urlsafe(26)}"),
    (re.compile(r'(?:sk_live_|pk_live_|sk_test_|pk_test_)[A-Za-z0-9]{24}'), "stripe_key",
     lambda c: f"sk_live_{secrets.token_urlsafe(18)}"),
    # --- Tokens ---
    (re.compile(r'Bearer\s+[A-Za-z0-9-_.=]{30,}'), "bearer_token",
     lambda c: f"Bearer {secrets.token_urlsafe(32)}"),
    (re.compile(r'(?:xox[abprs])-[0-9]{10,13}-[0-9]{10,13}-[A-Za-z0-9]{24}'), "slack_token",
     lambda c: f"xoxb-{c:010d}-{c*17%10_000_000_000:010d}-{secrets.token_urlsafe(18)}"),
    # --- Credentials ---
    (re.compile(r'(?:postgres(?:ql)?|mysql|mongodb(?:\\+srv)?|redis|rediss)://[^\s\'\"<>]+'),
     "database_url",
     lambda c: f"postgresql://user_{c}:redacted@db.internal:5432/db_{c}"),
    (re.compile(r'-----BEGIN\s+(?:RSA\s+)?PRIVATE\s+KEY-----'
                r'[\s\S]*?'
                r'-----END\s+(?:RSA\s+)?PRIVATE\s+KEY-----'),
     "private_key",
     lambda c: f"-----BEGIN PRIVATE KEY-----\n{secrets.token_urlsafe(64)}\n-----END PRIVATE KEY-----"),
    # --- PII ---
    (re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'), "email",
     lambda c: f"user.{c:04d}@sanitized.local"),
    (re.compile(r'\b\d{3}[-.]?\d{3}[-.]?\d{4}\b'), "phone",
     lambda c: f"555-0{c%100:02d}-{(c*17+1234)%10000:04d}"),
    (re.compile(r'\b\d{3}-\d{2}-\d{4}\b'), "ssn",
     lambda c: f"{c%100:02d}-{(c*7)%100:02d}-{(c*13+4567)%10000:04d}"),
    (re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b'), "ip_address",
     lambda c: f"10.{c//256%256}.{c%256}.{c%254+1}"),
    (re.compile(r'\b(?:\d{4}[-\s]?){3}\d{4}\b'), "credit_card",
     lambda c: f"4111-1111-1111-{c%10000:04d}"),
]


# Every hand-marked stand-in starts with this, so the way back can find them
# without guessing. It's fixed rather than per-process so a stand-in the model
# echoes from an older turn is still recognizably one of ours.
MARKER_PREFIX = "96178c403fd9"
_MARKER_RE = re.compile(
    rf"__{MARKER_PREFIX}__"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


class SecretsProtectorTrick(Trick):
    """Protect secrets by pseudonymizing them before reaching the model."""

    __brief__ = "Pseudonymizes API keys, tokens, and PII before sending to the model"
    __display_name__ = "Secrets Protector"
    __category__ = "Safety & Privacy"
    prompt_keyword = "secret"
    strip_prompt_keyword = False

    def __init__(self, patterns: list | None = None):
        self._patterns = patterns if patterns is not None else _PATTERNS
        self._vault: dict[tuple[str, str], str] = {}
        self._reverse: dict[str, str] = {}
        self._counters: dict[str, int] = {}
        # Hand-marked secrets: stand-in -> original. The stand-in is an HMAC of
        # the value, so the same secret gets the same stand-in on every resend
        # of the history without the stand-in revealing anything about it.
        self._key = secrets.token_bytes(32)
        self._marked: dict[str, str] = {}

    def _marker(self, value: str) -> str:
        digest = hmac.new(self._key, value.encode(), hashlib.sha256).digest()
        return f"__{MARKER_PREFIX}__{uuid.UUID(bytes=digest[:16], version=4)}"

    def _mark(self, text: str) -> str:
        """Replace each (secret: value) in text with its stand-in, in place."""
        keyword = self.prompt_keyword.lower()
        for p in reversed(find_prompt_keyword_patterns(text)):
            if p["keyword"].lower() != keyword:
                continue
            value = p["request"]
            if value:
                marker = self._marker(value)
                if marker not in self._marked:
                    self._announce_hidden("marked", marker)
                self._marked[marker] = value
            else:
                marker = ""
            text = text[:p["start"]] + marker + text[p["end"]:]
        return text

    def _mark_message(self, msg: dict) -> None:
        content = msg.get("content")
        if isinstance(content, str):
            msg["content"] = self._mark(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    block["text"] = self._mark(block["text"])

    def _hide_marked(self, text: str) -> str:
        """Put stand-ins back over any marked secret that shows up in clear.

        Our own restored replies come back in the history the client resends,
        as do tool calls we filled in and tool results that echo them, so the
        real values reappear on the way up and have to be swapped out again.
        """
        for marker, original in sorted(self._marked.items(), key=lambda kv: len(kv[1]), reverse=True):
            forms = [original]
            escaped = json.dumps(original)[1:-1]
            if escaped != original:
                forms.append(escaped)
            for form in forms:
                if form in text:
                    text = text.replace(form, marker)
        return text

    def _reveal_marked(self, text: str, in_json: bool = False) -> str:
        def swap(m: re.Match) -> str:
            original = self._marked.get(m.group(0))
            if original is None:
                return m.group(0)
            return json.dumps(original)[1:-1] if in_json else original
        return _MARKER_RE.sub(swap, text)

    def _scrub_message(self, msg: dict) -> None:
        content = msg.get("content")
        if isinstance(content, str):
            msg["content"] = self._hide_marked(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    block["text"] = self._hide_marked(block["text"])
        for tc in msg.get("tool_calls") or []:
            func = tc.get("function") or {}
            args = func.get("arguments")
            if isinstance(args, str):
                func["arguments"] = self._hide_marked(args)

    def _pseudonym(self, original: str, secret_type: str) -> str:
        key = (secret_type, original)
        existing = self._vault.get(key)
        if existing is not None:
            return existing
        counter = self._counters.get(secret_type, 0) + 1
        self._counters[secret_type] = counter
        for _, t, gen in self._patterns:
            if t == secret_type:
                pseudonym = gen(counter)
                break
        else:
            pseudonym = f"__{secret_type}_{counter}__"
        self._vault[key] = pseudonym
        self._reverse[pseudonym] = original
        self._announce_hidden(secret_type, pseudonym)
        return pseudonym

    def _find_spans(self, text: str) -> list[tuple[int, int, str, str]]:
        spans: list[tuple[int, int, str, str]] = []
        for pattern, secret_type, _ in self._patterns:
            for m in pattern.finditer(text):
                spans.append((m.start(), m.end(), m.group(0), secret_type))
        if not spans:
            return []
        spans.sort(key=lambda x: (x[0], -(x[1] - x[0])))
        merged: list[tuple[int, int, str, str]] = []
        last_end = 0
        for start, end, match, stype in spans:
            if start >= last_end:
                merged.append((start, end, match, stype))
                last_end = end
        return merged

    def _sanitize(self, text: str) -> str:
        if _MARKER_RE.search(text):
            parts = _MARKER_RE.split(text)
            markers = _MARKER_RE.findall(text)
            out = [self._sanitize(parts[0])]
            for marker, part in zip(markers, parts[1:]):
                out.append(marker)
                out.append(self._sanitize(part))
            return "".join(out)
        spans = self._find_spans(text)
        if not spans:
            return text
        spans.sort(key=lambda x: x[0])
        parts: list[str] = []
        pos = 0
        for start, end, match, stype in spans:
            if start > pos:
                parts.append(text[pos:start])
            parts.append(self._pseudonym(match, stype))
            pos = end
        if pos < len(text):
            parts.append(text[pos:])
        return "".join(parts)

    def _restore(self, text: str) -> str:
        if not self._reverse:
            return text
        for pseudonym in sorted(self._reverse, key=len, reverse=True):
            original = self._reverse[pseudonym]
            if pseudonym in text:
                text = text.replace(pseudonym, original)
        return text

    def _content_messages(self, context: list) -> list:
        roles = {"user", "tool"}
        return [m for m in context if m.get("role") in roles and isinstance(m.get("content"), str)]

    def pre_hook(self, context: list, params: dict) -> list:
        # Every user turn, not just the newest: the client resends the raw
        # (secret: ...) text with the whole history on each request.
        for msg in context:
            if msg.get("role") == "user":
                self._mark_message(msg)
        if self._marked:
            for msg in context:
                self._scrub_message(msg)
        for msg in self._content_messages(context):
            sanitized = self._sanitize(msg["content"])
            if sanitized != msg["content"]:
                msg["content"] = sanitized
        return context

    def post_hook(self, context: list) -> list:
        if not context:
            return context
        last = context[-1]
        content = last.get("content")
        if content and isinstance(content, str):
            self._announce_restored(content, "the reply")
            last["content"] = self._reveal_marked(self._restore(content))
        tool_calls = last.get("tool_calls")
        if tool_calls:
            for tc in tool_calls:
                func = tc.get("function", {})
                args = func.get("arguments", "")
                if args and isinstance(args, str):
                    self._announce_restored(args, f"a call to {func.get('name') or 'a tool'}")
                    func["arguments"] = self._reveal_marked(self._restore(args), in_json=True)
        return context

    # -- live page -------------------------------------------------------------
    # Only ever the kind of secret and the stand-in the model saw. The real
    # value never goes into an event: the Live page is for seeing that it
    # works, and must not become a place the secret shows up.

    ui_page = "secrets_protector.html"

    def ui_action(self, data):
        if isinstance(data, dict) and data.get("action") == "clear":
            self.live_feed.clear()
        return {"ok": True}

    KIND_LABELS = {
        "openai_proj_key": "an OpenAI key", "openai_key": "an OpenAI key",
        "anthropic_key": "an Anthropic key", "aws_key": "an AWS key",
        "jwt": "a JWT", "github_token": "a GitHub token",
        "google_api_key": "a Google API key", "stripe_key": "a Stripe key",
        "bearer_token": "a bearer token", "slack_token": "a Slack token",
        "database_url": "a database URL", "private_key": "a private key",
        "email": "an email address", "phone": "a phone number",
        "ssn": "a Social Security number", "ip_address": "an IP address",
        "credit_card": "a card number", "marked": "a value you marked",
    }

    def _announce_hidden(self, kind: str, stand_in: str) -> None:
        label = self.KIND_LABELS.get(kind, "a " + kind.replace("_", " "))
        self.publish({"event": "hidden", "kind": kind, "label": label,
                      "stand_in": stand_in, "ts": time.time()})

    def _announce_restored(self, text: str, where: str) -> None:
        found = [p for p in self._reverse if p in text]
        found += [m for m in _MARKER_RE.findall(text) if m in self._marked]
        if found:
            self.publish({"event": "restored", "where": where,
                          "stand_ins": sorted(set(found)), "ts": time.time()})

    def info(self, capabilities: dict) -> dict:
        capabilities["secrets_protection"] = True
        return capabilities

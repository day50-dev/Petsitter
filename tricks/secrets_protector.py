"""Secrets Protector Trick.

Detects and pseudonymizes sensitive information (API keys, tokens, credentials, PII)
before it reaches the model, using format-preserving substitutes, and restores
original values in the response.

Uses a bidirectional vault to maintain consistent pseudonyms throughout a session.
"""

import re
import secrets
from typing import Callable

from src.trick import Trick

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


class SecretsProtectorTrick(Trick):
    """Protect secrets by pseudonymizing them before reaching the model."""

    __brief__ = "Pseudonymizes API keys, tokens, and PII before sending to the model"
    __display_name__ = "Secrets Protector"

    def __init__(self, patterns: list | None = None):
        self._patterns = patterns if patterns is not None else _PATTERNS
        self._vault: dict[tuple[str, str], str] = {}
        self._reverse: dict[str, str] = {}
        self._counters: dict[str, int] = {}

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
            last["content"] = self._restore(content)
        tool_calls = last.get("tool_calls")
        if tool_calls:
            for tc in tool_calls:
                func = tc.get("function", {})
                args = func.get("arguments", "")
                if args and isinstance(args, str):
                    func["arguments"] = self._restore(args)
        return context

    def info(self, capabilities: dict) -> dict:
        capabilities["secrets_protection"] = True
        return capabilities

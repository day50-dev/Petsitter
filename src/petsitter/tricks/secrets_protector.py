"""Hides API keys, passwords and personal details from the model by swapping in stand-ins, then puts the real values back in the reply.

It's easy to paste a config file, a stack trace or a `.env` into a chat without
noticing the API key or database password in it. This trick finds those before
the request leaves petsitter and replaces each with an opaque stand-in like
`gRefWg2D7zO8-sp-3Lw9rTqXc0VbN7mK2pZs4a`. When the model mentions or uses a stand-in, in
its reply or in a tool call, the real value is put back, so your tool still gets
working output. The model is never told a swap happened.

Detected automatically:

- passwords and secrets by the name they sit under, in most syntaxes:
  `"password": "..."`, `api_key = '...'`, `DB_PASSWORD=...`, `secret: ...`
- about 200 kinds of vendor keys and tokens (OpenAI, Anthropic, AWS, GitHub,
  Slack, Stripe, Google...), database URLs and private keys
- anything on your Always hide list (settings), one value per line, matched
  exactly: for what no detector would guess, like a codename or a hostname
- emails, SSNs and card numbers, and phone numbers if you turn them on. Each
  has a switch in the settings: a stand-in doesn't look like what it replaced,
  so hiding one the model needs to recognise can confuse it.

IP addresses are left alone: whether one is local or public, which subnet and
which machine it is, are what make it useful, and a stand-in loses all of that.

Code that only *refers* to a secret (`password = os.environ["DB_PASSWORD"]`,
`token: ${GITHUB_TOKEN}`) is left alone.

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
  resends it each time. The proxy leaves the keyword in place for this
  (`strip_prompt_keyword = False`).
- Detection (`petsitter/secret_scan.py`) combines three sources, since no one
  covers what people paste into a chat: gitleaks' rules for vendor keys
  (bundled in `petsitter/data/`), detect-secrets' keyword detector for values
  named as secrets, and petsitter's own patterns for unquoted `.env`/YAML lines
  and personal details. Only `user` and `tool` messages are scanned;
  overlapping matches keep the earliest, longest. Results are cached per
  message, so a long history isn't rescanned on every request.
- Every stand-in, marked or detected, is an HMAC of the value under a
  per-process key: the same value always gets the same stand-in, and it
  reveals nothing. Hidden values that reappear in clear (restored replies,
  tool calls, tool results that echo them) are swapped out again, including
  their JSON-escaped form.
- `post_hook` puts the real values back in the reply text and in tool call
  arguments (JSON-escaped there). Only the stand-in format is swapped back,
  so nothing else in the reply can be mistaken for one.
- The personal-detail patterns are broad: 16 digits in groups of four read as
  a card number.
"""

import hashlib
import hmac
import json
import re
import secrets
import time

from petsitter import secret_scan
from petsitter.secret_scan import find_secrets
from petsitter.trick import Trick, find_prompt_keyword_patterns, reserved, reserved_id, reserved_pattern

# Every stand-in is a reserved name, gRefWg2D7zO8-sp-<id> ("sp" for Secrets
# Protector), so the way back can find them without guessing, including one the
# model echoes from an older turn. The word claims nothing about the value: with
# "redacted" there, a model decided the secrets were gone and asked for them again.
_MARKER_RE = reserved_pattern("sp")

_MARKER_LEN = len(reserved("sp"))


# A detected value at least this long is hidden wherever it shows up (in
# history the client resends, in escaped JSON), not just where a detector
# finds it. Shorter ones are too likely to be ordinary words.
MIN_SWAP_ANYWHERE = 6

_JSON_UNESCAPE = re.compile(r'\\(["\\/nt])')


def _unescaped_views(text: str, depth: int = 3):
    """text with JSON string escapes undone, once per level of nesting."""
    for _ in range(depth):
        view = _JSON_UNESCAPE.sub(lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), text)
        if view == text:
            return
        yield view
        text = view


def _escaped_forms(value: str, depth: int = 3) -> list[str]:
    """value as it appears plainly and inside one, two, three JSON strings."""
    forms = [value]
    for _ in range(depth):
        nxt = json.dumps(forms[-1])[1:-1]
        if nxt == forms[-1]:
            break
        forms.append(nxt)
    return forms


class SecretsProtectorTrick(Trick):
    """Protect secrets by pseudonymizing them before reaching the model."""

    __brief__ = "Hides passwords, API keys and personal details from the model, and puts them back in its replies"
    __display_name__ = "Secrets Protector"
    __category__ = "Safety & Privacy"
    prompt_keyword = "secret"
    strip_prompt_keyword = False
    # Personal details, each switchable. A stand-in doesn't look like what it
    # replaced, so hiding one the model needs to recognise (a phone number to
    # format or dial) can confuse it.
    config_fields = [
        {"key": "hide_email", "label": "Hide email addresses", "type": "boolean", "default": True,
         "description": "They're often logins."},
        {"key": "hide_phone", "label": "Hide phone numbers", "type": "boolean", "default": False,
         "description": "Can confuse the model, and catches any 10-digit number."},
        {"key": "hide_ssn", "label": "Hide social security numbers", "type": "boolean", "default": True},
        {"key": "hide_credit_card", "label": "Hide card numbers", "type": "boolean", "default": True},
        # Values that are always hidden, wherever they appear, on top of
        # whatever the detectors find. "secret": never shown to a model, even
        # through Expose Petsitter.
        {"key": "always_hide", "label": "Always hide", "type": "lines", "secret": True, "default": "",
         "description": "One per line. Hidden exactly as written, wherever it appears."},
    ]

    def __init__(self):
        # Which of those were found by a detector rather than marked by hand.
        self._detected: set[str] = set()
        # Every hidden value, marked by hand or detected: stand-in -> original. The stand-in is an HMAC of
        # the value, so the same secret gets the same stand-in on every resend
        # of the history without the stand-in revealing anything about it.
        self._key = secrets.token_bytes(32)
        self._marked: dict[str, str] = {}

    # Every stand-in is the same length, so the reply can stream with only
    # that much held back (see reply_window).
    needs_window = _MARKER_LEN

    def _marker(self, value: str) -> str:
        digest = hmac.new(self._key, value.encode(), hashlib.sha256).digest()
        return reserved("sp", reserved_id(digest))

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
            # A short detected value ("test", "admin") would turn every
            # occurrence of a common word into a stand-in, so those are only
            # hidden where a detector finds them. Hand-marked ones always are.
            if marker in self._detected and len(original) < MIN_SWAP_ANYWHERE:
                continue
            for form in _escaped_forms(original):
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
        """A detected value's stand-in: the same opaque marker a hand-marked
        one gets, so the way back only ever swaps something unmistakably ours."""
        marker = self._marker(original)
        if marker not in self._marked:
            self._announce_hidden(secret_type, marker)
            self._detected.add(marker)
        self._marked[marker] = original
        return marker

    def _listed(self) -> list[str]:
        """The Always hide values, longest first (so one that contains
        another wins)."""
        raw = getattr(self, "always_hide", "") or ""
        lines = raw if isinstance(raw, list) else str(raw).splitlines()
        return sorted({v.strip() for v in lines if isinstance(v, str) and v.strip()}, key=len, reverse=True)

    def _find_spans(self, text: str) -> list[tuple[int, int, str, str]]:
        spans = [s for s in find_secrets(text) if self._hiding(s[3])]
        for value in self._listed():
            i = text.find(value)
            while i >= 0:
                spans.append((i, i + len(value), value, "listed"))
                i = text.find(value, i + len(value))
        if not spans:
            return spans
        # earliest first, then longest; anything overlapping an earlier one is dropped
        spans.sort(key=lambda x: (x[0], -(x[1] - x[0])))
        merged, end = [], 0
        for sp in spans:
            if sp[0] >= end:
                merged.append(sp)
                end = sp[1]
        return merged

    def _hiding(self, kind: str) -> bool:
        if kind not in secret_scan.PERSONAL_KINDS:
            return True
        field = next(f for f in self.config_fields if f["key"] == f"hide_{kind}")
        return bool(getattr(self, field["key"], field["default"]))

    def problems(self) -> list[str]:
        return secret_scan.problems()

    def _sanitize(self, text: str) -> str:
        out = self._sanitize_spans(text)
        # JSON inside a JSON string (a tool returning an encoded object, a
        # config in a tool call's arguments) has its quotes escaped, which no
        # detector recognizes: {\"password\":\"...\"}. Look again at the
        # unescaped text, and hide what turns up wherever it appears.
        if "\\" in text:
            found = False
            for view in _unescaped_views(text):
                for _, _, value, kind in self._find_spans(view):
                    if len(value) >= MIN_SWAP_ANYWHERE and not _MARKER_RE.fullmatch(value):
                        self._pseudonym(value, kind)
                        found = True
            if found:
                out = self._hide_marked(out)
        return out

    def _sanitize_spans(self, text: str) -> str:
        if _MARKER_RE.search(text):
            parts = _MARKER_RE.split(text)
            markers = _MARKER_RE.findall(text)
            out = [self._sanitize_spans(parts[0])]
            for marker, part in zip(markers, parts[1:]):
                out.append(marker)
                out.append(self._sanitize_spans(part))
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
        return self._reveal_marked(text)

    def _content_messages(self, context: list) -> list:
        roles = {"user", "tool"}
        return [m for m in context if m.get("role") in roles and isinstance(m.get("content"), (str, list))]

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
            content = msg["content"]
            if isinstance(content, str):
                msg["content"] = self._sanitize(content)
            else:
                # a list of parts: the text ones (images and the like pass as is)
                for block in content:
                    if isinstance(block, dict) and isinstance(block.get("text"), str):
                        block["text"] = self._sanitize(block["text"])
        return context

    def post_hook(self, context: list) -> list:
        if not context:
            return context
        last = context[-1]
        content = last.get("content")
        if content and isinstance(content, str):
            self._announce_restored(content, "the reply")
            last["content"] = self._reveal_marked(content)
        tool_calls = last.get("tool_calls")
        if tool_calls:
            for tc in tool_calls:
                func = tc.get("function", {})
                args = func.get("arguments", "")
                if args and isinstance(args, str):
                    self._announce_restored(args, f"a call to {func.get('name') or 'a tool'}")
                    func["arguments"] = self._reveal_marked(args, in_json=True)
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
        "ssn": "a Social Security number",
        "credit_card": "a card number", "marked": "a value you marked",
        "listed": "a value on your Always hide list",
        "credential": "a password or secret",
    }

    def _announce_hidden(self, kind: str, stand_in: str) -> None:
        # gitleaks rule ids read well enough as words: "a github pat"
        label = self.KIND_LABELS.get(kind) or "a " + kind.removeprefix("gitleaks:").replace("_", " ").replace("-", " ")
        self.publish({"event": "hidden", "kind": kind, "label": label,
                      "stand_in": stand_in, "ts": time.time()})

    def _announce_restored(self, text: str, where: str) -> None:
        found = [m for m in _MARKER_RE.findall(text) if m in self._marked]
        if found:
            self.publish({"event": "restored", "where": where,
                          "stand_ins": sorted(set(found)), "ts": time.time()})

    def info(self, capabilities: dict) -> dict:
        capabilities["secrets_protection"] = True
        return capabilities

"""Rewrites your message into a polite, professional tone before the model sees it, without changing what you're asking for.

Models tend to do worse when a prompt is hostile or full of swearing, even when
the request itself is perfectly reasonable. If you type while frustrated, this
trick quietly cleans up the tone so you still get the model's best effort:

```
You type:        why the hell is this stupid test still failing, fix it
Model receives:  Could you help me understand why this test is still failing and fix it?
```

Code blocks, file paths, commands and technical terms are kept exactly as
written. The original wording never leaves petsitter.

## How to use

Nothing to type. A message is only sent for a rewrite when it might have
swearing, insults or ALL-CAPS SHOUTING in it (shouting used to help with older
models; with current ones it tends to hurt); anything else goes to the model exactly as you wrote
it.

The rewrite goes to a model named `rephraser` if you add one under Models;
otherwise to your `default` model. Any chat model works, no tool support or
uncensored model needed; an uncensored one keeps the most of your urgency
(`qwen3.5-9b-uncensored` works well).

## How it works

- `pre_hook` checks the newest user message against a word list (outside code
  blocks): the classic LDNOOBW list (`data/profanity-en.txt`, CC BY 4.0) plus
  common swearing and name-calling it leaves out, and three or more all-caps
  words in a row. A match only means "worth a
  look": that message goes to the `rephraser` model, asked to swap foul
  language and rudeness for professional language while keeping the
  message's sentiment, intent and urgency.
- That model gets a conversation that has already got going: it was asked for
  a rewrite sent back as a code block, "agreed" (noting it won't swear, but
  will rephrase swearing), and was then handed the draft in a code block. A
  small model given a message as a plain user turn tends to answer or refuse
  it; one that has already agreed to send back only a code block sends back the
  rewrite. Only the code block counts: a reply without one is never sent on,
  and the message goes as written. No tool support is needed. Nothing is
  masked or blocked. Messages that don't match, or are shorter than the
  `min_length` setting (default 12 characters), pass through untouched.
- Every rewrite is cached (original -> rewritten, the 200 most recently used). Your tool resends
  the whole conversation each turn, so earlier messages are swapped from the
  cache: the model keeps seeing the polite versions, with no extra calls.
- Model: the `rephraser` entry in your models if present (an older
  `politeify` entry also works), else `default`.
- If the rewrite call fails or returns nothing, the message is sent unchanged
  (a warning is logged). Each rephrasing, and each failure, shows up on the
  Live tab.
"""

import logging
import re
import threading
from pathlib import Path
from collections import OrderedDict

from petsitter.trick import Trick, callmodel_sync, get_model_config

logger = logging.getLogger("petsitter")

# The model is handed a conversation that has already got going: the user
# asked for a rewrite returned as a code block, the assistant agreed (and
# noted it won't swear, but will rephrase swearing), and the user has just
# pasted the draft. A small model given a message as a plain user turn tends
# to answer it ("Yes, Dick Van Dyke played Bert") or refuse it; one that has
# already agreed to send back only a code block sends back the rewrite. The
# note lets a model that won't produce profanity still rework it.
REWRITE_INSTRUCTION = (
    "can you help me rephrase the following message (the prompt)? While keeping the "
    "sentiments, intentions and urgency of the message intact, swap out foul language "
    "or rude behavior with more professional language, and keep code, file paths and "
    "commands exactly as written. Send it back to me in markdown braces like ```"
)
AGREED = (
    "Sure, tell me what you'd like me to rephrase and I will send it back without any "
    "commentary, as a code block in braces like this ```. As a note, I'm not allowed to "
    "generate profane language, but I'll be happy to rephrase any profanity in ways that "
    "capture its sentiment while maintaining professionalism."
)


def _rewriter_conversation(draft: str) -> list:
    fence = "```"
    while fence in draft:   # so code in the draft can't close the block early
        fence += "`"
    return [
        {"role": "user", "content": REWRITE_INSTRUCTION},
        {"role": "assistant", "content": AGREED},
        {"role": "user", "content": f"here is the original message to rephrase\n{fence}\n{draft}\n{fence}"},
    ]


_FENCED_RE = re.compile(r"(`{3,})[^\n]*\n(.*?)\n?\1", re.S)


def _rephrased(reply: dict) -> str | None:
    """The rewrite: the first code block in the reply, if there is one."""
    m = _FENCED_RE.search(reply.get("content") or "")
    text = m.group(2).strip() if m else ""
    return text or None


# Whether a message is worth sending for a rewrite. It's only a hint: the
# rewriter is free to leave a message alone ("Dick Van Dyke" trips the list, and
# comes back unchanged), so a broad, crude list is fine. Nothing is ever masked
# or blocked here.
#
# The classic LDNOOBW list (data/profanity-en.txt, CC BY 4.0) is mostly sexual
# and crude terms; these add the swearing and name-calling it leaves out.
_EXTRA = ("fuck", "fucking", "fucked", "fucker", "motherfucker", "shit", "shitty", "bullshit",
          "damn", "dammit", "goddamn", "crap", "crappy", "piss", "pissed", "hell", "wtf", "stfu",
          "ffs", "idiot", "idiots", "idiotic", "moron", "moronic", "dumbass", "jackass",
          "imbecile", "cretin", "stupid", "pathetic", "incompetent", "shut up", "loser")


def _load_words() -> re.Pattern:
    words = set(_EXTRA)
    try:
        path = Path(__file__).resolve().parent.parent / "data" / "profanity-en.txt"
        words |= {w.strip().lower() for w in path.read_text(encoding="utf-8").splitlines() if w.strip()}
    except OSError:
        logger.warning("politeify: word list missing; using the short built-in one")
    alts = sorted((re.escape(w) for w in words), key=len, reverse=True)
    return re.compile(r"(?i)(?<![\w-])(?:" + "|".join(alts) + r")(?![\w-])")


_RUDE_RE = _load_words()
# SHOUTING: three or more all-caps words in a row. It used to help with older
# models; with current ones it tends to hurt, so it's worth a rewrite too.
# (A run of acronyms trips it as well; the rewriter leaves those alone.)
_SHOUT_RE = re.compile(r"\b[A-Z][A-Z']+(?:[\s,.!?;:-]+[A-Z][A-Z']+){2,}\b")
# Code is left alone: fenced blocks and `inline` spans.
_CODE_RE = re.compile(r"```.*?```|`[^`\n]*`", re.S)


def is_rude(text: str) -> bool:
    """Might this message be worth rephrasing? (Outside code.)"""
    text = _CODE_RE.sub(" ", text)
    return bool(_RUDE_RE.search(text) or _SHOUT_RE.search(text))


class PoliteifyTrick(Trick):
    """Rewrites the user's message to be more polite before it reaches the model."""

    __brief__ = "Rewrites the user's message to be more polite before it reaches the model"
    __display_name__ = "Politeify"
    __category__ = "Output & Style"
    optional_models = ["rephraser"]
    config_fields = [
        {
            "key": "min_length",
            "label": "Minimum length",
            "description": (
                "Skip messages shorter than this many characters -- not "
                "worth a round trip, and keeps bare keywords/commands intact."
            ),
            "type": "number",
            "default": 12,
        },
    ]

    # Rewrites already made, original -> rewritten. The client resends the
    # whole conversation every turn, so without this every earlier message
    # would reach the model in its original wording again (or cost a fresh
    # rewrite each time). Bounded, oldest dropped first.
    CACHE_SIZE = 200

    def __init__(self, min_length: int = 12):
        self.min_length = min_length
        self._cache: OrderedDict[str, str] = OrderedDict()
        self._cache_lock = threading.Lock()

    def pre_hook(self, context: list, params: dict) -> list:
        last = self._last_user_index(context)
        if last is None:
            return context
        minimum = int(self.min_length or 0)
        for idx, msg in enumerate(context):
            if msg.get("role") != "user":
                continue
            original = msg.get("content")
            if not isinstance(original, str) or not is_rude(original):
                continue   # civil messages go as written
            cached = self._cached(original)
            if cached is None and idx == last and len(original.strip()) >= minimum:
                # Only the newest message costs a rewrite; older ones that
                # aren't cached came from before this trick was on, and stay.
                cached = self._rewrite(original)
            if cached and cached != original:
                context[idx] = {**msg, "content": cached}
        return context

    def _cached(self, original: str) -> str | None:
        with self._cache_lock:
            hit = self._cache.get(original)
            if hit is not None:
                self._cache.move_to_end(original)
            return hit

    def _rewrite(self, original: str) -> str | None:
        cfg = self._model_config()
        try:
            reply = callmodel_sync(
                _rewriter_conversation(original),
                model_url=cfg.get("url") or "",
                model_name=cfg.get("model") or "",
                api_key=cfg.get("key") or "",
            )[-1]
        except Exception as e:
            logger.warning("politeify: rewrite failed, passing message through unchanged: %s", e)
            self.report("Couldn't rephrase a message; sent it as written", error=str(e)[:200])
            return None
        rewritten = _rephrased(reply)
        if not rewritten:
            # No code block: it answered or refused instead. Never send that on.
            said = (reply.get("content") or "").strip()
            self.report("The rephraser didn't rewrite a message; sent it as written",
                        draft=original[:300], it_said=said[:300])
            return None
        with self._cache_lock:
            self._cache[original] = rewritten
            while len(self._cache) > self.CACHE_SIZE:
                self._cache.popitem(last=False)
        if rewritten != original:
            self.report("Rephrased a message to be more polite",
                        before=original[:300], after=rewritten[:300])
        return rewritten

    def info(self, capabilities: dict) -> dict:
        capabilities["politeify"] = True
        return capabilities

    # -- internal -------------------------------------------------------

    @staticmethod
    def _model_config() -> dict:
        """The "rephraser" model if one is set up (or an older "politeify"
        entry), else "default"."""
        for key in ("rephraser", "politeify"):
            try:
                return get_model_config(key)
            except KeyError:
                continue
        return get_model_config("default")

    @staticmethod
    def _last_user_index(context: list) -> int | None:
        for i in range(len(context) - 1, -1, -1):
            if context[i].get("role") == "user":
                return i
        return None

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
it. The rewrite removes them even where you quote them ("what do you make of:
..."), and leaves a message alone if nothing in it was actually rude.

To send the rewrite to a separate, cheaper model, add a `politeify` entry to your
models; otherwise it uses the `default` model.

## How it works

- `pre_hook` checks the newest user message against a word list (outside code
  blocks): the classic LDNOOBW list (`data/profanity-en.txt`, CC BY 4.0) plus
  common swearing and name-calling it leaves out, and three or more all-caps
  words in a row. A match only means "worth a
  look": that message gets one `callmodel_sync` call with a rewrite-only
  instruction, and the model decides what, if anything, to change. Nothing is
  masked or blocked. Messages that don't match, or are shorter than the
  `min_length` setting (default 12 characters), pass through untouched.
- Every rewrite is cached (original -> rewritten, last 1000). Your tool resends
  the whole conversation each turn, so earlier messages are swapped from the
  cache: the model keeps seeing the polite versions, with no extra calls.
- Model config: the `politeify` modelset entry if present, else `default`.
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

REWRITE_INSTRUCTION = (
    "Rewrite the following message to be polite and professional in tone. "
    "Remove every swear word, slur and insult wherever it appears, including "
    "inside quotation marks, examples, or text the message asks about: describe "
    "such words generically instead (for example \"an insult with profanity\"), "
    "never repeat them. Write ALL-CAPS shouting in normal case (acronyms and "
    "identifiers stay as they are). Keep the meaning, intent, and every factual or technical "
    "detail unchanged -- do not answer it, comment on it, soften the actual "
    "request, or add anything of your own. Preserve code blocks, file paths, "
    "commands, and technical terms exactly as written. Reply with ONLY the "
    "rewritten message and nothing else -- no preamble, no quotes around it. If "
    "nothing in it is actually rude, reply with the message exactly as it is."
)

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
    CACHE_SIZE = 1000

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
            rewritten_ctx = callmodel_sync(
                [{"role": "system", "content": REWRITE_INSTRUCTION}],
                original,
                model_url=cfg.get("url") or "",
                model_name=cfg.get("model") or "",
                api_key=cfg.get("key") or "",
            )
        except Exception as e:
            logger.warning("politeify: rewrite failed, passing message through unchanged: %s", e)
            self.report("Couldn't rephrase a message; sent it as written", error=str(e)[:200])
            return None
        rewritten = rewritten_ctx[-1].get("content", "").strip() if rewritten_ctx else ""
        if not rewritten:
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
        """A dedicated "politeify" modelset entry if one exists, else "default"."""
        try:
            return get_model_config("politeify")
        except KeyError:
            return get_model_config("default")

    @staticmethod
    def _last_user_index(context: list) -> int | None:
        for i in range(len(context) - 1, -1, -1):
            if context[i].get("role") == "user":
                return i
        return None

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

Nothing to type. Every message you send in the trickset is rewritten. Messages
shorter than the `min_length` setting (default 12 characters) are skipped, so
short replies like "yes" or a bare keyword pass through untouched.

To send the rewrite to a separate, cheaper model, add a `politeify` entry to your
models; otherwise it uses the `default` model.

## How it works

- `pre_hook`: the newest user message (string content only) gets one
  `callmodel_sync` call with a rewrite-only instruction.
- Every rewrite is cached (original -> rewritten, last 1000). Your tool resends
  the whole conversation each turn, so earlier messages are swapped from the
  cache: the model keeps seeing the polite versions, with no extra calls.
- Model config: the `politeify` modelset entry if present, else `default`.
- If the rewrite call fails or returns nothing, the message is sent unchanged
  (a warning is logged). Each rephrasing, and each failure, shows up on the
  Live tab.
"""

import logging
import threading
from collections import OrderedDict

from petsitter.trick import Trick, callmodel_sync, get_model_config

logger = logging.getLogger("petsitter")

REWRITE_INSTRUCTION = (
    "Rewrite the following message to be polite and professional in tone. "
    "Keep its meaning, intent, and every factual or technical detail "
    "completely unchanged -- do not answer it, comment on it, soften the "
    "actual request, or add anything of your own. Preserve code blocks, "
    "file paths, commands, and technical terms exactly as written. Reply "
    "with ONLY the rewritten message and nothing else -- no preamble, no "
    "quotes around it."
)


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
            if not isinstance(original, str) or len(original.strip()) < minimum:
                continue
            cached = self._cached(original)
            if cached is None and idx == last:
                # Only the newest message costs a rewrite; anything older that
                # isn't cached came from before this trick was on, and stays.
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

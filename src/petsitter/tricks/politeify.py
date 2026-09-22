"""Politeify trick.

Rewrites the user's outgoing message to be polite and professional before it
reaches the backend model. Models tend to perform worse when a prompt is
hostile or profane, even when the actual request is perfectly reasonable --
this launders the tone without touching the ask itself.

The rewrite is one extra, one-shot call. It can go to a dedicated
"politeify" entry in the modelset (its own host/model/key, so the rewrite
never has to touch the same backend as the real request), or, if no such
entry is configured, to the same "default" model the real request is about
to use. Either way, the rewritten text is what actually gets sent upstream;
the original wording never leaves petsitter, and nothing about the rewrite
is visible in the model's response.
"""

import logging

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

    def __init__(self, min_length: int = 12):
        self.min_length = min_length

    def pre_hook(self, context: list, params: dict) -> list:
        idx = self._last_user_index(context)
        if idx is None:
            return context

        original = context[idx].get("content")
        if not isinstance(original, str) or len(original.strip()) < int(self.min_length or 0):
            return context

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
            return context

        rewritten = rewritten_ctx[-1].get("content", "").strip() if rewritten_ctx else ""
        if rewritten:
            context[idx] = {**context[idx], "content": rewritten}
        return context

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

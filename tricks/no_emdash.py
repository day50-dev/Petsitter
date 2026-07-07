"""No Em-Dash trick.

Modifies the system prompt to ask the model not to use em-dashes,
and post-filters responses to replace any em-dashes with hyphens.
"""

from src.trick import Trick


EMDASH = "\u2014"


class NoEmDashTrick(Trick):
    """Replace em-dashes with hyphens in model output."""

    __brief__ = "Replaces em-dashes with hyphens in model responses"
    __display_name__ = "No Em-Dash"

    def system_prompt(self, to_add: str) -> str:
        """Add instruction to avoid em-dashes."""
        return (
            "Do NOT use em-dashes (the long dash character). "
            "Use a regular hyphen (-) instead."
        )

    def post_hook(self, context: list) -> list:
        """Replace any em-dashes with hyphens."""
        if not context:
            return context

        last = context[-1]
        content = last.get("content", "")
        if EMDASH in content:
            content = content.replace(EMDASH, "-")
            last["content"] = content

        return context

    def info(self, capabilities: dict) -> dict:
        capabilities["no_emdash"] = True
        return capabilities

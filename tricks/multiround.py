"""Multi-round cross-validation and thinking trick.

Activate by including "multiround" in your message.
"""

from src.trick import Trick, callmodel


class MultiRoundTrick(Trick):
    """Only activates when user includes "multiround" in their message."""

    keywords = ["multiround"]

    def system_prompt(self, to_add: str) -> str:
        return (
            "Think through this step-by-step. Then critique your own reasoning. "
            "Then produce a final, polished answer."
        )

    def post_hook(self, context: list) -> list:
        first_pass = context[-1]["content"]
        context = callmodel(
            context,
            "Critique your previous response. Identify flaws, "
            "edge cases, or missing details. Then produce an improved version.",
        )
        revised = context[-1]["content"]
        context[-1]["content"] = (
            f"<first_pass>\n{first_pass}\n</first_pass>\n\n"
            f"<revised>\n{revised}\n</revised>"
        )
        return context

"""Multi-model consultant trick.

Two models cross-validate and improve each other's responses through
iterative refinement and voting. Uses "default" as model1 and
"consultant" as model2 from the modelset.

Flow per round:
  1. model1's response (from proxy) is sent to model2 for improvement
  2. model2 generates a fresh response to the original prompt
  3. model1 improves model2's fresh response
  4. Both models vote on the two improved outputs
  5. If they agree, return the winner; if not, repeat once more
  6. On second disagreement, randomly pick one as fallback
"""

import random

from src.trick import Trick, callmodel_sync, get_model_config


class MultiConsultTrick(Trick):
    """Cross-validates responses between two models through iterative refinement and voting."""

    __brief__ = "Two models iteratively improve and vote on each other's responses"
    __display_name__ = "Multi-Model Consultant"
    required_models = ["default", "consultant"]

    def __init__(self, max_rounds: int = 2):
        self.max_rounds = max_rounds

    def post_hook(self, context: list) -> list:
        model1_response = context[-1].get("content", "")
        if not model1_response:
            return context

        user_msg = self._get_user_message(context)
        if not user_msg:
            return context

        cfg_a = get_model_config("default")
        cfg_b = get_model_config("consultant")

        improved_by_b = ""
        improved_by_a = ""

        for _round in range(self.max_rounds):
            improved_by_b = self._improve(cfg_b, model1_response)
            fresh_b = self._generate(cfg_b, user_msg)
            improved_by_a = self._improve(cfg_a, fresh_b)

            vote_a = self._vote(cfg_a, improved_by_b, improved_by_a)
            vote_b = self._vote(cfg_b, improved_by_b, improved_by_a)

            if vote_a == vote_b:
                winner = improved_by_b if vote_a == "A" else improved_by_a
                context[-1]["content"] = winner
                return context

        context[-1]["content"] = random.choice([improved_by_b, improved_by_a])
        return context

    # -- model interaction helpers -------------------------------------------

    def _generate(self, cfg: dict, user_msg: str) -> str:
        ctx = [{"role": "user", "content": user_msg}]
        result = callmodel_sync(ctx, model_url=cfg["url"], model_name=cfg["model"])
        return result[-1].get("content", "") if result else ""

    def _improve(self, cfg: dict, response: str) -> str:
        ctx = [
            {
                "role": "system",
                "content": (
                    "Improve the following response. Make it more accurate, "
                    "complete, and well-structured. Return only the improved "
                    "response with no preamble or explanation."
                ),
            },
            {"role": "user", "content": response},
        ]
        result = callmodel_sync(ctx, model_url=cfg["url"], model_name=cfg["model"])
        return result[-1].get("content", "") if result else response

    def _vote(self, cfg: dict, option_a: str, option_b: str) -> str:
        ctx = [
            {
                "role": "system",
                "content": (
                    "You are a judge comparing two responses to the same question. "
                    "Decide which is better. Respond with ONLY the letter A or B."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Response A:\n{option_a}\n\n"
                    f"Response B:\n{option_b}"
                ),
            },
        ]
        result = callmodel_sync(ctx, model_url=cfg["url"], model_name=cfg["model"])
        content = result[-1].get("content", "").strip().upper() if result else ""
        if "A" in content and "B" not in content:
            return "A"
        if "B" in content and "A" not in content:
            return "B"
        return random.choice(["A", "B"])

    # -- context helpers -----------------------------------------------------

    @staticmethod
    def _get_user_message(context: list) -> str:
        for msg in reversed(context):
            if msg.get("role") == "user" and isinstance(msg.get("content"), str):
                return msg["content"]
        return ""

    def info(self, capabilities: dict) -> dict:
        capabilities["multi_consult"] = True
        return capabilities

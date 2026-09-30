"""Has two different models improve and judge each other's answers, and returns the one they agree is better.

Every model has blind spots. Asking a second model to review and rewrite the
first one's answer, and letting both vote on the result, tends to catch mistakes
either would make alone. The cost is several extra model calls per reply, so it
suits questions where quality matters more than speed.

## How to use

You need a second model under the name `consultant` (the first is your usual
`default` model):

```bash
pet model consultant url http://localhost:11434 --trickset consult
pet model consultant model qwen3:8b --trickset consult
```

After that, nothing to type: every reply in the trickset goes through the
process.

## How it works

`post_hook`, per round (up to `max_rounds`, constructor arg, default 2):

1. The default model's reply is improved by the consultant.
2. The consultant writes its own fresh answer to your last message.
3. The default model improves that fresh answer.
4. Both models vote "A" or "B" between the two improved answers (an unclear vote
   is a coin flip).
5. If the votes agree, the winner replaces the reply. Otherwise another round.

If they never agree, one of the last two candidates is picked at random. The
fresh answer only sees your last message, not the whole conversation. Only `url`
and `model` are read from the model configs.
"""

import random

from petsitter.trick import Trick, callmodel_sync, get_model_config


class MultiConsultTrick(Trick):
    """Cross-validates responses between two models through iterative refinement and voting."""

    __brief__ = "Two models iteratively improve and vote on each other's responses"
    __display_name__ = "Multi-Model Consultant"
    __category__ = "Reasoning & Quality"
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

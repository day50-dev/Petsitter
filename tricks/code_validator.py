"""Code validation trick with self-healing retry.

Validates model-generated code changes by:
1. Asking the model to describe the proposed change
2. Comparing that description against the original user request
3. Retrying with feedback if the descriptions don't match
"""

from src.trick import Trick, callmodel_sync


class CodeValidatorTrick(Trick):
    """Validate code changes through self-description and comparison."""

    __brief__ = "Validates code changes by comparing model description against user request"
    __display_name__ = "Code Validator"

    def __init__(self, max_attempts: int = 3):
        self.max_attempts = max_attempts

    def _get_user_request(self, context: list) -> str:
        """Extract the last user message from context."""
        for msg in reversed(context):
            if msg.get("role") == "user":
                return msg.get("content", "")
        return ""

    def post_hook(self, context: list) -> list:
        """Validate the proposed code change and retry if needed."""
        if not context:
            return context

        last_message = context[-1]
        proposed_change = last_message.get("content", "")

        if not proposed_change:
            return context

        user_request = self._get_user_request(context)

        attempts = 0
        while attempts < self.max_attempts:
            try:
                desc_context = callmodel_sync(
                    [{"role": "system", "content": "You are a code reviewer."}],
                    f"Describe what this code change does:\n\n{proposed_change}",
                )
            except Exception:
                break

            model_description = desc_context[-1].get("content", "")

            try:
                compare_context = callmodel_sync(
                    [{"role": "system", "content": "You are a validation assistant. Compare the two descriptions."}],
                    f"User Request:\n{user_request}\n\n"
                    f"Description of Proposed Code:\n{model_description}\n\n"
                    "Are these two descriptions effectively the same? "
                    "Answer Yes or No, and give reasons.",
                )
            except Exception:
                break

            validation = compare_context[-1].get("content", "")
            verdict = validation.strip().upper()

            if verdict.startswith("YES"):
                break

            attempts += 1
            if attempts >= self.max_attempts:
                break

            context = context[:-1]
            try:
                context = callmodel_sync(
                    context,
                    f"The last proposed change failed the following validation:\n{validation}\n\n"
                    "You must do a new approach. Generate a different code change.",
                )
            except Exception:
                break

            proposed_change = context[-1].get("content", "")

        return context

    def info(self, capabilities: dict) -> dict:
        capabilities["code_validation"] = True
        return capabilities

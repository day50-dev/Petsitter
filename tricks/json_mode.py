"""JSON mode enforcement trick.

Ensures the model returns valid JSON by:
1. Adding instructions to the system prompt
2. Retrying with feedback if the response is not valid JSON
"""

import json

from src import callmodel
from src.trick import Trick


class JsonModeTrick(Trick):
    """Enforce valid JSON output from the model."""

    def __init__(self, max_attempts: int = 3):
        self.max_attempts = max_attempts

    def system_prompt(self, to_add: str) -> str:
        """Add JSON formatting instructions to system prompt."""
        return (
            "IMPORTANT: Your response must be valid JSON only. "
            "Do not include any explanatory text, markdown formatting, "
            "or code blocks. Respond with raw JSON."
        )

    def post_hook(self, context: list) -> list:
        """Validate JSON and retry if invalid."""
        if not context:
            return context

        last_message = context[-1]
        content = last_message.get("content", "")

        # Try to parse as JSON
        attempts = self.max_attempts
        while attempts > 0:
            try:
                # Strip markdown code blocks if present
                if content.startswith("```"):
                    # Extract JSON from markdown block
                    lines = content.split("\n")
                    if lines[0].startswith("```"):
                        content = "\n".join(lines[1:-1]) if lines[-1] == "```" else "\n".join(lines[1:])
                
                json.loads(content)
                break  # Valid JSON
            except (json.JSONDecodeError, IndexError):
                attempts -= 1
                if attempts == 0:
                    # Out of retries, return as-is
                    break
                
                # Retry with feedback
                context = callmodel(
                    context,
                    "Your response was not valid JSON. Please respond with valid JSON only, "
                    "no markdown, no explanatory text.",
                )
                last_message = context[-1]
                content = last_message.get("content", "")

        # Update the last message with cleaned content
        context[-1]["content"] = content
        return context

    def info(self, capabilities: dict) -> dict:
        """Declare JSON mode capability."""
        capabilities["json_mode"] = True
        return capabilities

"""Makes the model answer with valid JSON only, and retries when it doesn't.

Useful when a script or another program reads the model's output. Small models
love to wrap JSON in a Markdown code fence or add "Here is your JSON:" in front,
which breaks a parser. With this trick:

```
Before:  Sure! Here you go:  ```json {"ok": true} ```
After:   {"ok": true}
```

## How to use

Nothing to type. Every response in the trickset is held to the rule, so put it in
a trickset used only by the tool that expects JSON.

## How it works

- `system_prompt`: tells the model to respond with raw JSON, no prose, no
  Markdown, no code blocks.
- `post_hook`: if the reply starts with a Markdown code fence, the fence lines are
  stripped. The result is parsed with `json.loads`; on failure the model is told
  "Your response was not valid JSON" and asked again (`callmodel_sync`, default
  model).
- Up to `max_attempts` tries in total (constructor arg, default 3). If it still
  isn't valid JSON, the last reply is passed through unchanged.
"""

import json

from petsitter.trick import Trick, callmodel_sync


class JsonModeTrick(Trick):
    """Enforce valid JSON output from the model."""

    __brief__ = "Enforces valid JSON output with automatic retry on failure"
    __display_name__ = "JSON Mode"
    __category__ = "Reasoning & Quality"

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
                context = callmodel_sync(
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

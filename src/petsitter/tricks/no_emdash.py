"""Stops the model from using em-dashes, replacing any that slip through with a plain hyphen.

Many people find the long dash (U+2014, the em-dash) a telltale sign of AI-written text, and some
terminals, commit hooks and style guides don't want it either. This trick asks
the model not to use it and cleans up any that appear anyway:

```
Before:  The fix is simple\u2014just retry.
After:   The fix is simple-just retry.
```

## How to use

Nothing to type or configure. Add it to a trickset and it applies to every
response.

## How it works

- `system_prompt`: asks the model to use a regular hyphen instead of em-dashes.
- `post_hook`: replaces every U+2014 in the final assistant message with `-`.
  It does not touch en-dashes (U+2013) or tool call arguments.
"""

from petsitter.trick import Trick


EMDASH = "\u2014"


class NoEmDashTrick(Trick):
    """Replace em-dashes with hyphens in model output."""

    __brief__ = "Replaces em-dashes with hyphens in model responses"
    needs_window = 1   # one character at a time; the reply streams
    __display_name__ = "No Em-Dash"
    __category__ = "Output & Style"

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
        if isinstance(content, str) and EMDASH in content:
            n = content.count(EMDASH)
            last["content"] = content.replace(EMDASH, "-")
            self.report(f"Replaced {n} em-dash{'es' if n != 1 else ''} in a reply")

        return context

    def info(self, capabilities: dict) -> dict:
        capabilities["no_emdash"] = True
        return capabilities

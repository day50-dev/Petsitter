"""Makes the model critique its own first answer and write an improved version, when you ask for it.

Good for tricky questions where a first draft is often wrong: the model answers,
then reviews that answer for flaws and missed edge cases, then rewrites it. You
see both drafts, so you can tell what changed.

## How to use

Include the word `multiround` anywhere in your message. It only applies to that
message, and the word is removed before the model sees it:

```
multiround: why does my binary search loop forever on [1, 2]?
```

The reply comes back in two parts:

```
<first_pass> ...original answer... </first_pass>
<revised> ...critiqued and improved answer... </revised>
```

## How it works

- Keyword-activated (`keywords = ["multiround"]`): the proxy only activates it
  when the last user message contains the word, and strips it.
- `system_prompt` asks for step-by-step thinking, self-critique, and a polished
  answer. `post_hook` then asks the model to critique and improve its reply, and
  wraps both versions in `<first_pass>` / `<revised>` tags.
- Known issue: `post_hook` calls the async `callmodel` without awaiting it, so
  the critique round raises instead of running; it should use `callmodel_sync`.
"""

from petsitter.trick import Trick, callmodel


class MultiRoundTrick(Trick):
    """Only activates when user includes "multiround" in their message."""

    __brief__ = "Step-by-step reasoning with self-critique and revision"
    __display_name__ = "Multi-Round"
    __category__ = "Reasoning & Quality"
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

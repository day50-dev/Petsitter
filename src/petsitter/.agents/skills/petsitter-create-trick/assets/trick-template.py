"""<One sentence: what this extension does, for the person installing it. Shown as its tagline.>

<A short paragraph: the problem it solves and why someone would want it.>

## How to use

<What the user does: "Install it; nothing to type", or the keyword to type,
or which settings to change.>

## How it works

- <Which hooks it uses and what each one does.>
"""

from petsitter.trick import Trick


class <Name>Trick(Trick):
    """<class docstring describing the trick behavior>"""

    __brief__ = "<one-line summary shown in the dashboard>"
    __display_name__ = "<human-readable name>"
    __category__ = "<grouping label, e.g. 'Tool Calling' -- reuse an existing category from src/petsitter/tricks/ if one fits>"

    # Optional: run only when one of these words is in the user's message.
    # keywords = ["<keyword>"]

    # Optional: settings. The dashboard shows a form; values arrive as
    # attributes on self. Types: "text", "number", "boolean", "path".
    # config_fields = [
    #     {"key": "limit", "label": "Limit", "type": "number", "default": 3,
    #      "description": "<help text shown under the field>"},
    # ]

    # How much of a streamed reply post_hook must see at once: -1 the whole
    # reply (held until complete), 0 none (it only looks; runs after the reply
    # is sent), N the last N characters (the reply streams; post_hook must then
    # be right on any stretch of it and change nothing if run again).
    needs_window = -1

    def system_prompt(self, to_add: str) -> str:
        """<what instructions this adds to the system prompt>"""
        # Return "" to leave unchanged; return a string to append
        return ""

    def pre_hook(self, context: list, params: dict) -> list:
        """<what this modifies before the model call>"""
        # Mutate params["tools"] to change the tools the model sees.
        # A message's content may be a str, None, or a list of parts.
        # Per-request state goes in request_meta(), never on self.
        return context

    def post_hook(self, context: list) -> list:
        """<what this checks/transforms after the model responds>"""
        # context[-1] is the assistant's reply.
        # When the trick actually changes something, say so on its Live tab:
        # self.report("Fixed 2 things in a reply")
        return context

    def info(self, capabilities: dict) -> dict:
        """<what capabilities this trick declares>"""
        # capabilities["your_key"] = True
        return capabilities

    # Optional: what's wrong with the setup, as sentences saying how to fix it.
    # The dashboard shows a "!" on the extension and its channel.
    # def problems(self) -> list[str]:
    #     return []

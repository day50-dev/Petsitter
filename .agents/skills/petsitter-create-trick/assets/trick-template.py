"""Trick: <name> - <brief description>.

<Describe what this trick does: which hooks it uses and why.>
"""

from src.trick import Trick


class <Name>Trick(Trick):
    """<description>"""

    def __init__(self):
        # Store per-instance state here
        pass

    def system_prompt(self, to_add: str) -> str:
        """<what instructions this adds to the system prompt>"""
        # Return "" to leave unchanged; return a string to append
        return ""

    def pre_hook(self, context: list, params: dict) -> list:
        """<what this modifies before the model call>"""
        # Mutate params["tools"] to inject tool definitions
        # Modify or append to context to add messages
        return context

    def post_hook(self, context: list) -> list:
        """<what this checks/transforms after the model responds>"""
        # context[-1] is the assistant's response
        # Validate output, retry with callmodel, detect tool calls
        return context

    def info(self, capabilities: dict) -> dict:
        """<what capabilities this trick declares>"""
        # capabilities["your_key"] = True
        return capabilities

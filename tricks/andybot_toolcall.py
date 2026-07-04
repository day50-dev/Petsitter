"""Conversational tool calling via the ANDYBOT persona (experimental).

Works well with small models, especially older ones that struggle with
structured JSON output. Instead of requiring a JSON-RPC blob, the model
simply says "DEAR ANDYBOT, <FUNCTION>" and ANDYBOT asks for each required
parameter in dialogue.

This is an **experimental** alternative to the JSON-RPC approach in
``tool_call.py``. For a more advanced version with inline-arg parsing,
confusion recovery, and state management, see ``conversational_tool.py``.
"""

import json
import re
import secrets

from src.context import set_last_message_content
from src.trick import Trick, callmodel_sync


def _find_tool(content: str, tools: list) -> str | None:
    """Extract the tool name from a 'DEAR ANDYBOT <FUNC>' message.

    Normalises both sides (strip underscores, uppercase) so small models
    don't need to match the exact spelling.
    """
    match = re.search(r"DEAR\s+ANDYBOT\s*,?\s*(\w+)", content, re.IGNORECASE)
    if not match:
        return None
    wanted = match.group(1).upper().replace("_", "")
    for tool in tools:
        name = tool.get("function", {}).get("name", "")
        if name.upper().replace("_", "") == wanted:
            return name
    return None


def _required_params(tools: list, tool_name: str) -> list[tuple[str, str]]:
    """Return [(name, description), …] for a tool's required parameters."""
    for tool in tools:
        func = tool.get("function", {})
        if func.get("name") != tool_name:
            continue
        params = func.get("parameters", {})
        props = params.get("properties", {})
        required = params.get("required", [])
        return [(p, props.get(p, {}).get("description", p)) for p in required]
    return []


def _ask_for_param(context: list, name: str, desc: str) -> tuple[list, str]:
    """Ask the model for a single parameter value via dialogue.

    Returns (updated_context, value).
    """
    question = (
        f"ANDYBOT HAS A QUESTION: Can you provide {desc}? "
        "Respond as succinctly as possible!"
    )
    context = callmodel_sync(context, question)
    return context, (context[-1].get("content") or "").strip()


def _gen_id() -> str:
    return secrets.token_hex(8)


class AndybotToolcallTrick(Trick):
    """Conversational tool calling through the ANDYBOT persona.

    Experimental — works best with small/older models that cannot reliably
    produce structured JSON or handle native ``tool_calls``.
    """

    __brief__ = "Conversational tool calling via ANDYBOT persona (experimental)"
    __display_name__ = "Andybot Toolcall"

    def __init__(self):
        self._tools: list | None = None
        self._has_native_tools = False

    # ── system_prompt ──────────────────────────────────────────────────

    def system_prompt(self, to_add: str) -> str:
        if self._has_native_tools or not self._tools:
            return ""

        tool_list = "\n".join(
            f"- {func.get('name', '').upper()}: {func.get('description', '')}"
            for tool in self._tools
            if (func := tool.get("function", {}))
        )

        return (
            "You are an assistant for a user. You have access to ANDYBOT.\n\n"
            "To ask ANDYBOT to do something, say:\n"
            "DEAR ANDYBOT, <FUNCTION_NAME>\n\n"
            "THINGS YOU CAN ASK ANDYBOT:\n"
            f"{tool_list}\n\n"
            "ANDYBOT will guide you through providing the details."
        )

    # ── pre_hook ───────────────────────────────────────────────────────

    def pre_hook(self, context: list, params: dict) -> list:
        if params.get("tools"):
            self._tools = params["tools"]

        result: list = []
        for msg in context:
            if msg.get("role") == "tool":
                content = msg.get("content", "OK")
                result.append({
                    "role": "user",
                    "content": f"ANDYBOT RESPONDS: {content}",
                })
            elif msg.get("role") == "assistant" and "tool_calls" in msg:
                content = msg.get("content") or ""
                result.append({"role": "assistant", "content": content})
            else:
                result.append(msg)

        if self._tools:
            sp = self.system_prompt("")
            if result and result[0].get("role") == "system":
                result[0]["content"] = sp
            else:
                result.insert(0, {"role": "system", "content": sp})

        return result

    # ── post_hook ──────────────────────────────────────────────────────

    def post_hook(self, context: list) -> list:
        if not context or not self._tools:
            return context

        last = context[-1]
        content = last.get("content") or ""

        # Native support detected — pass through
        if "tool_calls" in last:
            self._has_native_tools = True
            return context

        tool_name = _find_tool(content, self._tools)
        if not tool_name:
            return context

        # Normalise the model message to just the command
        set_last_message_content(context, f"DEAR ANDYBOT {tool_name}")

        params: dict[str, str] = {}
        for pname, pdesc in _required_params(self._tools, tool_name):
            context, value = _ask_for_param(context, pname, pdesc)
            params[pname] = value

        last = context[-1]
        last["tool_calls"] = [
            {
                "id": f"call_{_gen_id()}",
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": json.dumps(params),
                },
            }
        ]
        last["content"] = None

        return context

    # ── info ────────────────────────────────────────────────────────────

    def info(self, capabilities: dict) -> dict:
        capabilities["tools_support"] = True
        return capabilities

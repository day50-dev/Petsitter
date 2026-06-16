import json
import re
import secrets

from src.trick import Trick, callmodel_sync


def get_parameter(context: list, param_name: str, param_desc: str) -> tuple[list, str]:
    """Ask model for a parameter value via ANDYBOT HAS A QUESTION.
    
    Maintains strict user/assistant alternation pattern.
    
    Args:
        context: Current conversation context
        param_name: Name of parameter (for internal use)
        param_desc: Description shown to user/model
        
    Returns:
        (new_context, value) - updated context and the parameter value
    """
    question = f"ANDYBOT HAS A QUESTION: Can you provide {param_desc}? Respond as succinctly and shortly as possible!"
    context = callmodel_sync(context, question)
    value = context[-1].get("content", "").strip()
    return context, value


def parse_tool_request(content: str, tools: list) -> str | None:
    """Parse 'DEAR ANDYBOT <FUNCTION>' from content.
    
    Strips everything after the tool name (handles hallucinated responses).
    
    Returns tool name or None.
    """
    if "DEAR ANDYBOT" not in content.upper():
        return None
    
    match = re.search(r'DEAR\s+ANDYBOT\s*,?\s*(\w+)', content, re.IGNORECASE)
    if not match:
        return None
    
    requested = match.group(1).upper().replace("_", "")
    for tool in tools:
        actual = tool.get("function", {}).get("name", "")
        if actual.upper().replace("_", "") == requested:
            return actual
    return None


def get_required_params(tools: list, tool_name: str) -> list:
    """Get required parameters for a tool.
    
    Returns list of (name, description) tuples.
    """
    for tool in tools:
        func = tool.get("function", {})
        if func.get("name") == tool_name:
            params = func.get("parameters", {})
            required = params.get("required", [])
            props = params.get("properties", {})
            return [(p, props.get(p, {}).get("description", p)) for p in required]
    return []


def gen_id() -> str:
    return secrets.token_hex(8)


class AndybotToolcallTrick(Trick):
    """Conversational tool calling via ANDYBOT pattern.
    
    Flow:
    1. Assistant says: "DEAR ANDYBOT, LIST_MP3S"
    2. Trick truncates message, loops to collect params:
       - User: "ANDYBOT HAS A QUESTION: What path?"
       - Assistant: "~/mp3"
    3. Returns tool_calls to client
    4. Client sends tool result
    5. pre_hook converts tool result to user message
    """

    def __init__(self):
        self._tools = None
        self._has_native_tools = False

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
            "To make a request, say:\n"
            "DEAR ANDYBOT, <FUNCTION_NAME>\n\n"
            "THINGS YOU CAN ASK ANDYBOT:\n"
            f"{tool_list}\n\n"
            "ANDYBOT will guide you through providing parameters.\n"
        )

    def pre_hook(self, context: list, params: dict) -> list:
        """Convert tool results to user messages (ANDYBOT RESPONDS)."""
        if params.get("tools"):
            self._tools = params["tools"]
        
        result = []
        for msg in context:
            if msg.get("role") == "tool":
                content = f"ANDYBOT RESPONDS: {msg.get('content', 'OK')}"
                result.append({"role": "user", "content": content})
            elif msg.get("role") == "assistant" and "tool_calls" in msg:
                result.append({"role": "assistant", "content": msg.get("content") or ""})
            else:
                result.append(msg)
        
        if self._tools:
            if result and result[0].get("role") == "system":
                result[0]["content"] = self.system_prompt("")
            else:
                result.insert(0, {"role": "system", "content": self.system_prompt("")})
        
        return result

    def post_hook(self, context: list) -> list:
        """Detect tool calls and collect parameters via ANDYBOT loop.
        
        Maintains strict assistant/user/assistant/user pattern.
        """
        if not context or not self._tools:
            return context

        last = context[-1]
        content = last.get("content") or ""

        if "tool_calls" in last:
            self._has_native_tools = True
            return context

        tool_name = parse_tool_request(content, self._tools)
        print(tool_name)
        if not tool_name:
            return context

        match = re.search(r'DEAR\s+ANDYBOT\s*,?\s*(\w+)', content, re.IGNORECASE)
        if match:
            truncated_content = f"DEAR ANDYBOT {match.group(1)}"
            last["content"] = truncated_content

        params = {}
        for param_name, param_desc in get_required_params(self._tools, tool_name):
            context, value = get_parameter(context, param_name, param_desc)
            params[param_name] = value

        context.append({
            "tool_calls": {
                "id": f"call_{gen_id()}",
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": json.dumps(params),
                },
            }
        })

        return context

    def info(self, capabilities: dict) -> dict:
        capabilities["tools_support"] = True
        return capabilities

"""XML-based tool calling trick for models without native support.

Uses simple XML-like tags that are easier for smaller models to follow:
- <tool>name</tool> for the tool name
- <args>json</args> for arguments
- <result>output</result> for tool results
"""

import json
import re
from typing import Any

from src.trick import Trick


class XmlToolTrick(Trick):
    """Enable tool calling using XML-style syntax."""

    def __init__(self):
        self._tools_cache = None
        self._model_has_native_tools = False

    def system_prompt(self, to_add: str) -> str:
        """Add XML tool calling instructions to system prompt."""
        if self._model_has_native_tools:
            return ""
        
        return (
            "You have access to tools. To call a tool, use this XML format:\n\n"
            "<tool>tool_name</tool>\n"
            "<args>{\"param\": \"value\"}</args>\n\n"
            "IMPORTANT: After calling a tool, WAIT for the result. Do NOT make up results.\n"
            "The system will respond with actual results in <result>...</result> tags.\n"
            "Then use those results to answer or call another tool.\n\n"
            "Example conversation:\n"
            "User: What files are in ~/mp3?\n"
            "Assistant: <tool>list_mp3s</tool>\n<args>{\"path\": \"~/mp3\"}</args>\n"
            "System: <result>[\"song1.mp3\", \"song2.mp3\"]</result>\n"
            "Assistant: You have song1.mp3 and song2.mp3.\n\n"
            "Do NOT call the same tool twice with the same arguments."
        )

    def pre_hook(self, context: list, params: dict) -> list:
        """Inject tool definitions into context."""
        tools = params.get("tools")
        if tools:
            self._tools_cache = tools
        
        if not tools:
            tools = self._tools_cache
        
        if not tools:
            return context

        # Build simple tool list
        tool_descriptions = []
        for tool in tools:
            func = tool.get("function", {})
            desc = f"- {func.get('name')}: {func.get('description', '')}"
            tool_descriptions.append(desc)
        
        tools_info = "\n".join(tool_descriptions)
        tool_marker = "Available tools:"

        if context and context[0].get("role") == "system":
            if tool_marker not in context[0].get("content", ""):
                if not self._model_has_native_tools:
                    context[0]["content"] += f"\n\n{tool_marker}\n{tools_info}"
                else:
                    context[0]["content"] += f"\n\n{tool_marker}\n{tools_info}"
        else:
            content = f"{tool_marker}\n{tools_info}"
            if not self._model_has_native_tools:
                content = self.system_prompt("") + "\n\n" + content
            context.insert(0, {"role": "system", "content": content})

        return context

    def post_hook(self, context: list) -> list:
        """Detect XML tool calls and convert to OpenAI format."""
        if not context:
            return context

        last_message = context[-1]
        content = last_message.get("content", "")

        # Check for native tool_calls
        if "tool_calls" in last_message:
            self._model_has_native_tools = True
            cleaned_tool_calls = []
            for tc in last_message["tool_calls"]:
                cleaned = {
                    "id": tc.get("id", f"call_{self._generate_id()}"),
                    "type": "function",
                    "function": {
                        "name": tc.get("function", {}).get("name", ""),
                        "arguments": tc.get("function", {}).get("arguments", "{}"),
                    },
                }
                cleaned_tool_calls.append(cleaned)
            last_message["tool_calls"] = cleaned_tool_calls
            return context

        # Parse XML-style tool calls
        tool_calls = self._parse_xml_tool_calls(content)
        if tool_calls:
            last_message["tool_calls"] = [
                {
                    "id": f"call_{self._generate_id()}",
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc["arguments"]),
                    },
                }
                for tc in tool_calls
            ]
            last_message["content"] = None

        return context

    def _parse_xml_tool_calls(self, content: str) -> list:
        """Parse XML-style tool calls from content."""
        tool_calls = []
        
        # Match <tool>name</tool> and <args>{...}</args> pairs
        # Handle both <args>{...}</args> and malformed <args={...}</args>
        tool_pattern = r'<tool>([^<]+)</tool>\s*<args[=]?>?(\{[^<]+\})</args>'
        matches = re.findall(tool_pattern, content, re.DOTALL)
        
        for tool_name, args_json in matches:
            try:
                args = json.loads(args_json.strip())
                tool_calls.append({
                    "name": tool_name.strip(),
                    "arguments": args,
                })
            except json.JSONDecodeError:
                continue
        
        return tool_calls

    def _generate_id(self) -> str:
        """Generate a random ID for tool call."""
        import secrets
        return secrets.token_hex(8)

    def info(self, capabilities: dict) -> dict:
        """Declare tool calling capability."""
        capabilities["tools_support"] = True
        return capabilities

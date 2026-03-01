"""Tool calling trick for models without native support.

Enables tool calling by:
1. Injecting tool definitions into the prompt
2. Parsing model responses for tool call patterns
3. Converting to OpenAI tool_call format
"""

import json
import re
from typing import Any

from src.trick import Trick


class ToolCallTrick(Trick):
    """Enable tool calling for models that don't support it natively."""

    def __init__(self):
        self._tools_cache = None
        self._model_has_native_tools = False

    def system_prompt(self, to_add: str) -> str:
        """Add tool calling instructions to system prompt.
        
        Only adds instructions if the model doesn't have native tool support.
        """
        # Don't add JSONRPC instructions if model has native tool support
        if self._model_has_native_tools:
            return ""
        
        return (
            "IMPORTANT: To call a tool, respond ONLY with a JSON object in this exact format:\n"
            '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"<function_name>","arguments":{<arguments_as_json>}}}\n'
            "Do not include any other text in your response when calling a tool.\n\n"
            "CRITICAL: After receiving tool results, analyze them and decide your next action.\n"
            "Do NOT call the same tool twice with the same arguments. If you already have the information you need, use a different tool or provide your final answer."
        )

    def pre_hook(self, context: list, params: dict) -> list:
        """Inject tool definitions into context if tools are provided."""
        # Cache tools from first request
        tools = params.get("tools")
        if tools:
            self._tools_cache = tools
            # If client sends tools, assume model might have native support
            # We'll detect for sure in post_hook
        
        # Use cached tools if not in current params
        if not tools:
            tools = self._tools_cache
        
        if not tools:
            return context

        tools_info = json.dumps(tools, indent=2)
        tool_marker = "The tools you have access to are:"

        # Build the tool instruction
        tool_instruction = f"\n\n{tool_marker}\n{tools_info}"

        if context and context[0].get("role") == "system":
            # Check if tools already in system prompt to avoid duplication
            if tool_marker not in context[0].get("content", ""):
                # Only add JSONRPC instructions if model doesn't have native support
                if not self._model_has_native_tools:
                    context[0]["content"] += tool_instruction
                else:
                    # For native models, just add tools without JSONRPC instructions
                    context[0]["content"] += f"\n\nAvailable tools:\n{tools_info}"
        else:
            if not self._model_has_native_tools:
                context.insert(0, {"role": "system", "content": tool_instruction})
            else:
                context.insert(0, {"role": "system", "content": f"Available tools:\n{tools_info}"})

        return context

    def post_hook(self, context: list) -> list:
        """Detect tool call patterns and convert to OpenAI format."""
        if not context:
            return context

        last_message = context[-1]
        content = last_message.get("content", "")

        # If model already returned tool_calls, it has native support
        if "tool_calls" in last_message:
            self._model_has_native_tools = True
            
            # Clean up any non-standard fields (like 'index' that Ollama adds)
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

        # Try to detect tool call patterns in content (for models without native support)
        tool_calls = self._parse_all_tool_calls(content)
        if tool_calls:
            # Convert to OpenAI tool_call format
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
            # Remove content since we have tool calls
            last_message["content"] = None

        return context

    def _parse_all_tool_calls(self, content: str) -> list:
        """Parse content for all tool call patterns.
        
        Handles multiple tool calls in a single response.
        """
        tool_calls = []
        
        # First try parsing the entire content as a single JSON object
        try:
            data = json.loads(content.strip())
            if (
                data.get("jsonrpc") == "2.0"
                and data.get("method") == "tools/call"
                and "params" in data
            ):
                params = data["params"]
                tool_calls.append({
                    "name": params.get("name", ""),
                    "arguments": params.get("arguments", {}),
                })
                return tool_calls
        except json.JSONDecodeError:
            pass
        
        # Try parsing line by line for multiple tool calls
        for line in content.strip().split('\n'):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                if (
                    data.get("jsonrpc") == "2.0"
                    and data.get("method") == "tools/call"
                    and "params" in data
                ):
                    params = data["params"]
                    tool_calls.append({
                        "name": params.get("name", ""),
                        "arguments": params.get("arguments", {}),
                    })
            except json.JSONDecodeError:
                continue
        
        # Fallback: try to find JSON objects with balanced braces
        if not tool_calls:
            depth = 0
            start = None
            for i, char in enumerate(content):
                if char == '{':
                    if depth == 0:
                        start = i
                    depth += 1
                elif char == '}':
                    depth -= 1
                    if depth == 0 and start is not None:
                        try:
                            data = json.loads(content[start:i+1])
                            if (
                                data.get("jsonrpc") == "2.0"
                                and data.get("method") == "tools/call"
                                and "params" in data
                            ):
                                params = data["params"]
                                tool_calls.append({
                                    "name": params.get("name", ""),
                                    "arguments": params.get("arguments", {}),
                                })
                        except json.JSONDecodeError:
                            pass
                        start = None
        
        return tool_calls

    def _parse_tool_call(self, content: str) -> dict | None:
        """Parse content for single tool call pattern (legacy)."""
        tool_calls = self._parse_all_tool_calls(content)
        return tool_calls[0] if tool_calls else None

    def _generate_id(self) -> str:
        """Generate a random ID for tool call."""
        import secrets

        return secrets.token_hex(8)

    def info(self, capabilities: dict) -> dict:
        """Declare tool calling capability."""
        capabilities["tools_support"] = True
        return capabilities

"""Conversational tool calling trick for small models.

Uses an iterative conversational approach:
1. Shows only function names and descriptions
2. Collects parameters one-by-one through dialogue
3. Handles confusion by retrying with original context
"""

import json
import re
from typing import Any

from src.trick import Trick


class ConversationalToolTrick(Trick):
    """Enable tool calling through iterative parameter collection."""

    def __init__(self):
        self._tools_cache = None
        self._model_has_native_tools = False
        self._pending_tool = None
        self._original_user_request = ""
        self._collected_params = {}
        self._optional_asked = {}

    def system_prompt(self, to_add: str) -> str:
        """Show only function names and descriptions."""
        if self._model_has_native_tools:
            return ""
        
        tool_list = ""
        if self._tools_cache:
            lines = []
            for tool in self._tools_cache:
                func = tool.get("function", {})
                name = func.get("name", "").upper()
                desc = func.get("description", "")
                lines.append(f"{name}: {desc}")
            if lines:
                tool_list = "\n".join(lines)
        
        return (
            "You are an assistant for a user. You have access to ANDYBOT.\n\n"
            "THINGS YOU CAN ASK ANDYBOT:\n"
            f"{tool_list}\n\n"
            "To make a request, say:\n"
            "DEAR ANDYBOT, <FUNCTION_NAME>\n\n"
            "ANDYBOT will guide you through providing parameters.\n"
            "Wait for ANDYBOT's response before continuing."
        )

    def pre_hook(self, context: list, params: dict) -> list:
        """Inject tool definitions and handle tool results."""
        tools = params.get("tools")
        if tools:
            self._tools_cache = tools
        
        if not tools:
            tools = self._tools_cache
        
        original_request = ""
        for msg in context:
            if msg.get("role") == "user":
                original_request = msg.get("content", "").strip()
                break
        
        if original_request:
            self._original_user_request = original_request

        for i, msg in enumerate(context):
            if msg.get("role") == "tool" and "content" in msg:
                content = msg["content"]
                try:
                    items = json.loads(content)
                    if isinstance(items, list):
                        response = f"THE ORACLE RESPONDS: {', '.join(items)}" if items else "THE ORACLE RESPONDS: (empty result)"
                    elif isinstance(items, dict):
                        response = f"THE ORACLE RESPONDS: {json.dumps(items)}"
                    else:
                        response = f"THE ORACLE RESPONDS: {items}"
                except (json.JSONDecodeError, TypeError):
                    response = f"THE ORACLE RESPONDS: {content}"

                msg["content"] = (
                    f"ANDYBOT RESPONDS: {response}\n\n"
                    f"THE ORIGINAL USER REQUEST: {self._original_user_request}\n\n"
                    "If you need ANDYBOT to do more things, it awaits your request."
                )
        
        if not tools:
            return context

        if context and context[0].get("role") == "system":
            context[0]["content"] = self.system_prompt("")
        else:
            context.insert(0, {"role": "system", "content": self.system_prompt("")})

        return context

    def post_hook(self, context: list) -> list:
        """Detect tool calls and collect parameters iteratively."""
        if not context:
            return context

        last_message = context[-1]
        content = last_message.get("content", "")

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
            self._reset_state()
            return context

        if self._pending_tool:
            return self._handle_param_response(context, content)

        parsed = self._parse_tool_request_with_args(content)
        if parsed:
            tool_name, inline_args = parsed
            return self._start_tool_collection(context, tool_name, inline_args)

        return context

    def _reset_state(self):
        self._pending_tool = None
        self._collected_params = {}
        self._optional_asked = {}

    def _parse_tool_request_with_args(self, content: str) -> tuple | None:
        """Parse 'DEAR ORACLE, <FUNCTION_NAME> [args...]' from content.
        
        Returns (tool_name, inline_args) or None.
        """
        pattern = r'DEAR\s+ANDYBOT,\s*(\w+)(?:\s*(.+))?'
        match = re.search(pattern, content, re.IGNORECASE | re.DOTALL)
        if match:
            requested = match.group(1).upper()
            args_str = match.group(2).strip() if match.group(2) else ""
            tool_name = self._find_tool_name(requested)
            if tool_name:
                inline_args = self._parse_inline_args(args_str, tool_name)
                return (tool_name, inline_args)
        return None

    def _parse_inline_args(self, args_str: str, tool_name: str) -> dict:
        """Parse inline arguments like 'PATH=~/mp3' or '~/mp3' or 'path: ~/mp3'."""
        if not args_str:
            return {}
        
        args = {}
        param_names = self._get_param_names(tool_name)
        
        args_str = args_str.strip()
        
        pattern = r'(\w+)\s*[=:]\s*([^\n,]+)'
        for match in re.finditer(pattern, args_str, re.IGNORECASE):
            key = match.group(1).lower()
            value = match.group(2).strip().strip('"\'')
            for pname in param_names:
                if pname.lower() == key:
                    args[pname] = value
                    break
        
        if not args:
            parts = re.split(r'\s+AND\s+|\s*,\s+', args_str, flags=re.IGNORECASE)
            cleaned = []
            for part in parts:
                part = part.strip()
                if ':' in part:
                    maybe_key, val = part.split(':', 1)
                    maybe_key = maybe_key.strip().lower()
                    found = False
                    for pname in param_names:
                        if pname.lower() == maybe_key:
                            cleaned.append(val.strip().strip('"\''))
                            found = True
                            break
                    if not found:
                        cleaned.append(part.strip().strip('"\''))
                else:
                    cleaned.append(part.strip().strip('"\''))
            
            for i, pname in enumerate(param_names):
                if i < len(cleaned) and cleaned[i]:
                    args[pname] = cleaned[i]
        
        return args

    def _get_param_names(self, tool_name: str) -> list:
        """Get parameter names for a tool."""
        if not self._tools_cache:
            return []
        for tool in self._tools_cache:
            func = tool.get("function", {})
            if func.get("name") == tool_name:
                params = func.get("parameters", {}).get("properties", {})
                return list(params.keys())
        return []

    def _start_tool_collection(self, context: list, tool_name: str, inline_args: dict) -> list:
        """Start collecting parameters for a tool."""
        self._pending_tool = tool_name
        self._collected_params = inline_args.copy()
        self._optional_asked = {k: True for k in inline_args.keys()}
        
        missing = self._get_next_missing_param()
        if not missing:
            return self._finalize_tool_call(context)
        
        last_message = context[-1]
        last_message["content"] = self._build_param_question(missing["param"], missing["description"], missing["required"])
        return context

    def _handle_param_response(self, context: list, content: str) -> list:
        """Handle the model's response to a parameter question."""
        content_stripped = content.strip()
        content_lower = content_stripped.lower()
        
        if content_lower in ["i am confused", "i do not know", "i'm confused", "i don't know", "skip", "none", "", "not required"]:
            missing = self._get_next_missing_param()
            if missing and not missing["required"]:
                if missing["param"] not in self._optional_asked:
                    self._optional_asked[missing["param"]] = True
                    next_missing = self._get_next_missing_param()
                    if next_missing:
                        last_message = context[-1]
                        last_message["content"] = self._build_param_question(
                            next_missing["param"], next_missing["description"], next_missing["required"]
                        )
                        return context
                    else:
                        return self._finalize_tool_call(context)
                else:
                    return self._finalize_tool_call(context)
            elif missing and missing["required"]:
                self._reset_state()
                last_message = context[-1]
                last_message["content"] = (
                    "ANDYBOT SAYS: Let me help you!\n\n"
                    f"THE ORIGINAL USER REQUEST: {self._original_user_request}\n\n"
                    "What would you like to ask ANDYBOT?"
                )
                return context
            else:
                return self._finalize_tool_call(context)
        
        missing = self._get_next_missing_param()
        if missing:
            self._collected_params[missing["param"]] = content_stripped
            if not missing["required"]:
                self._optional_asked[missing["param"]] = True
        
        next_missing = self._get_next_missing_param()
        if not next_missing:
            return self._finalize_tool_call(context)
        
        last_message = context[-1]
        last_message["content"] = self._build_param_question(
            next_missing["param"], next_missing["description"], next_missing["required"]
        )
        return context

    def _get_next_missing_param(self) -> dict | None:
        """Get the next parameter that hasn't been collected.
        
        Returns required params first, then optional ones.
        """
        if not self._pending_tool or not self._tools_cache:
            return None
        
        for tool in self._tools_cache:
            func = tool.get("function", {})
            if func.get("name") == self._pending_tool:
                params_def = func.get("parameters", {})
                required = params_def.get("required", [])
                props = params_def.get("properties", {})
                
                for param in required:
                    if param not in self._collected_params:
                        return {
                            "param": param,
                            "description": props.get(param, {}).get("description", param),
                            "required": True
                        }
                
                for param in props:
                    if param not in self._collected_params and param not in self._optional_asked:
                        return {
                            "param": param,
                            "description": props.get(param, {}).get("description", param),
                            "required": False
                        }
                break
        
        return None

    def _build_param_question(self, param_name: str, description: str, required: bool) -> str:
        """Build the oracle's question for a parameter."""
        req_text = " (required)" if required else " (optional)"
        return (
            f"ANDYBOT WOULD LIKE TO KNOW: {description}{req_text}?\n"
            "Answer as succinctly as possible! ANDYBOT has little patience!\n"
            "You can also say \"I am confused\", \"I do not know\", or \"not required\"."
        )

    def _finalize_tool_call(self, context: list) -> list:
        """Create the final tool call with all collected parameters."""
        last_message = context[-1]
        last_message["tool_calls"] = [
            {
                "id": f"call_{self._generate_id()}",
                "type": "function",
                "function": {
                    "name": self._pending_tool,
                    "arguments": json.dumps(self._collected_params),
                },
            }
        ]
        last_message["content"] = None
        
        self._reset_state()
        
        return context

    def _find_tool_name(self, requested_name: str) -> str | None:
        """Find a tool name from cache matching the requested name."""
        if not self._tools_cache:
            return None
        
        requested_normalized = requested_name.upper().replace("_", "")
        
        for tool in self._tools_cache:
            func = tool.get("function", {})
            actual_name = func.get("name", "")
            actual_normalized = actual_name.upper().replace("_", "")
            
            if requested_normalized == actual_normalized:
                return actual_name
        
        return None

    def _generate_id(self) -> str:
        """Generate a random ID for tool call."""
        import secrets
        return secrets.token_hex(8)

    def info(self, capabilities: dict) -> dict:
        """Declare tool calling capability."""
        capabilities["tools_support"] = True
        return capabilities
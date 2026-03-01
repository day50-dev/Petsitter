"""Conversational tool calling trick for small models.

Uses madlib-style templates that small models can complete:
- "DEAR ORACLE, <REQUEST> ON <ARG1> AND <ARG2>"
- "THE ORACLE RESPONDS: <RESULT>"

This works better because:
- Clear slots to fill (like a form)
- Models are trained on similar patterns
- No complex syntax to remember
"""

import json
import re
from typing import Any

from src.trick import Trick


class ConversationalToolTrick(Trick):
    """Enable tool calling using madlib-style conversational markers."""

    def __init__(self):
        self._tools_cache = None
        self._model_has_native_tools = False

    def system_prompt(self, to_add: str) -> str:
        """Add madlib-style tool calling instructions."""
        if self._model_has_native_tools:
            return ""
        
        # Build tool list with parameter descriptions
        tool_list = ""
        if self._tools_cache:
            examples = []
            for tool in self._tools_cache:
                func = tool.get("function", {})
                name = func.get("name", "").upper()
                params_def = func.get("parameters", {})
                props = params_def.get("properties", {})
                required = params_def.get("required", [])
                
                # Build parameter description
                param_descs = []
                for param_name, param_def in props.items():
                    desc = param_def.get("description", "")
                    req_marker = " (required)" if param_name in required else ""
                    param_descs.append(f"{param_name}{req_marker}: {desc}")
                
                if param_descs:
                    examples.append(f"{name} - {', '.join(param_descs)}")
                else:
                    examples.append(name)
            
            if examples:
                tool_list = "\n\n" + "\n".join(examples)
        
        return (
            "You are an assistant for a user. You have access to a magical oracle.\n\n"
            "To make a request, use this EXACT format:\n"
            "DEAR ORACLE, <REQUEST> ON <PARAMETER>\n\n"
            "IMPORTANT: Only say the request line. DO NOT continue writing after it.\n"
            "Wait for the oracle's response before continuing.\n\n"
            f"THINGS YOU CAN ASK THE ORACLE:{tool_list}\n\n"
            "NEVER call the oracle twice with the same request."
        )

    def pre_hook(self, context: list, params: dict) -> list:
        """Inject tool definitions and format tool results as oracle responses."""
        tools = params.get("tools")
        if tools:
            self._tools_cache = tools
        
        if not tools:
            tools = self._tools_cache
        
        # Find the original user request
        original_request = ""
        for msg in context:
            if msg.get("role") == "user":
                original_request = msg.get("content", "").strip()
                break
        
        # Build tool info for responses
        tool_list_detailed = ""
        tool_names = []
        tool_params = {}
        if tools:
            for tool in tools:
                func = tool.get("function", {})
                name = func.get("name", "").upper()
                tool_names.append(name)
                # Store parameter descriptions
                params_def = func.get("parameters", {})
                props = params_def.get("properties", {})
                required = params_def.get("required", [])
                tool_params[func.get("name", "")] = {
                    "params": props,
                    "required": required,
                }
            
            # Build detailed tool list with parameter descriptions
            examples = []
            for tool in tools:
                func = tool.get("function", {})
                name = func.get("name", "").upper()
                params_def = func.get("parameters", {})
                props = params_def.get("properties", {})
                required = params_def.get("required", [])
                
                param_descs = []
                for param_name, param_def in props.items():
                    desc = param_def.get("description", "")
                    req_marker = " (required)" if param_name in required else ""
                    param_descs.append(f"{param_name}{req_marker}: {desc}")
                
                if param_descs:
                    examples.append(f"{name} - {', '.join(param_descs)}")
                else:
                    examples.append(name)
            
            if examples:
                tool_list_detailed = "\n\n" + "\n".join(examples)

        # Format any tool results as oracle responses with context
        for i, msg in enumerate(context):
            if msg.get("role") == "tool" and "content" in msg:
                # Convert tool result to oracle response format
                content = msg["content"]
                try:
                    # If it's a JSON list, format nicely
                    items = json.loads(content)
                    if isinstance(items, list):
                        if items:
                            response = f"THE ORACLE RESPONDS: {', '.join(items)}"
                        else:
                            response = "THE ORACLE RESPONDS: (empty result)"
                    elif isinstance(items, dict):
                        response = f"THE ORACLE RESPONDS: {json.dumps(items)}"
                    else:
                        response = f"THE ORACLE RESPONDS: {items}"
                except (json.JSONDecodeError, TypeError):
                    response = f"THE ORACLE RESPONDS: {content}"

                # Add context for multi-turn conversations
                msg["content"] = (
                    f"{response}\n\n"
                    f"THE ORIGINAL USER REQUEST: {original_request}\n\n"
                    f"THINGS YOU CAN ASK THE ORACLE:{tool_list_detailed}\n\n"
                    "If you need to ask the oracle to do more things, he eagerly awaits your request."
                )
        
        if not tools:
            return context

        # Regenerate system prompt with current tools
        if context and context[0].get("role") == "system":
            context[0]["content"] = self.system_prompt("")
        else:
            context.insert(0, {"role": "system", "content": self.system_prompt("")})

        return context

    def post_hook(self, context: list) -> list:
        """Detect conversational tool calls and convert to OpenAI format.
        
        Also checks if tool calls have all required parameters.
        If not, adds a clarifying oracle message.
        """
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

        # Parse conversational tool calls
        tool_calls = self._parse_conversational_calls(content)
        if tool_calls:
            # Check if tool calls have all required parameters
            missing_params = self._check_missing_params(tool_calls)
            if missing_params:
                # Add oracle question about missing params - replace content
                oracle_question = self._build_oracle_question(missing_params)
                last_message["content"] = oracle_question
                return context
            
            # Preserve original content for debugging/persistence
            original_content = content
            
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
            # Preserve original content - don't set to null so llcat can replay correctly
            # The tool_calls field takes precedence for OpenAI API

        return context

    def _check_missing_params(self, tool_calls: list) -> list:
        """Check if any tool calls are missing required parameters."""
        missing = []
        for tc in tool_calls:
            tool_name = tc.get("name", "")
            args = tc.get("arguments", {})
            
            # Get required params for this tool
            required = []
            if self._tools_cache:
                for tool in self._tools_cache:
                    func = tool.get("function", {})
                    if func.get("name") == tool_name:
                        params_def = func.get("parameters", {})
                        required = params_def.get("required", [])
                        break
            
            # Check which required params are missing
            for param in required:
                if param not in args or not args[param]:
                    missing.append({
                        "tool": tool_name,
                        "param": param,
                    })
        
        return missing

    def _build_oracle_question(self, missing_params: list) -> str:
        """Build an oracle question for missing parameters."""
        if not missing_params:
            return ""
        
        parts = []
        for mp in missing_params:
            tool_name = mp["tool"]
            param = mp["param"]
            
            # Get parameter description
            desc = ""
            if self._tools_cache:
                for tool in self._tools_cache:
                    func = tool.get("function", {})
                    if func.get("name") == tool_name:
                        props = func.get("parameters", {}).get("properties", {})
                        desc = props.get(param, {}).get("description", "")
                        break
            
            if desc:
                parts.append(f"For {tool_name}, the parameter '{param}' requires: {desc}")
            else:
                parts.append(f"For {tool_name}, the parameter '{param}' is required")
        
        return (
            "THE ORACLE HAS A QUESTION!\n"
            + "\n".join(parts)
            + "\n\nPlease provide the missing information."
        )

    def _parse_conversational_calls(self, content: str) -> list:
        """Parse madlib-style tool calls like 'DEAR ORACLE, LISTMP3 ON ~/mp3'.
        
        Only parses the FIRST tool call and ignores everything after.
        Small models tend to continue rambling after the call.
        """
        tool_calls = []
        
        # Match "DEAR ORACLE, <REQUEST> ON <ARG1>..."
        # Stop at first match - models often ramble after
        pattern = r'DEAR\s+ORACLE,\s*(\w+)\s+ON\s+([^\n\.]+)'
        match = re.search(pattern, content, re.IGNORECASE)
        
        if match:
            tool_name = match.group(1).upper()
            arg1 = match.group(2).strip()
            
            # Find matching tool from cache by normalizing names
            mapped_name = self._find_tool_name(tool_name)
            if not mapped_name:
                return []  # No matching tool found
            
            # Build args dict based on tool's parameter names
            args = self._build_args(arg1, mapped_name)
            
            tool_calls.append({
                "name": mapped_name,
                "arguments": args,
            })
            return tool_calls  # Return after first match
        
        # Fallback: try simpler pattern without ON
        simple_pattern = r'DEAR\s+ORACLE,\s*(\w+)(?:\s+([^\n\.]+))?'
        match = re.search(simple_pattern, content, re.IGNORECASE)
        if match:
            tool_name = match.group(1).upper()
            args_str = match.group(2).strip() if match.group(2) else ""
            
            mapped_name = self._find_tool_name(tool_name)
            if mapped_name:
                args = self._parse_args(args_str, mapped_name)
                tool_calls.append({"name": mapped_name, "arguments": args})
        
        return tool_calls

    def _find_tool_name(self, requested_name: str) -> str | None:
        """Find a tool name from cache that matches the requested name.
        
        Normalizes names for matching:
        - LISTMP3S, LIST_MP3S, LISTMP3 -> list_mp3s
        - PLAYMP3, PLAY_MP3 -> play_mp3
        """
        if not self._tools_cache:
            return None
        
        # Normalize requested name: remove underscores, uppercase
        requested_normalized = requested_name.upper().replace("_", "")
        
        for tool in self._tools_cache:
            func = tool.get("function", {})
            actual_name = func.get("name", "")
            # Normalize actual name the same way
            actual_normalized = actual_name.upper().replace("_", "")
            
            if requested_normalized == actual_normalized:
                return actual_name
        
        return None

    def _build_args(self, arg_string: str, tool_name: str) -> dict:
        """Build args dict based on tool's actual parameter names.
        
        Handles formats like:
        - "~/mp3" -> {"path": "~/mp3"}
        - "path: ~/mp3" -> {"path": "~/mp3"}
        - "filename AND path" -> {"filename": "...", "path": "..."}
        """
        args = {}

        # Get parameter names from tool definition
        param_names = []
        if self._tools_cache:
            for tool in self._tools_cache:
                func = tool.get("function", {})
                if func.get("name") == tool_name:
                    params = func.get("parameters", {}).get("properties", {})
                    param_names = list(params.keys())
                    break

        if not param_names:
            # Fallback: use generic parsing
            return self._parse_args(arg_string, tool_name)

        # Split args by " AND " and map to parameter names
        parts = [p.strip() for p in arg_string.split(" AND ")]
        
        # Clean up each part - remove "param:" prefixes
        cleaned_parts = []
        for part in parts:
            # Remove "param:" prefix if present
            if ":" in part:
                maybe_param, value = part.split(":", 1)
                maybe_param = maybe_param.strip().lower()
                # Check if it matches a real parameter
                if maybe_param in [p.lower() for p in param_names]:
                    part = value.strip()
            cleaned_parts.append(part.strip('"\''))
        
        for i, param_name in enumerate(param_names):
            if i < len(cleaned_parts):
                args[param_name] = cleaned_parts[i]

        return args

    def _parse_args(self, args_str: str, tool_name: str) -> dict:
        """Parse argument string into dict based on tool's actual parameters."""
        if not args_str:
            return {}
        
        args_str = args_str.strip().strip('"\'')
        
        # Try JSON first
        try:
            return json.loads(args_str)
        except json.JSONDecodeError:
            pass
        
        # Get actual parameter names from tool definition
        param_names = []
        if self._tools_cache:
            for tool in self._tools_cache:
                func = tool.get("function", {})
                if func.get("name") == tool_name:
                    params = func.get("parameters", {}).get("properties", {})
                    param_names = list(params.keys())
                    break
        
        if not param_names:
            # No params defined, return empty
            return {}
        
        # Split by " AND " and map to parameter names
        parts = [p.strip() for p in args_str.split(" AND ")]
        args = {}
        for i, param_name in enumerate(param_names):
            if i < len(parts):
                args[param_name] = parts[i]
            else:
                args[param_name] = ""
        
        return args

    def _generate_id(self) -> str:
        """Generate a random ID for tool call."""
        import secrets
        return secrets.token_hex(8)

    def info(self, capabilities: dict) -> dict:
        """Declare tool calling capability."""
        capabilities["tools_support"] = True
        return capabilities


def format_oracle_response(result: str) -> str:
    """Format a tool result as an oracle response.
    
    Use this in llcat's tool_program.py to format results.
    
    Example:
        result = format_oracle_response('["a.mp3", "b.mp3"]')
        # Returns: "THE ORACLE RESPONDS: a.mp3, b.mp3"
    """
    try:
        items = json.loads(result)
        if isinstance(items, list):
            return f"THE ORACLE RESPONDS: {', '.join(items)}"
    except (json.JSONDecodeError, TypeError):
        pass
    return f"THE ORACLE RESPONDS: {result}"

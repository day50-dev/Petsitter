# Hook Examples from Built-in Tricks

## Example 1: Simple tool injection (`tricks/list_files.py`)

Demonstrates all 3 common hooks: inject system instructions, inject tool definitions, and declare capabilities.

```python
from src.trick import Trick

class ListFilesTrick(Trick):

    def system_prompt(self, to_add: str) -> str:
        """Tell the model about the list_files tool."""
        return (
            'You have access to the list_files tool. '
            'To use it, respond with: '
            '{"jsonrpc":"2.0","id":1,"method":"tools/call",'
            '"params":{"name":"list_files","arguments":{"path":"<directory_path>"}}}'
        )

    def pre_hook(self, context: list, params: dict) -> list:
        """Inject the tool definition into params so the client sees it."""
        tool_def = {
            "type": "function",
            "function": {
                "name": "list_files",
                "description": "List files in a directory",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "The directory path to list"}
                    },
                    "required": ["path"],
                },
            },
        }
        tools = params.get("tools", [])
        params["tools"] = tools + [tool_def]
        # Also append tool info to system prompt for visibility
        if context and context[0].get("role") == "system":
            context[0]["content"] += "\n\nAvailable tool:\n- list_files(path: str) -> list[str]"
        return context

    def info(self, capabilities: dict) -> dict:
        capabilities["tools_support"] = True
        capabilities["custom_tools"] = ["list_files"]
        return capabilities
```

Key patterns:
- `params["tools"]` is mutated directly to inject tool definitions that downstream tricks and the client can see.
- `system_prompt` returns the instruction string; the framework appends it to the system prompt.
- `info` adds keys to the capabilities dict.

## Example 2: Post-hook validation with retry (`tricks/json_mode.py`)

Demonstrates output validation, retry loops using `callmodel`, and state stored on `self`.

```python
from src import callmodel
from src.trick import Trick

class JsonModeTrick(Trick):

    def __init__(self, max_attempts: int = 3):
        self.max_attempts = max_attempts

    def system_prompt(self, to_add: str) -> str:
        return (
            "IMPORTANT: Your response must be valid JSON only. "
            "Do not include any explanatory text, markdown formatting, "
            "or code blocks. Respond with raw JSON."
        )

    def post_hook(self, context: list) -> list:
        last_message = context[-1]
        content = last_message.get("content", "")

        attempts = self.max_attempts
        while attempts > 0:
            try:
                # Strip markdown code blocks if present
                if content.startswith("```"):
                    lines = content.split("\n")
                    if lines[0].startswith("```"):
                        content = "\n".join(lines[1:-1]) if lines[-1] == "```" else "\n".join(lines[1:])
                json.loads(content)  # validate
                break
            except (json.JSONDecodeError, IndexError):
                attempts -= 1
                if attempts == 0:
                    break
                # Retry with feedback via callmodel
                context = callmodel(
                    context,
                    "Your response was not valid JSON. Please respond with valid JSON only.",
                )
                last_message = context[-1]
                content = last_message.get("content", "")

        context[-1]["content"] = content
        return context

    def info(self, capabilities: dict) -> dict:
        capabilities["json_mode"] = True
        return capabilities
```

Key patterns:
- `__init__` stores per-instance config (max retries).
- `post_hook` extracts the last message, validates it, and loops via `callmodel` for retries.
- `callmodel` (async) takes the current context and a feedback instruction; it returns the updated context with the model's new response appended.
- The cleaned content replaces `context[-1]["content"]`.

## Example 3: Tool call detection and transformation (`tricks/tool_call.py`)

Demonstrates complex `post_hook` logic: detecting patterns in model output, transforming to OpenAI standard format, and caching state across requests.

```python
class ToolCallTrick(Trick):

    def __init__(self):
        self._tools_cache = None
        self._model_has_native_tools = False

    def system_prompt(self, to_add: str) -> str:
        # Skip instructions if model has native tool support
        if self._model_has_native_tools:
            return ""
        return (
            'IMPORTANT: To call a tool, respond ONLY with a JSON object...'
        )

    def pre_hook(self, context: list, params: dict) -> list:
        tools = params.get("tools")
        if tools:
            self._tools_cache = tools
        # Inject tool definitions into system prompt as formatted JSON
        ...
        return context

    def post_hook(self, context: list) -> list:
        last_message = context[-1]

        # Option A: Model already returned native tool_calls (pass-through)
        if "tool_calls" in last_message:
            self._model_has_native_tools = True
            # Clean up non-standard fields
            ...
            return context

        # Option B: Detect JSONRPC tool call patterns in content
        tool_calls = self._parse_all_tool_calls(content)
        if tool_calls:
            # Convert to OpenAI format
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
```

Key patterns:
- Instance state (`_tools_cache`, `_model_has_native_tools`) persists across requests in the same session.
- Auto-detection of native tool support: if the model returns `tool_calls` on its own, switch to pass-through mode.
- Multiple parsing strategies: full-content JSON parse, line-by-line, brace-matching fallback.

## Example 4: Multi-step self-validation (`tricks/code_validator.py`)

Demonstrates multi-turn model calling within `post_hook` for self-healing validation:
model proposes a change, describes it, compares against the original request, and retries on mismatch.

Key patterns:
- `_get_user_request` extracts the last user message from context for comparison.
- Sub-calls to the model use **clean, minimal contexts** (just a system prompt) so the validation isn't polluted by conversation history.
- The main `context` is only mutated during regeneration: the failed assistant message is removed, a user message with feedback is appended, and `callmodel_sync` produces a new attempt.
- Retry loop with configurable `max_attempts` to avoid infinite loops.
- Exception handling ensures the proxy doesn't crash if a sub-call fails.

## Example 5: XML-style tool calling (`tricks/xml_tool.py`)

Demonstrates an alternative calling convention for smaller models.

```python
class XmlToolTrick(Trick):

    def system_prompt(self, to_add: str) -> str:
        return (
            "You have access to tools. To call a tool, use this XML format:\n\n"
            "<tool>tool_name</tool>\n"
            '<args>{"param": "value"}</args>\n\n'
            "IMPORTANT: After calling a tool, WAIT for the result..."
        )

    def post_hook(self, context: list) -> list:
        content = context[-1].get("content", "")

        # Parse XML-style tool calls
        tool_pattern = r'<tool>([^<]+)</tool>\s*<args[=]?>?(\{[^<]+\})</args>'
        matches = re.findall(tool_pattern, content, re.DOTALL)

        if matches:
            tool_calls = []
            for tool_name, args_json in matches:
                args = json.loads(args_json.strip())
                tool_calls.append({"name": tool_name.strip(), "arguments": args})

            # Convert to OpenAI format (same pattern as tool_call.py)
            context[-1]["tool_calls"] = [...]
            context[-1]["content"] = None
        return context
```

## Pattern summary

| What you want | Which hook | How |
|---------------|-----------|-----|
| Tell model how to behave | `system_prompt` | Return instruction string |
| Add tool definitions | `pre_hook` | Mutate `params["tools"]` |
| Inject extra context into messages | `pre_hook` | Modify or append to `context` list |
| Validate and fix model output | `post_hook` | Check `context[-1]["content"]`, retry with `callmodel` |
| Detect tool calls in text output | `post_hook` | Parse content, set `context[-1]["tool_calls"]` |
| Declare capabilities | `info` | Add keys to capabilities dict |

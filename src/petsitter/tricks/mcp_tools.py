"""Adds the tools described in an mcp.json file to every request, so any AI tool pointed at petsitter can see and call them.

Your coding agent only knows about the tools it ships with. If you have extra
tools described in an [mcp.json](https://github.com/sourcey/mcp-schema) file
(a docs search, an internal API), this trick injects them into every request,
whatever agent you're using. The model can then call them like any other tool;
your agent is still the one that runs the call.

## How to use

By default it reads `~/.config/petsitter/mcp.json`; set the `mcp_path` setting to
use another file. A minimal file:

```json
{
  "tools": [
    {
      "name": "search_docs",
      "description": "Search documentation by query",
      "inputSchema": {
        "type": "object",
        "properties": { "query": { "type": "string" } },
        "required": ["query"]
      }
    }
  ]
}
```

Switch files at runtime, or type `(mcp)` alone to see what's loaded:

```
(mcp: ~/work/my-tools.json)
```

## How it works

- Each MCP tool becomes an OpenAI `function` tool; `inputSchema` becomes
  `parameters`. Tools without a name are skipped.
- `pre_hook` merges them into `params["tools"]`: a client tool with the same
  name is dropped in favor of the MCP one.
- `system_prompt` also lists the MCP tool names and descriptions.
- The file is (re)loaded on configure, on startup, and when the keyword sets a
  new path. A missing or unparseable file just means no tools (logged).
"""

import json
import logging
from pathlib import Path

from petsitter.trick import Trick

logger = logging.getLogger("petsitter")

DEFAULT_MCP_PATH = Path.home() / ".config" / "petsitter" / "mcp.json"


def _mcp_tool_to_openai(tool: dict) -> dict | None:
    """Convert an MCP tool definition to OpenAI function-calling format."""
    name = tool.get("name", "")
    if not name:
        return None
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": tool.get("description", ""),
            "parameters": tool.get(
                "inputSchema",
                {"type": "object", "properties": {}},
            ),
        },
    }


class McpToolsTrick(Trick):
    """Loads tools from an mcp.json file and injects them into every request."""

    __brief__ = "Injects MCP tools from an mcp.json file into any harness"
    __display_name__ = "MCP Tools"
    __category__ = "Tool Calling"
    prompt_keyword = "mcp"
    config_fields = [
        {
            "key": "mcp_path",
            "label": "MCP config file",
            "description": (
                "Path to the mcp.json file describing the tools to inject. "
                "Leave blank for the default ~/.config/petsitter/mcp.json."
            ),
            "type": "path",
            "default": str(DEFAULT_MCP_PATH),
        },
    ]

    def __init__(self, mcp_path: str = ""):
        self.mcp_path = Path(mcp_path).expanduser() if mcp_path else DEFAULT_MCP_PATH
        self._tools: list[dict] = []
        self._tool_names: set[str] = set()

    def configure(self, config: dict) -> None:
        super().configure(config)
        self.mcp_path = Path(self.mcp_path).expanduser() if self.mcp_path else DEFAULT_MCP_PATH
        self._load_tools()

    # -- lifecycle -----------------------------------------------------------

    def startup(self) -> None:
        self._load_tools()

    # -- prompt keyword ------------------------------------------------------

    def handle_prompt_keyword(self, request: str, messages: list | None = None, payload: dict | None = None) -> dict | None:
        path = request.strip()
        if path:
            self.mcp_path = Path(path).expanduser()
            self._load_tools()
            return {
                "role": "assistant",
                "content": (
                    f"Loaded {len(self._tools)} MCP tool(s) from {self._mcp_path}"
                    if self._tools
                    else f"No tools found in {self._mcp_path}"
                ),
            }
        tool_names = ", ".join(self._tool_names) or "(none)"
        return {
            "role": "assistant",
            "content": (
                f"MCP path: {self.mcp_path}\n"
                f"Tools loaded: {len(self._tools)}\n"
                f"Names: {tool_names}"
            ),
        }

    # -- hooks ---------------------------------------------------------------

    def system_prompt(self, to_add: str) -> str:
        if not self._tools:
            return ""
        lines = ["You have access to the following MCP tools:"]
        for tool in self._tools:
            fn = tool["function"]
            lines.append(f"- {fn['name']}: {fn.get('description', '')}")
        return "\n".join(lines)

    def pre_hook(self, context: list, params: dict) -> list:
        if not self._tools:
            return context

        existing = params.get("tools", [])
        merged = [
            t
            for t in existing
            if t.get("function", {}).get("name") not in self._tool_names
        ]
        merged.extend(self._tools)
        params["tools"] = merged
        return context

    def info(self, capabilities: dict) -> dict:
        if self._tools:
            capabilities["mcp_tools"] = [t["function"]["name"] for t in self._tools]
        return capabilities

    # -- internal ------------------------------------------------------------

    def _load_tools(self) -> None:
        self._tools = []
        self._tool_names = set()

        if not self.mcp_path.exists():
            logger.info("MCP file not found: %s", self.mcp_path)
            return

        try:
            data = json.loads(self.mcp_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Failed to read MCP file %s: %s", self.mcp_path, e)
            return

        for tool in data.get("tools", []):
            openai_tool = _mcp_tool_to_openai(tool)
            if openai_tool:
                self._tools.append(openai_tool)
                self._tool_names.add(tool["name"])

        logger.info(
            "Loaded %d MCP tool(s) from %s", len(self._tools), self.mcp_path
        )

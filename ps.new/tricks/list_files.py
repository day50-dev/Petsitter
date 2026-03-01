"""Test trick with list_files tool for testing petsitter."""

import os
from pathlib import Path

from src.trick import Trick


class ListFilesTrick(Trick):
    """Test trick that provides a list_files tool."""

    def system_prompt(self, to_add: str) -> str:
        """Add tool calling instructions."""
        return (
            "You have access to the list_files tool. "
            "To use it, respond with: "
            '{"jsonrpc":"2.0","id":1,"method":"tools/call",'
            '"params":{"name":"list_files","arguments":{"path":"<directory_path>"}}}'
        )

    def pre_hook(self, context: list, params: dict) -> list:
        """Add tool definition to context."""
        tool_def = {
            "type": "function",
            "function": {
                "name": "list_files",
                "description": "List files in a directory",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "The directory path to list",
                        }
                    },
                    "required": ["path"],
                },
            },
        }

        tools = params.get("tools", [])
        params["tools"] = tools + [tool_def]

        if len(context) <= 2:
            tool_info = f"\n\nAvailable tool:\n- list_files(path: str) -> list[str]"
            if context and context[0].get("role") == "system":
                context[0]["content"] += tool_info
            else:
                context.insert(0, {"role": "system", "content": tool_info})

        return context

    def info(self, capabilities: dict) -> dict:
        """Declare tool capability."""
        capabilities["tools_support"] = True
        capabilities["custom_tools"] = ["list_files"]
        return capabilities


def list_files(path: str) -> list[str]:
    """List files in a directory.

    Args:
        path: Directory path to list.

    Returns:
        List of file/directory names.
    """
    try:
        entries = list(os.listdir(path))
        return sorted(entries)
    except (OSError, PermissionError) as e:
        return [f"Error: {e}"]

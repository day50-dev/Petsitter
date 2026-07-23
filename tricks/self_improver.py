"""Self-improver trick: use the (petsitter: ...) prompt keyword to invoke an
agent that can add, modify, and list tricks — all at runtime.

Usage:
    Include ``(petsitter: <request>)`` in any user message. The pattern is
    stripped before the model sees it, and the request is handled by an
    agent loop using the default model.
"""

import json
import os
import pathlib

import httpx

from src.trick import Trick, callmodel_sync


SKILL_PATH = pathlib.Path(__file__).resolve().parent.parent / ".agents" / "skills" / "self-improver" / "SKILL.md"

TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "add_trick",
            "description": "Create a new trick file under tricks/",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Filename, e.g. tricks/my_trick.py",
                    },
                    "code": {
                        "type": "string",
                        "description": "Full Python source for the trick module",
                    },
                },
                "required": ["path", "code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "modify_trick",
            "description": "Overwrite an existing trick file",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Filename, e.g. tricks/my_trick.py",
                    },
                    "code": {
                        "type": "string",
                        "description": "Full Python source to write",
                    },
                },
                "required": ["path", "code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tricks",
            "description": "List Python files in a directory (default: tricks/)",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path, defaults to tricks/",
                        "default": "tricks/",
                    },
                },
            },
        },
    },
]


def _tool_defs_text() -> str:
    lines = ["You have access to these tools. When you want to call one, respond ONLY with:"]
    lines.append('{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"<tool>","arguments":{...}}}')
    lines.append("")
    lines.append("Available tools:")
    for td in TOOL_DEFS:
        fn = td["function"]
        lines.append(f"\n### {fn['name']}")
        lines.append(fn.get("description", ""))
        props = fn.get("parameters", {}).get("properties", {})
        for pname, pinfo in props.items():
            lines.append(f"  - {pname} ({pinfo.get('type', 'string')}): {pinfo.get('description', '')}")
    return "\n".join(lines)


class SelfImproverTrick(Trick):
    """Handles (self-improve: <request>) by running an agent loop."""

    __brief__ = "Agent that can add, modify, and list tricks at runtime"
    __display_name__ = "Self-Improver"
    prompt_keyword = "petsitter"

    def __init__(self, max_iterations: int = 10):
        self.max_iterations = max_iterations

    def handle_prompt_keyword(self, request: str, messages: list | None = None, payload: dict | None = None) -> dict | None:
        skill = self._read_skill()
        system_prompt = f"{skill}\n\n{_tool_defs_text()}"
        context = [{"role": "system", "content": system_prompt}]

        for iteration in range(self.max_iterations):
            context = callmodel_sync(context, request if iteration == 0 else "")
            last = context[-1]
            content = last.get("content", "")

            tool_calls = self._parse_tool_calls(content)
            if not tool_calls:
                return last

            for tc in tool_calls:
                result = self._execute_tool(tc["name"], tc["arguments"])
                context.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": json.dumps(result)})

        return context[-1]

    def _read_skill(self) -> str:
        try:
            return SKILL_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            return "You are a helpful assistant that can manage petsitter tricks."

    def _parse_tool_calls(self, content: str) -> list[dict]:
        tool_calls = []

        try:
            data = json.loads(content.strip())
            if data.get("jsonrpc") == "2.0" and data.get("method") == "tools/call":
                params = data["params"]
                tool_calls.append({
                    "id": str(data.get("id", "1")),
                    "name": params.get("name", ""),
                    "arguments": params.get("arguments", {}),
                })
                return tool_calls
        except json.JSONDecodeError:
            pass

        for line in content.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                if data.get("jsonrpc") == "2.0" and data.get("method") == "tools/call":
                    params = data["params"]
                    tool_calls.append({
                        "id": str(data.get("id", "1")),
                        "name": params.get("name", ""),
                        "arguments": params.get("arguments", {}),
                    })
            except json.JSONDecodeError:
                continue

        return tool_calls

    def _execute_tool(self, name: str, arguments: dict) -> dict:
        if name == "add_trick":
            return self._add_trick(arguments.get("path", ""), arguments.get("code", ""))
        elif name == "modify_trick":
            return self._modify_trick(arguments.get("path", ""), arguments.get("code", ""))
        elif name == "list_tricks":
            return self._list_tricks(arguments.get("path", "tricks/"))
        else:
            return {"error": f"Unknown tool: {name}"}

    def _add_trick(self, path: str, code: str) -> dict:
        try:
            p = pathlib.Path(path)
            if p.exists():
                return {"error": f"File already exists: {path}"}
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(code, encoding="utf-8")
            return {"success": True, "path": path}
        except Exception as e:
            return {"error": str(e)}

    def _modify_trick(self, path: str, code: str) -> dict:
        try:
            p = pathlib.Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(code, encoding="utf-8")
            return {"success": True, "path": path}
        except Exception as e:
            return {"error": str(e)}

    def _list_tricks(self, path: str = "tricks/") -> dict:
        try:
            p = pathlib.Path(path)
            if not p.is_dir():
                return {"error": f"Not a directory: {path}"}
            files = sorted(f.name for f in p.iterdir() if f.suffix == ".py")
            return {"files": files, "path": path}
        except Exception as e:
            return {"error": str(e)}

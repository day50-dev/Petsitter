"""Saves the conversation, as the model sees it (or before and after the extensions), to JSON you can reload in llcat or any OpenAI-compatible tool.

Handy for keeping a record of a good session, reproducing a bug, or moving a
conversation to another tool. The file is a plain array of messages in the
standard Chat Completions format, with tool calls, tool results and reasoning
kept intact.

## How to use

Type `(exportit)` in a message. petsitter answers directly, without calling
the model:

```
Conversation exported to `/tmp/petsitter/convo-20260718-143022.json` (6 messages, llcat-compatible)
```

The export is the conversation **after** the channel's extensions transformed
it: what the model would see (rewritten messages, secrets as stand-ins, added
system prompts). For your side of it as well, before any transformation:

```
(exportit: both)
```

writes `convo-<time>-before.json` and `convo-<time>-after.json`. Anything else
after the colon is kept as a note: `(exportit: backup before refactor)`.

Load an export back with `llcat -c convo.json`.

## How it works

- Prompt keyword `exportit`; the handler writes the file and returns the reply.
- "After" comes from `transformed_messages()`: the channel's system prompts and
  pre_hooks run on a copy (no model call; extensions that only watch, like the
  Traffic Logger, are skipped so they don't record it). "Before" is the
  conversation as your tool sent it.
- Files go to `/tmp/petsitter/convo-<YYYYmmdd-HHMMSS>.json` (not configurable).
- Messages are normalized: assistant `tool_calls` are cleaned to
  `{id, type, function: {name, arguments}}` (an empty list when absent),
  `reasoning` is kept, tool messages keep `name` and `tool_call_id`. Unknown
  roles are copied as-is.
"""

import json
import os
from datetime import datetime

from petsitter.trick import Trick, transformed_messages


EXPORT_DIR = "/tmp/petsitter"


class ExportItTrick(Trick):
    """Exports the conversation as llcat-compatible JSON when (exportit) is used."""

    __brief__ = "Export conversation as llcat-compatible JSON"
    __display_name__ = "Export It"
    __category__ = "Diagnostics"
    prompt_keyword = "exportit"

    def handle_prompt_keyword(self, request: str, messages: list | None = None, payload: dict | None = None) -> dict | None:
        os.makedirs(EXPORT_DIR, exist_ok=True)
        before = messages or []
        both = (request or "").strip().lower() == "both"
        note = "" if both else (request or "").strip()

        after = transformed_messages()
        if after is None:   # no channel to run (called outside the proxy)
            after = before
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        sides = [("before", before), ("after", after)] if both else [("", after)]

        lines = []
        for side, msgs in sides:
            conversation = self._normalize_conversation(msgs)
            filepath = os.path.join(EXPORT_DIR, f"convo-{timestamp}{'-' + side if side else ''}.json")
            with open(filepath, "w") as f:
                json.dump(conversation, f, indent=2)
            label = f"{side.capitalize()}: " if side else "Conversation exported to "
            lines.append(f"{label}`{filepath}` ({len(conversation)} messages, llcat-compatible)")
            self.report(f"Exported {len(conversation)} messages to {filepath}")

        head = "Conversation exported, before and after the extensions:\n" if both else ""
        return {
            "role": "assistant",
            "content": head + "\n".join(lines) + (f"\nNote: {note}" if note else ""),
        }

    @staticmethod
    def _normalize_conversation(messages: list) -> list:
        result = []

        for msg in messages:
            role = msg.get("role", "")
            normalized: dict = {"role": role}

            if role == "system":
                normalized["content"] = msg.get("content", "")
            elif role == "user":
                normalized["content"] = msg.get("content", "")
            elif role == "assistant":
                normalized["content"] = msg.get("content")
                if msg.get("reasoning"):
                    normalized["reasoning"] = msg["reasoning"]
                tool_calls = msg.get("tool_calls")
                if tool_calls:
                    clean_calls = []
                    for tc in tool_calls:
                        clean_calls.append({
                            "id": tc.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": tc.get("function", {}).get("name", ""),
                                "arguments": tc.get("function", {}).get("arguments", "{}"),
                            },
                        })
                    normalized["tool_calls"] = clean_calls
                else:
                    normalized["tool_calls"] = []
            elif role == "tool":
                normalized["name"] = msg.get("name", "")
                normalized["tool_call_id"] = msg.get("tool_call_id", "")
                normalized["content"] = msg.get("content", "")
            else:
                normalized = dict(msg)

            result.append(normalized)

        return result

"""Export conversation as llcat-compatible JSON.

Activate by typing (exportit) or (exportit: optional note) in your message.
Exports the conversation history to /tmp/petsitter/ as a JSON file compatible
with llcat's conversation format.
"""

import json
import os
from datetime import datetime

from src.trick import Trick


EXPORT_DIR = "/tmp/petsitter"


class ExportItTrick(Trick):
    """Exports the conversation as llcat-compatible JSON when (exportit) is used."""

    __brief__ = "Export conversation as llcat-compatible JSON"
    __display_name__ = "Export It"
    prompt_keyword = "exportit"

    def handle_prompt_keyword(self, request: str, messages: list | None = None) -> dict | None:
        os.makedirs(EXPORT_DIR, exist_ok=True)
        messages = messages or []

        conversation = self._normalize_conversation(messages)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"convo-{timestamp}.json"
        filepath = os.path.join(EXPORT_DIR, filename)

        with open(filepath, "w") as f:
            json.dump(conversation, f, indent=2)

        note = f"\nNote: {request}" if request else ""
        return {
            "role": "assistant",
            "content": (
                f"Conversation exported to `{filepath}` "
                f"({len(conversation)} messages, llcat-compatible){note}"
            ),
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

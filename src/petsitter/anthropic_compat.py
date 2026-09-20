"""Translation between Anthropic's Messages API and the OpenAI chat shape.

Claude Code and anything else driven by ``ANTHROPIC_BASE_URL`` speaks Anthropic's
``/v1/messages``: a separate ``system`` field, message content that may be a list
of typed blocks, and tools described by ``input_schema``.  Every trick in
petsitter is written against the OpenAI shape instead -- a flat message list with
a ``system`` role, and ``tools[].function.parameters``.

Rather than teach every trick a second schema, requests are converted on the way
in, run through the normal pipeline, and converted back on the way out.  A trick
does not need to know which API the caller used.

What survives the round trip: system prompts, text content, tool definitions,
assistant tool calls, tool results, stop reasons and usage.  What does not:
image and document blocks are carried through untouched but are invisible to a
trick reading message text, and extended-thinking blocks are preserved in place
rather than exposed as text.
"""

import json
import time
import uuid
from typing import Any

ANTHROPIC_VERSION = "2023-06-01"


# --------------------------------------------------------------------------
# inbound: Anthropic request -> OpenAI-shaped messages
# --------------------------------------------------------------------------

def _system_text(system: Any) -> str:
    """Anthropic's system field is a string or a list of text blocks."""
    if not system:
        return ""
    if isinstance(system, str):
        return system
    parts = []
    for block in system:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(p for p in parts if p)


def _blocks_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        b.get("text", "") for b in content
        if isinstance(b, dict) and b.get("type") == "text"
    )


def to_openai_messages(payload: dict) -> tuple[list[dict], list[dict]]:
    """Return (messages, tools) in the shape the pipeline expects."""
    messages: list[dict] = []

    system = _system_text(payload.get("system"))
    if system:
        messages.append({"role": "system", "content": system})

    for entry in payload.get("messages") or []:
        if not isinstance(entry, dict):
            continue
        role = entry.get("role", "user")
        content = entry.get("content")

        if isinstance(content, str) or content is None:
            messages.append({"role": role, "content": content or ""})
            continue

        # A user turn carrying tool results becomes one `tool` message per
        # result, which is how the OpenAI shape represents the same thing.
        tool_results = [b for b in content
                        if isinstance(b, dict) and b.get("type") == "tool_result"]
        tool_uses = [b for b in content
                     if isinstance(b, dict) and b.get("type") == "tool_use"]
        text = _blocks_to_text(content)

        if tool_results:
            for block in tool_results:
                body = block.get("content")
                messages.append({
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id", ""),
                    "content": body if isinstance(body, str) else _blocks_to_text(body),
                })
            if text:
                messages.append({"role": role, "content": text})
            continue

        message: dict[str, Any] = {"role": role, "content": text}
        if tool_uses:
            message["tool_calls"] = [{
                "id": b.get("id", ""),
                "type": "function",
                "function": {
                    "name": b.get("name", ""),
                    "arguments": json.dumps(b.get("input") or {}),
                },
            } for b in tool_uses]
            if not text:
                message["content"] = None
        messages.append(message)

    tools = []
    for tool in payload.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        tools.append({
            "type": "function",
            "function": {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
            },
        })
    return messages, tools


# --------------------------------------------------------------------------
# outbound: OpenAI-shaped messages -> Anthropic request
# --------------------------------------------------------------------------

def to_anthropic_payload(messages: list[dict], tools: list[dict], original: dict) -> dict:
    """Rebuild an Anthropic request, carrying the caller's own fields through."""
    out: dict[str, Any] = {}
    for key in ("model", "max_tokens", "temperature", "top_p", "top_k",
                "stop_sequences", "metadata", "tool_choice", "thinking"):
        if key in original:
            out[key] = original[key]
    out.setdefault("max_tokens", 4096)

    system_parts, converted = [], []
    for message in messages:
        role = message.get("role")
        if role == "system":
            if message.get("content"):
                system_parts.append(message["content"])
            continue
        if role == "tool":
            converted.append({"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": message.get("tool_call_id", ""),
                "content": message.get("content") or "",
            }]})
            continue
        if role == "assistant" and message.get("tool_calls"):
            blocks: list[dict] = []
            if message.get("content"):
                blocks.append({"type": "text", "text": message["content"]})
            for call in message["tool_calls"]:
                fn = call.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (ValueError, TypeError):
                    args = {}
                blocks.append({"type": "tool_use", "id": call.get("id", ""),
                               "name": fn.get("name", ""), "input": args})
            converted.append({"role": "assistant", "content": blocks})
            continue
        converted.append({"role": role or "user", "content": message.get("content") or ""})

    if system_parts:
        out["system"] = "\n".join(system_parts)
    out["messages"] = converted

    if tools:
        out["tools"] = [{
            "name": (t.get("function") or {}).get("name", ""),
            "description": (t.get("function") or {}).get("description", ""),
            "input_schema": (t.get("function") or {}).get("parameters")
                            or {"type": "object", "properties": {}},
        } for t in tools]
    return out


# --------------------------------------------------------------------------
# the reply, in both directions
# --------------------------------------------------------------------------

def response_to_assistant_message(response: dict) -> dict:
    """The Anthropic reply as one OpenAI-shaped assistant message."""
    text_parts, tool_calls = [], []
    for block in response.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            tool_calls.append({
                "id": block.get("id", ""),
                "type": "function",
                "function": {"name": block.get("name", ""),
                             "arguments": json.dumps(block.get("input") or {})},
            })
    message: dict[str, Any] = {"role": "assistant",
                               "content": "\n".join(text_parts) if text_parts else None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def apply_assistant_message(response: dict, message: dict) -> dict:
    """Write an edited reply back into the Anthropic response.

    Only the text is written back. A trick that rewrites prose is the common
    case; one that invents tool calls would need to speak Anthropic's schema
    itself, and silently reshaping them here would be worse than leaving them.
    """
    text = message.get("content")
    if text is None:
        return response
    blocks = list(response.get("content") or [])
    for i, block in enumerate(blocks):
        if isinstance(block, dict) and block.get("type") == "text":
            if block.get("text") != text:
                blocks[i] = {**block, "text": text}
            response["content"] = blocks
            return response
    # no text block to edit (a pure tool-call reply); add one only if a trick
    # actually produced text
    if text:
        response["content"] = [{"type": "text", "text": text}] + blocks
    return response


# --------------------------------------------------------------------------
# streaming
# --------------------------------------------------------------------------

def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def stream_events(response: dict):
    """Replay a complete Anthropic response as its streaming event sequence.

    Petsitter has to see a whole reply before post_hooks can run on it, so the
    upstream call is not streamed; the events are synthesised afterwards. A
    client cannot tell the difference apart from the timing.
    """
    message_id = response.get("id") or f"msg_{uuid.uuid4().hex[:24]}"
    blocks = response.get("content") or []

    yield _sse("message_start", {"type": "message_start", "message": {
        "id": message_id,
        "type": "message",
        "role": "assistant",
        "model": response.get("model", ""),
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": response.get("usage") or {"input_tokens": 0, "output_tokens": 0},
    }})

    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            yield _sse("content_block_start", {"type": "content_block_start", "index": index,
                                               "content_block": {"type": "text", "text": ""}})
            text = block.get("text", "")
            if text:
                yield _sse("content_block_delta", {"type": "content_block_delta", "index": index,
                                                   "delta": {"type": "text_delta", "text": text}})
        elif kind == "tool_use":
            yield _sse("content_block_start", {"type": "content_block_start", "index": index,
                                               "content_block": {"type": "tool_use",
                                                                 "id": block.get("id", ""),
                                                                 "name": block.get("name", ""),
                                                                 "input": {}}})
            yield _sse("content_block_delta", {"type": "content_block_delta", "index": index,
                                               "delta": {"type": "input_json_delta",
                                                         "partial_json": json.dumps(block.get("input") or {})}})
        else:
            yield _sse("content_block_start", {"type": "content_block_start",
                                               "index": index, "content_block": block})
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": index})

    yield _sse("message_delta", {"type": "message_delta",
                                 "delta": {"stop_reason": response.get("stop_reason", "end_turn"),
                                           "stop_sequence": response.get("stop_sequence")},
                                 "usage": response.get("usage") or {"output_tokens": 0}})
    yield _sse("message_stop", {"type": "message_stop"})


def error_event(message: str) -> str:
    return _sse("error", {"type": "error",
                          "error": {"type": "api_error", "message": message}})

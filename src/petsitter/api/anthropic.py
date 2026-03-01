"""Anthropic API compatible handlers for PetSitter."""

from __future__ import annotations

import uuid
from datetime import datetime

from petsitter.models import (
    AnthropicMessage,
    AnthropicRequest,
    AnthropicResponse,
    ChatRequest,
    Message,
    MessageRole,
)


def anthropic_to_internal(request: AnthropicRequest) -> ChatRequest:
    """Convert Anthropic request to internal format.

    Args:
        request: Anthropic API request

    Returns:
        Internal ChatRequest
    """
    messages = []

    # Add system message if present
    if request.system:
        messages.append(Message(role=MessageRole.SYSTEM, content=request.system))

    # Convert messages
    for msg in request.messages:
        role = MessageRole(msg.role)
        content = msg.content

        # Handle list content format
        if isinstance(content, list):
            text_parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_parts.append(item.get("text", ""))
            content = "\n".join(text_parts)

        messages.append(Message(role=role, content=content))

    return ChatRequest(
        messages=messages,
        model=request.model,
        max_tokens=request.max_tokens,
        temperature=request.temperature,
        stream=request.stream,
    )


def internal_to_anthropic(
    content: str,
    model: str,
    usage: dict[str, int] | None = None,
) -> AnthropicResponse:
    """Convert internal response to Anthropic format.

    Args:
        content: Response content
        model: Model name used
        usage: Optional token usage

    Returns:
        AnthropicResponse
    """
    return AnthropicResponse(
        id=f"msg_{uuid.uuid4().hex[:24]}",
        type="message",
        role="assistant",
        content=[{"type": "text", "text": content}],
        model=model,
        stop_reason="end_turn",
        usage=usage,
    )


def create_anthropic_error_response(
    error_type: str,
    message: str,
    status_code: int = 400,
) -> dict:
    """Create an Anthropic-style error response.

    Args:
        error_type: Type of error
        message: Error message
        status_code: HTTP status code

    Returns:
        Error response dict
    """
    return {
        "type": "error",
        "error": {
            "type": error_type,
            "message": message,
        },
    }

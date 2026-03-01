"""OpenAI API compatible handlers for PetSitter."""

from __future__ import annotations

import time
import uuid

from petsitter.models import (
    ChatRequest,
    Message,
    MessageRole,
    OpenAIChoice,
    OpenAIMessage,
    OpenAIRequest,
    OpenAIResponse,
)


def openai_to_internal(request: OpenAIRequest) -> ChatRequest:
    """Convert OpenAI request to internal format.

    Args:
        request: OpenAI API request

    Returns:
        Internal ChatRequest
    """
    messages = []

    for msg in request.messages:
        messages.append(Message(role=MessageRole(msg.role), content=msg.content))

    return ChatRequest(
        messages=messages,
        model=request.model,
        max_tokens=request.max_tokens,
        temperature=request.temperature,
        stream=request.stream,
    )


def internal_to_openai(
    content: str,
    model: str,
    usage: dict[str, int] | None = None,
    finish_reason: str | None = None,
) -> OpenAIResponse:
    """Convert internal response to OpenAI format.

    Args:
        content: Response content
        model: Model name used
        usage: Optional token usage
        finish_reason: Optional finish reason

    Returns:
        OpenAIResponse
    """
    return OpenAIResponse(
        id=f"chatcmpl-{uuid.uuid4().hex[:24]}",
        object="chat.completion",
        created=int(time.time()),
        model=model,
        choices=[
            OpenAIChoice(
                index=0,
                message=OpenAIMessage(role="assistant", content=content),
                finish_reason=finish_reason or "stop",
            )
        ],
        usage=usage,
    )


def create_openai_error_response(
    error_type: str,
    message: str,
    status_code: int = 400,
) -> dict:
    """Create an OpenAI-style error response.

    Args:
        error_type: Type of error
        message: Error message
        status_code: HTTP status code

    Returns:
        Error response dict
    """
    return {
        "error": {
            "message": message,
            "type": error_type,
            "code": status_code,
        }
    }

"""Tests for PetSitter API handlers."""

from __future__ import annotations

import pytest

from petsitter.api.anthropic import (
    anthropic_to_internal,
    create_anthropic_error_response,
    internal_to_anthropic,
)
from petsitter.api.openai import (
    create_openai_error_response,
    internal_to_openai,
    openai_to_internal,
)
from petsitter.models import (
    AnthropicMessage,
    AnthropicRequest,
    ChatRequest,
    Message,
    MessageRole,
    OpenAIMessage,
    OpenAIRequest,
)


class TestAnthropicConversion:
    """Tests for Anthropic API conversion."""

    def test_anthropic_to_internal_basic(self) -> None:
        """Test basic Anthropic to internal conversion."""
        request = AnthropicRequest(
            model="claude-3",
            messages=[AnthropicMessage(role="user", content="Hello")],
        )

        internal = anthropic_to_internal(request)

        assert len(internal.messages) == 1
        assert internal.messages[0].role == MessageRole.USER
        assert internal.messages[0].content == "Hello"

    def test_anthropic_to_internal_with_system(self) -> None:
        """Test Anthropic to internal with system prompt."""
        request = AnthropicRequest(
            model="claude-3",
            messages=[AnthropicMessage(role="user", content="Hi")],
            system="You are helpful",
        )

        internal = anthropic_to_internal(request)

        assert len(internal.messages) == 2
        assert internal.messages[0].role == MessageRole.SYSTEM
        assert internal.messages[0].content == "You are helpful"

    def test_anthropic_to_internal_with_list_content(self) -> None:
        """Test Anthropic to internal with list content format."""
        request = AnthropicRequest(
            model="claude-3",
            messages=[
                AnthropicMessage(
                    role="user",
                    content=[
                        {"type": "text", "text": "Hello"},
                        {"type": "text", "text": "World"},
                    ],
                ),
            ],
        )

        internal = anthropic_to_internal(request)

        assert internal.messages[0].content == "Hello\nWorld"

    def test_anthropic_to_internal_preserves_settings(self) -> None:
        """Test that settings are preserved in conversion."""
        request = AnthropicRequest(
            model="claude-3",
            messages=[AnthropicMessage(role="user", content="Hi")],
            max_tokens=500,
            temperature=0.5,
        )

        internal = anthropic_to_internal(request)

        assert internal.model == "claude-3"
        assert internal.max_tokens == 500
        assert internal.temperature == 0.5

    def test_internal_to_anthropic(self) -> None:
        """Test internal to Anthropic conversion."""
        response = internal_to_anthropic(
            content="Hello!",
            model="claude-3",
            usage={"prompt_tokens": 10, "completion_tokens": 5},
        )

        assert response.type == "message"
        assert response.role == "assistant"
        assert response.model == "claude-3"
        assert response.content[0]["text"] == "Hello!"
        assert response.usage["prompt_tokens"] == 10

    def test_internal_to_anthropic_without_usage(self) -> None:
        """Test internal to Anthropic without usage info."""
        response = internal_to_anthropic(
            content="Hello!",
            model="claude-3",
        )

        assert response.usage is None

    def test_create_anthropic_error_response(self) -> None:
        """Test creating Anthropic error response."""
        error = create_anthropic_error_response(
            error_type="invalid_request",
            message="Invalid model",
            status_code=400,
        )

        assert error["type"] == "error"
        assert error["error"]["type"] == "invalid_request"
        assert error["error"]["message"] == "Invalid model"


class TestOpenAIConversion:
    """Tests for OpenAI API conversion."""

    def test_openai_to_internal_basic(self) -> None:
        """Test basic OpenAI to internal conversion."""
        request = OpenAIRequest(
            model="gpt-4",
            messages=[OpenAIMessage(role="user", content="Hello")],
        )

        internal = openai_to_internal(request)

        assert len(internal.messages) == 1
        assert internal.messages[0].role == MessageRole.USER
        assert internal.messages[0].content == "Hello"

    def test_openai_to_internal_multiple_messages(self) -> None:
        """Test OpenAI to internal with multiple messages."""
        request = OpenAIRequest(
            model="gpt-4",
            messages=[
                OpenAIMessage(role="system", content="Be helpful"),
                OpenAIMessage(role="user", content="Hi"),
                OpenAIMessage(role="assistant", content="Hello!"),
            ],
        )

        internal = openai_to_internal(request)

        assert len(internal.messages) == 3
        assert internal.messages[0].role == MessageRole.SYSTEM
        assert internal.messages[1].role == MessageRole.USER
        assert internal.messages[2].role == MessageRole.ASSISTANT

    def test_openai_to_internal_preserves_settings(self) -> None:
        """Test that settings are preserved in conversion."""
        request = OpenAIRequest(
            model="gpt-4",
            messages=[OpenAIMessage(role="user", content="Hi")],
            max_tokens=100,
            temperature=0.7,
        )

        internal = openai_to_internal(request)

        assert internal.model == "gpt-4"
        assert internal.max_tokens == 100
        assert internal.temperature == 0.7

    def test_internal_to_openai(self) -> None:
        """Test internal to OpenAI conversion."""
        response = internal_to_openai(
            content="Hello!",
            model="gpt-4",
            usage={"prompt_tokens": 10, "completion_tokens": 5},
            finish_reason="stop",
        )

        assert response.object == "chat.completion"
        assert response.model == "gpt-4"
        assert len(response.choices) == 1
        assert response.choices[0].message.content == "Hello!"
        assert response.choices[0].finish_reason == "stop"
        assert response.usage["prompt_tokens"] == 10

    def test_internal_to_openai_default_finish_reason(self) -> None:
        """Test internal to OpenAI with default finish reason."""
        response = internal_to_openai(
            content="Hello!",
            model="gpt-4",
        )

        assert response.choices[0].finish_reason == "stop"

    def test_internal_to_openai_without_usage(self) -> None:
        """Test internal to OpenAI without usage info."""
        response = internal_to_openai(
            content="Hello!",
            model="gpt-4",
        )

        assert response.usage is None

    def test_create_openai_error_response(self) -> None:
        """Test creating OpenAI error response."""
        error = create_openai_error_response(
            error_type="invalid_request_error",
            message="Invalid model",
            status_code=400,
        )

        assert "error" in error
        assert error["error"]["type"] == "invalid_request_error"
        assert error["error"]["message"] == "Invalid model"
        assert error["error"]["code"] == 400


class TestMessageConversion:
    """Tests for message role conversions."""

    def test_all_roles_anthropic(self) -> None:
        """Test all message roles with Anthropic format."""
        request = AnthropicRequest(
            model="claude-3",
            messages=[
                AnthropicMessage(role="user", content="User msg"),
                AnthropicMessage(role="assistant", content="Assistant msg"),
            ],
        )

        internal = anthropic_to_internal(request)

        assert internal.messages[0].role == MessageRole.USER
        assert internal.messages[1].role == MessageRole.ASSISTANT

    def test_all_roles_openai(self) -> None:
        """Test all message roles with OpenAI format."""
        request = OpenAIRequest(
            model="gpt-4",
            messages=[
                OpenAIMessage(role="system", content="System"),
                OpenAIMessage(role="user", content="User"),
                OpenAIMessage(role="assistant", content="Assistant"),
            ],
        )

        internal = openai_to_internal(request)

        roles = [m.role for m in internal.messages]
        assert MessageRole.SYSTEM in roles
        assert MessageRole.USER in roles
        assert MessageRole.ASSISTANT in roles

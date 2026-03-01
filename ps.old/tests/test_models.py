"""Tests for PetSitter data models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from petsitter.models import (
    AnthropicMessage,
    AnthropicRequest,
    AnthropicResponse,
    ChatRequest,
    ChatResponse,
    Message,
    MessageRole,
    OpenAIMessage,
    OpenAIRequest,
    OpenAIResponse,
    RetryState,
    Skill,
    SkillConfig,
    ValidatorResult,
)


class TestMessage:
    """Tests for Message model."""

    def test_create_user_message(self) -> None:
        """Test creating a user message."""
        msg = Message(role=MessageRole.USER, content="Hello")
        assert msg.role == MessageRole.USER
        assert msg.content == "Hello"

    def test_create_system_message(self) -> None:
        """Test creating a system message."""
        msg = Message(role=MessageRole.SYSTEM, content="You are helpful")
        assert msg.role == MessageRole.SYSTEM

    def test_create_assistant_message(self) -> None:
        """Test creating an assistant message."""
        msg = Message(role=MessageRole.ASSISTANT, content="Hi there!")
        assert msg.role == MessageRole.ASSISTANT

    def test_message_role_from_string(self) -> None:
        """Test creating message with string role."""
        msg = Message(role="user", content="Test")
        assert msg.role == MessageRole.USER


class TestValidatorResult:
    """Tests for ValidatorResult model."""

    def test_create_passed_result(self) -> None:
        """Test creating a passed validator result."""
        result = ValidatorResult(
            validator_name="test_validator",
            passed=True,
        )
        assert result.passed is True
        assert result.errors == []

    def test_create_failed_result(self) -> None:
        """Test creating a failed validator result."""
        result = ValidatorResult(
            validator_name="test_validator",
            passed=False,
            errors=["Error 1", "Error 2"],
            feedback="Please fix these issues",
        )
        assert result.passed is False
        assert len(result.errors) == 2

    def test_to_feedback_string_passed(self) -> None:
        """Test feedback string for passed validation."""
        result = ValidatorResult(
            validator_name="test_validator",
            passed=True,
        )
        assert "passed" in result.to_feedback_string()

    def test_to_feedback_string_failed(self) -> None:
        """Test feedback string for failed validation."""
        result = ValidatorResult(
            validator_name="test_validator",
            passed=False,
            errors=["Error 1"],
        )
        feedback = result.to_feedback_string()
        assert "failed" in feedback
        assert "Error 1" in feedback


class TestSkillConfig:
    """Tests for SkillConfig model."""

    def test_create_minimal_config(self) -> None:
        """Test creating minimal skill config."""
        config = SkillConfig(name="test")
        assert config.name == "test"
        assert config.validators == []
        assert config.model_pin is None

    def test_create_full_config(self) -> None:
        """Test creating full skill config."""
        config = SkillConfig(
            name="programming",
            description="Programming skill",
            validators=["ruff_lint", "mypy_types"],
            model_pin="qwen3-8b",
            version="1.0.0",
        )
        assert config.name == "programming"
        assert len(config.validators) == 2
        assert config.model_pin == "qwen3-8b"


class TestSkill:
    """Tests for Skill model."""

    def test_create_skill(self) -> None:
        """Test creating a skill directly."""
        skill = Skill(
            name="test",
            description="Test skill",
            validators=["validator1"],
            source="/path/to/skill",
        )
        assert skill.name == "test"
        assert skill.source == "/path/to/skill"

    def test_skill_from_config(self, sample_skill_config: SkillConfig) -> None:
        """Test creating skill from config."""
        skill = Skill.from_config(sample_skill_config, source="/test")
        assert skill.name == sample_skill_config.name
        assert skill.validators == sample_skill_config.validators
        assert skill.model_pin == sample_skill_config.model_pin


class TestRetryState:
    """Tests for RetryState model."""

    def test_initial_state(self) -> None:
        """Test initial retry state."""
        state = RetryState(max_retries=3)
        assert state.attempt == 0
        assert state.can_retry is True

    def test_can_retry(self) -> None:
        """Test can_retry property."""
        state = RetryState(max_retries=2)
        assert state.can_retry is True

        state.attempt = 1
        assert state.can_retry is True

        state.attempt = 2
        assert state.can_retry is False

    def test_add_feedback(self) -> None:
        """Test adding feedback."""
        state = RetryState()
        state.add_feedback("Feedback 1")
        state.add_feedback("Feedback 2")

        assert len(state.accumulated_feedback) == 2
        assert "Feedback 1" in state.feedback_text

    def test_add_validator_result(self) -> None:
        """Test adding validator result."""
        state = RetryState()
        result = ValidatorResult(
            validator_name="test",
            passed=False,
            errors=["Error"],
        )
        state.add_validator_result(result)

        assert len(state.validator_failures) == 1
        assert len(state.accumulated_feedback) == 1


class TestChatRequest:
    """Tests for ChatRequest model."""

    def test_create_request(self, sample_messages: list[Message]) -> None:
        """Test creating a chat request."""
        request = ChatRequest(
            messages=sample_messages,
            model="test-model",
            temperature=0.5,
        )
        assert len(request.messages) == 2
        assert request.model == "test-model"

    def test_default_values(self, sample_messages: list[Message]) -> None:
        """Test default values in chat request."""
        request = ChatRequest(messages=sample_messages)
        assert request.model == "default"
        assert request.temperature == 0.7
        assert request.stream is False


class TestChatResponse:
    """Tests for ChatResponse model."""

    def test_create_response(self) -> None:
        """Test creating a chat response."""
        response = ChatResponse(
            content="Hello!",
            model="test-model",
            retries=1,
        )
        assert response.content == "Hello!"
        assert response.retries == 1


class TestAnthropicModels:
    """Tests for Anthropic API models."""

    def test_anthropic_request(self) -> None:
        """Test creating Anthropic request."""
        request = AnthropicRequest(
            model="claude-3",
            messages=[AnthropicMessage(role="user", content="Hello")],
            max_tokens=100,
        )
        assert request.model == "claude-3"
        assert len(request.messages) == 1

    def test_anthropic_response(self) -> None:
        """Test creating Anthropic response."""
        response = AnthropicResponse(
            id="msg_123",
            content=[{"type": "text", "text": "Hi!"}],
            model="claude-3",
        )
        assert response.type == "message"
        assert response.role == "assistant"


class TestOpenAIModels:
    """Tests for OpenAI API models."""

    def test_openai_request(self) -> None:
        """Test creating OpenAI request."""
        request = OpenAIRequest(
            model="gpt-4",
            messages=[OpenAIMessage(role="user", content="Hello")],
        )
        assert request.model == "gpt-4"

    def test_openai_response(self) -> None:
        """Test creating OpenAI response."""
        response = OpenAIResponse(
            id="chatcmpl_123",
            model="gpt-4",
            choices=[],
        )
        assert response.object == "chat.completion"

"""Core data models for PetSitter."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class MessageRole(str, Enum):
    """Message role enumeration."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class Message(BaseModel):
    """A chat message with role and content."""

    role: MessageRole
    content: str


class ValidatorResult(BaseModel):
    """Result from running a validator."""

    validator_name: str
    passed: bool
    errors: list[str] = Field(default_factory=list)
    feedback: str = ""
    code_block: str = ""

    def to_feedback_string(self) -> str:
        """Convert result to a feedback string."""
        if self.passed:
            return f"✓ {self.validator_name}: passed"
        feedback_parts = [f"✗ {self.validator_name}: failed"]
        if self.errors:
            feedback_parts.append("Errors:")
            for err in self.errors:
                feedback_parts.append(f"  - {err}")
        if self.feedback:
            feedback_parts.append(self.feedback)
        return "\n".join(feedback_parts)


class SkillConfig(BaseModel):
    """Configuration for a skill loaded from skill.yaml."""

    name: str
    description: str = ""
    validators: list[str] = Field(default_factory=list)
    model_pin: str | None = None
    version: str | None = None
    system_prompt: str = ""

    model_config = {"extra": "allow"}


class Skill(BaseModel):
    """A skill with its configuration and components."""

    name: str
    description: str = ""
    validators: list[str] = Field(default_factory=list)
    model_pin: str | None = None
    version: str | None = None
    system_prompt: str = ""
    source: str = ""  # Path or URL
    tools: list[str] = Field(default_factory=list)

    @classmethod
    def from_config(cls, config: SkillConfig, source: str = "") -> Skill:
        """Create a Skill from a SkillConfig."""
        return cls(
            name=config.name,
            description=config.description,
            validators=config.validators,
            model_pin=config.model_pin,
            version=config.version,
            system_prompt=config.system_prompt,
            source=source,
        )


class RetryState(BaseModel):
    """Tracks the state of retry attempts."""

    attempt: int = 0
    max_retries: int = 3
    accumulated_feedback: list[str] = Field(default_factory=list)
    validator_failures: list[ValidatorResult] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=datetime.now)

    @property
    def can_retry(self) -> bool:
        """Check if more retries are allowed."""
        return self.attempt < self.max_retries

    @property
    def feedback_text(self) -> str:
        """Get accumulated feedback as a single string."""
        if not self.accumulated_feedback:
            return ""
        return "\n\n".join(self.accumulated_feedback)

    def add_feedback(self, feedback: str) -> None:
        """Add feedback to the accumulated list."""
        self.accumulated_feedback.append(feedback)

    def add_validator_result(self, result: ValidatorResult) -> None:
        """Add a validator result and extract feedback."""
        self.validator_failures.append(result)
        if not result.passed:
            self.add_feedback(result.to_feedback_string())


class ChatRequest(BaseModel):
    """Internal chat request format."""

    messages: list[Message]
    model: str = "default"
    temperature: float = 0.7
    max_tokens: int | None = None
    stream: bool = False
    skills: list[str] = Field(default_factory=list)
    max_retries: int = 3


class ChatResponse(BaseModel):
    """Internal chat response format."""

    content: str
    model: str
    usage: dict[str, int] | None = None
    retries: int = 0
    validator_results: list[ValidatorResult] = Field(default_factory=list)


# Anthropic API compatible models
class AnthropicMessage(BaseModel):
    """Anthropic API message format."""

    role: str
    content: str | list[dict[str, Any]]


class AnthropicRequest(BaseModel):
    """Anthropic API request format."""

    model: str
    messages: list[AnthropicMessage]
    max_tokens: int = 1024
    system: str | None = None
    temperature: float = 0.7
    stream: bool = False


class AnthropicResponse(BaseModel):
    """Anthropic API response format."""

    id: str
    type: str = "message"
    role: str = "assistant"
    content: list[dict[str, Any]]
    model: str
    stop_reason: str | None = None
    usage: dict[str, int] | None = None


# OpenAI API compatible models
class OpenAIMessage(BaseModel):
    """OpenAI API message format."""

    role: str
    content: str


class OpenAIRequest(BaseModel):
    """OpenAI API request format."""

    model: str
    messages: list[OpenAIMessage]
    max_tokens: int | None = None
    temperature: float = 0.7
    stream: bool = False


class OpenAIChoice(BaseModel):
    """OpenAI API choice format."""

    index: int = 0
    message: OpenAIMessage
    finish_reason: str | None = None


class OpenAIResponse(BaseModel):
    """OpenAI API response format."""

    id: str
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(datetime.now().timestamp()))
    model: str
    choices: list[OpenAIChoice]
    usage: dict[str, int] | None = None


class HealthResponse(BaseModel):
    """Health check response."""

    status: str = "healthy"
    version: str = "0.1.0"
    timestamp: datetime = Field(default_factory=datetime.now)

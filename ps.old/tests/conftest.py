"""Pytest configuration and fixtures for PetSitter tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from petsitter.models import Message, MessageRole, Skill, SkillConfig


@pytest.fixture
def sample_message() -> Message:
    """Create a sample user message."""
    return Message(role=MessageRole.USER, content="Hello, world!")


@pytest.fixture
def sample_messages() -> list[Message]:
    """Create a sample conversation."""
    return [
        Message(role=MessageRole.SYSTEM, content="You are a helpful assistant."),
        Message(role=MessageRole.USER, content="Write a function to add two numbers."),
    ]


@pytest.fixture
def sample_skill_config() -> SkillConfig:
    """Create a sample skill configuration."""
    return SkillConfig(
        name="test_skill",
        description="A test skill for unit testing",
        validators=["ruff_lint", "no_eval_exec"],
        model_pin="test-model",
        version="1.0.0",
    )


@pytest.fixture
def sample_skill(sample_skill_config: SkillConfig) -> Skill:
    """Create a sample skill."""
    return Skill(
        name=sample_skill_config.name,
        description=sample_skill_config.description,
        validators=sample_skill_config.validators,
        model_pin=sample_skill_config.model_pin,
        version=sample_skill_config.version,
        system_prompt="# Test Skill Prompt\n\nThis is the test skill system prompt.",
        source="/test/path",
    )


@pytest.fixture
def sample_python_code() -> str:
    """Sample Python code for testing."""
    return """
def add(a: int, b: int) -> int:
    '''Add two numbers.'''
    return a + b
"""


@pytest.fixture
def unsafe_python_code() -> str:
    """Sample unsafe Python code for testing."""
    return """
def execute_user_code(code: str):
    return eval(code)
"""


@pytest.fixture
def code_with_lint_errors() -> str:
    """Sample Python code with lint errors."""
    return """
def add(a,b):
    x=1
    return a+b
"""


@pytest.fixture
def test_skills_dir(tmp_path: Path) -> Path:
    """Create a temporary skills directory with test skills."""
    skill_dir = tmp_path / "test_skill"
    skill_dir.mkdir()

    # Create skill.yaml
    skill_yaml = skill_dir / "skill.yaml"
    skill_yaml.write_text("""
name: test_skill
description: A test skill
validators:
  - no_eval_exec
version: "1.0.0"
""")

    # Create system_prompt.md
    system_prompt = skill_dir / "system_prompt.md"
    system_prompt.write_text("# Test Skill\n\nThis is a test skill.")

    return tmp_path


@pytest.fixture
def mock_validator_result() -> dict[str, Any]:
    """Create a mock validator result."""
    return {
        "validator_name": "test_validator",
        "passed": True,
        "errors": [],
        "feedback": "",
        "code_block": "",
    }

"""Validator interface and base classes for PetSitter."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Protocol

from petsitter.models import ValidatorResult


class ValidatorFunction(Protocol):
    """Protocol for validator functions."""

    def __call__(self, code: str, content: str) -> ValidatorResult:
        """Validate code/content and return result."""
        ...


class BaseValidator(ABC):
    """Base class for validators."""

    name: str = "base"
    description: str = "Base validator"

    @abstractmethod
    def validate(self, code: str, content: str) -> ValidatorResult:
        """Validate code/content and return result.

        Args:
            code: Code block to validate
            content: Full response content

        Returns:
            ValidatorResult with pass/fail status and feedback
        """
        ...

    def extract_code_blocks(self, content: str, language: str | None = None) -> list[str]:
        """Extract code blocks from content.

        Args:
            content: Full response content
            language: Optional language filter (e.g., "python")

        Returns:
            List of code blocks
        """
        if language:
            pattern = rf"```{language}\n(.*?)```"
        else:
            pattern = r"```(?:\w+)?\n(.*?)```"

        matches = re.findall(pattern, content, re.DOTALL)
        return [match.strip() for match in matches]

    def __call__(self, code: str, content: str) -> ValidatorResult:
        """Allow calling validator as a function."""
        return self.validate(code, content)


def extract_python_code_blocks(content: str) -> list[str]:
    """Extract Python code blocks from content."""
    patterns = [
        r"```python\n(.*?)```",
        r"```py\n(.*?)```",
    ]

    code_blocks = []
    for pattern in patterns:
        matches = re.findall(pattern, content, re.DOTALL)
        code_blocks.extend([match.strip() for match in matches])

    return code_blocks


def extract_all_code_blocks(content: str) -> list[tuple[str, str]]:
    """Extract all code blocks with their language.

    Returns:
        List of (language, code) tuples
    """
    pattern = r"```(\w+)?\n(.*?)```"
    matches = re.findall(pattern, content, re.DOTALL)
    return [(lang or "unknown", code.strip()) for lang, code in matches]

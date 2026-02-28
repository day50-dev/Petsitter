"""Task configuration models for PetSitter harness."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class TaskConfig(BaseModel):
    """Configuration for a task loaded from YAML."""

    name: str
    description: str = ""
    model: str | None = None
    forward_to: str | None = None
    validators: list[str] = Field(default_factory=list)
    system_prompt: str | None = None
    system_prompt_file: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    max_retries: int = 3

    model_config = {"extra": "allow"}


class Task(BaseModel):
    """A runnable task with its configuration."""

    name: str
    description: str = ""
    model: str | None = None
    forward_to: str | None = None
    validators: list[str] = Field(default_factory=list)
    system_prompt: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    max_retries: int = 3
    source: Path | None = None

    @classmethod
    def from_config(cls, config: TaskConfig, source: Path | None = None, system_prompt: str = "") -> Task:
        """Create a Task from a TaskConfig."""
        return cls(
            name=config.name,
            description=config.description,
            model=config.model,
            forward_to=config.forward_to,
            validators=config.validators,
            system_prompt=system_prompt,
            params=config.params,
            max_retries=config.max_retries,
            source=source,
        )
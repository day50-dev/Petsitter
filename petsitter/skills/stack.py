"""Skill stacking and merging for PetSitter."""

from __future__ import annotations

from petsitter.models import Skill


class StackedSkills:
    """Represents a stack of merged skills."""

    def __init__(self, skills: list[Skill]):
        self.skills = skills
        self._merged_system_prompt: str | None = None
        self._merged_validators: list[str] | None = None
        self._model_pin: str | None = None

    @property
    def system_prompt(self) -> str:
        """Get merged system prompt from all skills."""
        if self._merged_system_prompt is None:
            prompts = []
            for skill in self.skills:
                if skill.system_prompt:
                    prompts.append(f"## {skill.name}\n\n{skill.system_prompt}")
            self._merged_system_prompt = "\n\n".join(prompts)
        return self._merged_system_prompt

    @property
    def validators(self) -> list[str]:
        """Get combined validators from all skills (deduplicated, preserving order)."""
        if self._merged_validators is None:
            seen = set()
            validators = []
            for skill in self.skills:
                for validator in skill.validators:
                    if validator not in seen:
                        seen.add(validator)
                        validators.append(validator)
            self._merged_validators = validators
        return self._merged_validators

    @property
    def model_pin(self) -> str | None:
        """Get the model pin from the last skill that defines one."""
        if self._model_pin is None:
            for skill in reversed(self.skills):
                if skill.model_pin:
                    self._model_pin = skill.model_pin
                    break
        return self._model_pin

    @property
    def skill_names(self) -> list[str]:
        """Get list of skill names in order."""
        return [skill.name for skill in self.skills]

    def get_skill_by_name(self, name: str) -> Skill | None:
        """Get a skill by name."""
        for skill in self.skills:
            if skill.name == name:
                return skill
        return None

    def __len__(self) -> int:
        return len(self.skills)

    def __bool__(self) -> bool:
        return len(self.skills) > 0

    def __repr__(self) -> str:
        return f"StackedSkills({self.skill_names})"


def stack_skills(skills: list[Skill]) -> StackedSkills:
    """Stack multiple skills into a merged skill set.

    Args:
        skills: List of skills to stack (order matters)

    Returns:
        StackedSkills object with merged configuration
    """
    return StackedSkills(skills)


def create_system_message(
    stacked_skills: StackedSkills,
    base_system_prompt: str | None = None,
) -> str:
    """Create a combined system message from stacked skills.

    Args:
        stacked_skills: Stacked skills to combine
        base_system_prompt: Optional base system prompt to prepend

    Returns:
        Combined system message
    """
    parts = []

    if base_system_prompt:
        parts.append(base_system_prompt)

    if stacked_skills.system_prompt:
        parts.append(stacked_skills.system_prompt)

    # Add validator notice
    if stacked_skills.validators:
        validator_list = ", ".join(stacked_skills.validators)
        parts.append(
            f"\n## Output Validation\n\n"
            f"Your output will be validated by: {validator_list}. "
            f"Ensure your response passes all validators."
        )

    return "\n\n".join(parts)

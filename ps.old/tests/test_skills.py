"""Tests for PetSitter skill loading."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from petsitter.skills.loader import (
    GitHubSkillLoader,
    LocalSkillLoader,
    load_skill,
    load_skills,
    parse_skill_reference,
)
from petsitter.skills.stack import StackedSkills, create_system_message, stack_skills


class TestLocalSkillLoader:
    """Tests for LocalSkillLoader."""

    def test_load_valid_skill(self, test_skills_dir: Path) -> None:
        """Test loading a valid skill from local directory."""
        loader = LocalSkillLoader()
        skill = loader.load(str(test_skills_dir / "test_skill"))

        assert skill.name == "test_skill"
        assert skill.description == "A test skill"
        assert skill.validators == ["no_eval_exec"]
        assert skill.version == "1.0.0"
        assert "Test Skill" in skill.system_prompt

    def test_load_skill_with_base_dir(self, test_skills_dir: Path) -> None:
        """Test loading skill with base directory."""
        loader = LocalSkillLoader(base_dir=test_skills_dir)
        skill = loader.load("test_skill")

        assert skill.name == "test_skill"

    def test_load_nonexistent_skill(self) -> None:
        """Test loading nonexistent skill raises error."""
        loader = LocalSkillLoader()
        with pytest.raises(FileNotFoundError):
            loader.load("/nonexistent/path")

    def test_load_file_not_directory(self, tmp_path: Path) -> None:
        """Test loading a file instead of directory raises error."""
        test_file = tmp_path / "not_a_dir.txt"
        test_file.write_text("test")

        loader = LocalSkillLoader()
        with pytest.raises(ValueError, match="must be a directory"):
            loader.load(str(test_file))

    def test_load_skill_missing_yaml(self, tmp_path: Path) -> None:
        """Test loading skill without skill.yaml raises error."""
        loader = LocalSkillLoader()
        with pytest.raises(FileNotFoundError, match="skill.yaml"):
            loader.load(str(tmp_path))


class TestParseSkillReference:
    """Tests for parse_skill_reference."""

    def test_parse_local_reference(self) -> None:
        """Test parsing local skill reference."""
        loader_type, path = parse_skill_reference("/local/path")
        assert loader_type == "local"
        assert path == "/local/path"

    def test_parse_github_reference(self) -> None:
        """Test parsing GitHub skill reference."""
        loader_type, path = parse_skill_reference("github://owner/repo:skill")
        assert loader_type == "github"
        assert path == "github://owner/repo:skill"

    def test_parse_relative_reference(self) -> None:
        """Test parsing relative skill reference."""
        loader_type, path = parse_skill_reference("skills/my_skill")
        assert loader_type == "local"
        assert path == "skills/my_skill"


class TestLoadSkill:
    """Tests for load_skill function."""

    def test_load_local_skill(self, test_skills_dir: Path) -> None:
        """Test loading local skill via convenience function."""
        skill = load_skill(str(test_skills_dir / "test_skill"))
        assert skill.name == "test_skill"

    def test_load_multiple_skills(self, test_skills_dir: Path) -> None:
        """Test loading multiple skills."""
        # Create second skill
        skill2_dir = test_skills_dir / "skill2"
        skill2_dir.mkdir()
        (skill2_dir / "skill.yaml").write_text("""
name: skill2
description: Second skill
validators: []
""")

        skills = load_skills([
            str(test_skills_dir / "test_skill"),
            str(skill2_dir),
        ])

        assert len(skills) == 2
        assert skills[0].name == "test_skill"
        assert skills[1].name == "skill2"


class TestStackedSkills:
    """Tests for StackedSkills."""

    def test_stack_single_skill(self, sample_skill: "Skill") -> None:
        """Test stacking a single skill."""
        stacked = stack_skills([sample_skill])

        assert len(stacked) == 1
        assert stacked.skill_names == ["test_skill"]
        assert stacked.model_pin == "test-model"

    def test_stack_multiple_skills(self, sample_skill: "Skill") -> None:
        """Test stacking multiple skills."""
        from petsitter.models import Skill

        skill2 = Skill(
            name="skill2",
            description="Second skill",
            validators=["validator2"],
            model_pin="model2",
            system_prompt="# Skill 2",
        )

        stacked = stack_skills([sample_skill, skill2])

        assert len(stacked) == 2
        assert stacked.skill_names == ["test_skill", "skill2"]
        # Last skill's model_pin takes precedence
        assert stacked.model_pin == "model2"

    def test_merged_validators(self, sample_skill: "Skill") -> None:
        """Test merged validators from stacked skills."""
        from petsitter.models import Skill

        skill2 = Skill(
            name="skill2",
            validators=["validator2", "validator3"],
        )

        stacked = stack_skills([sample_skill, skill2])

        # Validators should be deduplicated and preserve order
        assert stacked.validators == ["ruff_lint", "no_eval_exec", "validator2", "validator3"]

    def test_merged_validators_deduplication(self, sample_skill: "Skill") -> None:
        """Test validator deduplication."""
        from petsitter.models import Skill

        skill2 = Skill(
            name="skill2",
            validators=["ruff_lint", "new_validator"],
        )

        stacked = stack_skills([sample_skill, skill2])

        # ruff_lint should only appear once
        assert stacked.validators.count("ruff_lint") == 1

    def test_merged_system_prompt(self, sample_skill: "Skill") -> None:
        """Test merged system prompts."""
        from petsitter.models import Skill

        skill2 = Skill(
            name="skill2",
            system_prompt="# Skill 2 Prompt",
        )

        stacked = stack_skills([sample_skill, skill2])

        assert "test_skill" in stacked.system_prompt
        assert "Skill 2 Prompt" in stacked.system_prompt

    def test_get_skill_by_name(self, sample_skill: "Skill") -> None:
        """Test getting skill by name."""
        stacked = stack_skills([sample_skill])

        skill = stacked.get_skill_by_name("test_skill")
        assert skill is not None
        assert skill.name == "test_skill"

    def test_get_nonexistent_skill(self, sample_skill: "Skill") -> None:
        """Test getting nonexistent skill."""
        stacked = stack_skills([sample_skill])

        skill = stacked.get_skill_by_name("nonexistent")
        assert skill is None

    def test_empty_stack(self) -> None:
        """Test empty skill stack."""
        stacked = stack_skills([])

        assert len(stacked) == 0
        assert bool(stacked) is False
        assert stacked.system_prompt == ""
        assert stacked.validators == []

    def test_stack_repr(self, sample_skill: "Skill") -> None:
        """Test string representation of stacked skills."""
        stacked = stack_skills([sample_skill])
        assert "test_skill" in repr(stacked)


class TestCreateSystemMessage:
    """Tests for create_system_message."""

    def test_create_with_skills(self, sample_skill: "Skill") -> None:
        """Test creating system message with skills."""
        stacked = stack_skills([sample_skill])
        message = create_system_message(stacked)

        assert "test_skill" in message
        assert "Validation" in message

    def test_create_with_base_prompt(self, sample_skill: "Skill") -> None:
        """Test creating system message with base prompt."""
        stacked = stack_skills([sample_skill])
        message = create_system_message(
            stacked,
            base_system_prompt="You are a helpful assistant.",
        )

        assert "helpful assistant" in message
        assert "test_skill" in message

    def test_create_without_skills(self) -> None:
        """Test creating system message without skills."""
        from petsitter.models import Skill

        stacked = stack_skills([])
        message = create_system_message(stacked)

        assert message == ""

    def test_create_with_base_only(self) -> None:
        """Test creating system message with only base prompt."""
        stacked = stack_skills([])
        message = create_system_message(
            stacked,
            base_system_prompt="Base prompt only",
        )

        assert message == "Base prompt only"

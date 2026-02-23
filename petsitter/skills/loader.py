"""Skill loading for PetSitter."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Protocol

import yaml

from petsitter.models import Skill, SkillConfig


class SkillLoader(Protocol):
    """Protocol for skill loaders."""

    def load(self, skill_ref: str) -> Skill:
        """Load a skill from a reference (path or URL)."""
        ...


class LocalSkillLoader:
    """Load skills from local filesystem."""

    def __init__(self, base_dir: Path | None = None):
        self.base_dir = base_dir

    def load(self, skill_ref: str) -> Skill:
        """Load a skill from a local path."""
        skill_path = Path(skill_ref)

        # If relative path and base_dir is set, resolve relative to base_dir
        if not skill_path.is_absolute() and self.base_dir:
            skill_path = self.base_dir / skill_path

        if not skill_path.exists():
            raise FileNotFoundError(f"Skill directory not found: {skill_path}")

        if not skill_path.is_dir():
            raise ValueError(f"Skill path must be a directory: {skill_path}")

        # Load skill.yaml
        skill_yaml = skill_path / "skill.yaml"
        if not skill_yaml.exists():
            raise FileNotFoundError(f"skill.yaml not found in: {skill_path}")

        with open(skill_yaml) as f:
            config_data = yaml.safe_load(f)

        config = SkillConfig(**config_data)

        # Load system prompt if exists
        system_prompt = ""
        system_prompt_file = skill_path / "system_prompt.md"
        if system_prompt_file.exists():
            with open(system_prompt_file) as f:
                system_prompt = f.read()

        # Discover validators
        validators_dir = skill_path / "validators"
        validators = config.validators.copy()  # Start with yaml-defined validators

        if validators_dir.exists() and validators_dir.is_dir():
            for validator_file in validators_dir.glob("*.py"):
                validator_name = validator_file.stem
                if validator_name != "__init__" and validator_name not in validators:
                    validators.append(validator_name)

        # Discover tools
        tools = []
        tools_dir = skill_path / "tools"
        if tools_dir.exists() and tools_dir.is_dir():
            for tool_file in tools_dir.glob("*.py"):
                tool_name = tool_file.stem
                if tool_name != "__init__":
                    tools.append(tool_name)

        return Skill(
            name=config.name,
            description=config.description,
            validators=validators,
            model_pin=config.model_pin,
            version=config.version,
            system_prompt=system_prompt,
            source=str(skill_path),
            tools=tools,
        )


class GitHubSkillLoader:
    """Load skills from GitHub repositories."""

    def __init__(self, cache_dir: Path | None = None):
        self.cache_dir = cache_dir or Path(tempfile.gettempdir()) / "petsitter" / "skills"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def load(self, skill_ref: str) -> Skill:
        """Load a skill from a GitHub reference.

        Format: github://owner/repo:path/to/skill or github://owner/repo:skill_name:ref
        """
        if not skill_ref.startswith("github://"):
            raise ValueError(f"Invalid GitHub skill reference: {skill_ref}")

        # Parse github://owner/repo:path[:ref]
        github_path = skill_ref[9:]  # Remove "github://"

        # Split by : to get path and optional ref
        parts = github_path.split(":")
        if len(parts) > 3:
            raise ValueError(f"Invalid GitHub skill reference format: {skill_ref}")

        repo_and_path = parts[0]
        skill_name = parts[1] if len(parts) > 1 else None
        ref = parts[2] if len(parts) > 2 else "main"

        # Split owner/repo from path
        if "/" not in repo_and_path:
            raise ValueError(f"Invalid GitHub repo format: {repo_and_path}")

        owner_repo, *skill_path_parts = repo_and_path.split("/", 2)
        if len(owner_repo.split("/")) != 2:
            raise ValueError(f"Invalid GitHub repo format: {owner_repo}")

        owner, repo = owner_repo.split("/")
        skill_path_in_repo = "/".join(skill_path_parts) if skill_path_parts else ""

        # Determine skill directory name for caching
        cache_name = f"{owner}_{repo}_{skill_name or skill_path_in_repo.replace('/', '_')}_{ref}"
        cache_path = self.cache_dir / cache_name

        # Clone or update the repo
        if cache_path.exists():
            # Update existing clone
            subprocess.run(
                ["git", "pull", "origin", ref],
                cwd=cache_path,
                capture_output=True,
                check=True,
            )
        else:
            # Clone new
            repo_url = f"https://github.com/{owner}/{repo}.git"
            subprocess.run(
                ["git", "clone", "--depth", "1", "--branch", ref, repo_url, str(cache_path)],
                capture_output=True,
                check=True,
            )

        # Find the skill directory
        if skill_path_in_repo:
            skill_dir = cache_path / skill_path_in_repo
        elif skill_name:
            skill_dir = cache_path / skill_name
        else:
            # Assume root of repo is the skill
            skill_dir = cache_path

        # Use local loader for the rest
        local_loader = LocalSkillLoader()
        return local_loader.load(str(skill_dir))


def parse_skill_reference(skill_ref: str) -> tuple[str, str]:
    """Parse a skill reference and return (type, path).

    Returns:
        Tuple of (loader_type, skill_path)
    """
    if skill_ref.startswith("github://"):
        return ("github", skill_ref)
    elif skill_ref.startswith("http://") or skill_ref.startswith("https://"):
        raise NotImplementedError("HTTP skill loading not yet implemented")
    else:
        return ("local", skill_ref)


def load_skill(skill_ref: str, skills_dir: Path | None = None) -> Skill:
    """Load a skill from a reference.

    Args:
        skill_ref: Local path or github:// URL
        skills_dir: Base directory for local skills

    Returns:
        Loaded Skill object
    """
    loader_type, path = parse_skill_reference(skill_ref)

    if loader_type == "local":
        loader = LocalSkillLoader(base_dir=skills_dir)
    elif loader_type == "github":
        loader = GitHubSkillLoader()
    else:
        raise ValueError(f"Unknown loader type: {loader_type}")

    return loader.load(path)


def load_skills(skill_refs: list[str], skills_dir: Path | None = None) -> list[Skill]:
    """Load multiple skills from references.

    Args:
        skill_refs: List of skill references
        skills_dir: Base directory for local skills

    Returns:
        List of loaded Skill objects
    """
    skills = []
    for ref in skill_refs:
        skill = load_skill(ref, skills_dir)
        skills.append(skill)
    return skills

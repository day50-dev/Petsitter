"""Task loading for PetSitter harness."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

import yaml

from petsitter.harness.models import Task, TaskConfig


class TaskLoader(Protocol):
    """Protocol for task loaders."""

    def load(self, task_ref: str) -> Task:
        """Load a task from a reference (path or URL)."""
        ...


class LocalTaskLoader:
    """Load tasks from local filesystem."""

    def __init__(self, base_dir: Path | None = None):
        self.base_dir = base_dir

    def load(self, task_ref: str) -> Task:
        """Load a task from a local YAML file."""
        task_path = Path(task_ref)

        if not task_path.is_absolute() and self.base_dir:
            task_path = self.base_dir / task_path

        if not task_path.exists():
            raise FileNotFoundError(f"Task file not found: {task_path}")

        if not task_path.is_file():
            raise ValueError(f"Task path must be a file: {task_path}")

        return self._load_from_file(task_path)

    def _load_from_file(self, task_path: Path) -> Task:
        """Load a task from a YAML file."""
        with open(task_path) as f:
            config_data = yaml.safe_load(f)

        config = TaskConfig(**config_data)

        system_prompt = ""
        if config.system_prompt_file:
            prompt_file = task_path.parent / config.system_prompt_file
            if prompt_file.exists():
                with open(prompt_file) as f:
                    system_prompt = f.read()
        elif config.system_prompt:
            system_prompt = config.system_prompt

        return Task.from_config(config, source=task_path, system_prompt=system_prompt)


class ModelTaskLoader:
    """Load tasks from the harness models directory."""

    def __init__(self, harness_dir: Path | None = None):
        self.harness_dir = harness_dir or self._find_harness_dir()

    def _find_harness_dir(self) -> Path:
        """Find the harness directory relative to this module."""
        current = Path(__file__).parent
        return current

    def load(self, task_ref: str) -> Task:
        """Load a task from harness/<model>/<task>.yaml format.

        Args:
            task_ref: Either 'model/task' format or direct path to YAML file

        Returns:
            Loaded Task object
        """
        task_path = Path(task_ref)

        if task_path.exists():
            loader = LocalTaskLoader()
            return loader.load(task_ref)

        if "/" in task_ref:
            parts = task_ref.split("/")
            if len(parts) == 2:
                model_name, task_name = parts
                model_dir = self.harness_dir / "models" / model_name
                task_file = model_dir / f"{task_name}.yaml"
                if task_file.exists():
                    loader = LocalTaskLoader()
                    return loader.load(str(task_file))

        model_dir = self.harness_dir / "models" / task_ref.replace(".yaml", "")
        if model_dir.exists() and model_dir.is_dir():
            return self._load_default_task(model_dir)

        raise FileNotFoundError(f"Task not found: {task_ref}")

    def _load_default_task(self, model_dir: Path) -> Task:
        """Load the default task from a model directory."""
        task_file = model_dir / "default.yaml"
        if task_file.exists():
            loader = LocalTaskLoader()
            return loader.load(str(task_file))

        for yaml_file in model_dir.glob("*.yaml"):
            loader = LocalTaskLoader()
            return loader.load(str(yaml_file))

        raise FileNotFoundError(f"No task files found in: {model_dir}")


def load_task(task_ref: str, base_dir: Path | None = None) -> Task:
    """Load a task from a reference.

    Args:
        task_ref: Path to task YAML file or 'model/task' format
        base_dir: Base directory for relative paths

    Returns:
        Loaded Task object
    """
    if base_dir:
        loader = LocalTaskLoader(base_dir=base_dir)
        return loader.load(task_ref)

    if "/" in task_ref and not Path(task_ref).exists():
        loader = ModelTaskLoader()
        return loader.load(task_ref)

    loader = LocalTaskLoader()
    return loader.load(task_ref)


def load_tasks_from_model(model_name: str) -> list[Task]:
    """Load all tasks from a model directory.

    Args:
        model_name: Name of the model directory under harness/models/

    Returns:
        List of loaded Task objects
    """
    harness_dir = Path(__file__).parent
    model_dir = harness_dir / "models" / model_name

    if not model_dir.exists():
        raise FileNotFoundError(f"Model directory not found: {model_dir}")

    tasks = []
    loader = LocalTaskLoader()

    for yaml_file in model_dir.glob("*.yaml"):
        try:
            task = loader.load(str(yaml_file))
            tasks.append(task)
        except Exception:
            continue

    return tasks
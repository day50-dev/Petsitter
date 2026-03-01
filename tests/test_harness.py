"""Tests for the harness module."""

from pathlib import Path

import pytest

from petsitter.harness.loader import load_task, load_tasks_from_model


def test_load_task_from_yaml(tmp_path):
    """Test loading a task from a YAML file."""
    task_yaml = tmp_path / "task.yaml"
    task_yaml.write_text("""
name: test-task
description: A test task
model: qwen3
validators:
  - no_eval_exec
max_retries: 2
""")
    
    task = load_task(str(task_yaml))
    assert task.name == "test-task"
    assert task.description == "A test task"
    assert task.model == "qwen3"
    assert task.validators == ["no_eval_exec"]
    assert task.max_retries == 2


def test_load_task_with_system_prompt_file(tmp_path):
    """Test loading a task with a system prompt file."""
    task_yaml = tmp_path / "task.yaml"
    task_yaml.write_text("""
name: test-task
system_prompt_file: prompt.md
""")
    
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("You are a helpful assistant.")
    
    task = load_task(str(task_yaml))
    assert task.system_prompt == "You are a helpful assistant."


def test_load_task_with_inline_system_prompt(tmp_path):
    """Test loading a task with inline system prompt."""
    task_yaml = tmp_path / "task.yaml"
    task_yaml.write_text("""
name: test-task
system_prompt: You are an expert coder.
""")
    
    task = load_task(str(task_yaml))
    assert task.system_prompt == "You are an expert coder."


def test_load_task_from_harness_model_path():
    """Test loading a task using model/task format."""
    task = load_task("sample-model/programming")
    assert task.name == "programming"
    assert task.model == "qwen3"


def test_load_tasks_from_model():
    """Test loading all tasks from a model directory."""
    tasks = load_tasks_from_model("sample-model")
    assert len(tasks) >= 1
    
    programming_task = next((t for t in tasks if t.name == "programming"), None)
    assert programming_task is not None
    assert programming_task.model == "qwen3"
"""Tests for PetSitter configuration."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from petsitter.config import Config, create_parser, parse_args


class TestConfig:
    """Tests for Config dataclass."""

    def test_default_config(self) -> None:
        """Test default configuration values."""
        config = Config()
        assert config.host == "0.0.0.0"
        assert config.port == 8000
        assert config.model == "qwen3"
        assert config.max_retries == 3

    def test_custom_config(self) -> None:
        """Test custom configuration values."""
        config = Config(
            host="localhost",
            port=9000,
            model="test-model",
            max_retries=5,
        )
        assert config.host == "localhost"
        assert config.port == 9000
        assert config.model == "test-model"
        assert config.max_retries == 5


class TestConfigFromArgs:
    """Tests for Config.from_args."""

    def test_from_args_basic(self) -> None:
        """Test creating config from basic args."""
        args = argparse.Namespace(
            host="localhost",
            port=9000,
            backend="ollama",
            model="test-model",
            ollama_url="http://localhost:11434",
            skills=[],
            skills_dir=None,
            max_retries=5,
            no_early_fail=False,
            escalate=False,
            escalate_model="claude-3",
            escalate_api_key=None,
            verbose=False,
            log_file=None,
        )
        config = Config.from_args(args)
        assert config.host == "localhost"
        assert config.port == 9000
        assert config.model == "test-model"
        assert config.max_retries == 5

    def test_from_args_with_skills(self) -> None:
        """Test creating config with skills."""
        args = argparse.Namespace(
            host="0.0.0.0",
            port=8000,
            backend="ollama",
            model="qwen3",
            ollama_url="http://localhost:11434",
            skills=["skill1", "skill2"],
            skills_dir="/skills",
            max_retries=3,
            no_early_fail=False,
            escalate=False,
            escalate_model="claude-3",
            escalate_api_key=None,
            verbose=True,
            log_file=None,
        )
        config = Config.from_args(args)
        assert config.skills == ["skill1", "skill2"]
        assert config.skills_dir == Path("/skills")
        assert config.verbose is True


class TestConfigFromYaml:
    """Tests for Config.from_yaml."""

    def test_from_yaml_file(self, tmp_path: Path) -> None:
        """Test loading config from YAML file."""
        config_file = tmp_path / "config.yaml"
        config_data = {
            "host": "localhost",
            "port": 9000,
            "model": "yaml-model",
            "max_retries": 5,
            "verbose": True,
        }
        with open(config_file, "w") as f:
            yaml.dump(config_data, f)

        config = Config.from_yaml(config_file)
        assert config.host == "localhost"
        assert config.port == 9000
        assert config.model == "yaml-model"
        assert config.max_retries == 5
        assert config.verbose is True

    def test_from_yaml_defaults(self, tmp_path: Path) -> None:
        """Test loading config with missing values uses defaults."""
        config_file = tmp_path / "config.yaml"
        with open(config_file, "w") as f:
            f.write("{}")

        config = Config.from_yaml(config_file)
        assert config.host == "0.0.0.0"
        assert config.port == 8000
        assert config.model == "qwen3"


class TestConfigToYaml:
    """Tests for Config.to_yaml."""

    def test_to_yaml_file(self, tmp_path: Path) -> None:
        """Test saving config to YAML file."""
        config = Config(host="localhost", port=9000)
        config_file = tmp_path / "output.yaml"

        config.to_yaml(config_file)

        with open(config_file) as f:
            data = yaml.safe_load(f)

        assert data["host"] == "localhost"
        assert data["port"] == 9000


class TestParser:
    """Tests for argument parser."""

    def test_create_parser(self) -> None:
        """Test parser creation."""
        parser = create_parser()
        assert parser is not None

    def test_parse_serve_args(self) -> None:
        """Test parsing serve command args."""
        args = parse_args([
            "serve",
            "--host", "localhost",
            "--port", "9000",
            "--model", "test-model",
            "--max-retries", "5",
        ])
        assert args.command == "serve"
        assert args.host == "localhost"
        assert args.port == 9000
        assert args.model == "test-model"
        assert args.max_retries == 5

    def test_parse_skills_args(self) -> None:
        """Test parsing skills arguments."""
        args = parse_args([
            "serve",
            "--skills", "skill1", "skill2",
            "--skills-dir", "/skills",
        ])
        assert args.skills == ["skill1", "skill2"]
        assert args.skills_dir == "/skills"

    def test_parse_verbose_flag(self) -> None:
        """Test parsing verbose flag."""
        args = parse_args(["serve", "--verbose"])
        assert args.verbose is True

    def test_parse_escalate_args(self) -> None:
        """Test parsing escalation arguments."""
        args = parse_args([
            "serve",
            "--escalate",
            "--escalate-model", "gpt-4",
        ])
        assert args.escalate is True
        assert args.escalate_model == "gpt-4"

    def test_default_args(self) -> None:
        """Test default argument values."""
        args = parse_args(["serve"])
        assert args.host == "0.0.0.0"
        assert args.port == 8000
        assert args.model == "qwen3"
        assert args.max_retries == 3

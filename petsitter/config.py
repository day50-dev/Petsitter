"""Configuration and CLI argument parsing for PetSitter."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Config:
    """PetSitter configuration."""

    # Server settings
    host: str = "0.0.0.0"
    port: int = 8000

    # LLM backend settings
    backend: str = "ollama"
    model: str = "qwen3"
    ollama_base_url: str = "http://localhost:11434"

    # Skill settings
    skills: list[str] = field(default_factory=list)
    skills_dir: Path | None = None

    # Retry settings
    max_retries: int = 3
    early_fail: bool = True

    # Escalation settings
    escalate: bool = False
    escalate_model: str = "claude-3-sonnet-20240229"
    escalate_api_key: str | None = None

    # Logging settings
    verbose: bool = False
    log_file: Path | None = None

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> Config:
        """Create Config from parsed arguments."""
        return cls(
            host=args.host,
            port=args.port,
            backend=args.backend,
            model=args.model,
            ollama_base_url=args.ollama_url,
            skills=args.skills,
            skills_dir=Path(args.skills_dir) if args.skills_dir else None,
            max_retries=args.max_retries,
            early_fail=not args.no_early_fail,
            escalate=args.escalate,
            escalate_model=args.escalate_model,
            escalate_api_key=args.escalate_api_key or os.environ.get("ESCALATE_API_KEY"),
            verbose=args.verbose,
            log_file=Path(args.log_file) if args.log_file else None,
        )

    @classmethod
    def from_yaml(cls, path: Path) -> Config:
        """Load configuration from YAML file."""
        with open(path) as f:
            data = yaml.safe_load(f)

        return cls(
            host=data.get("host", "0.0.0.0"),
            port=data.get("port", 8000),
            backend=data.get("backend", "ollama"),
            model=data.get("model", "qwen3"),
            ollama_base_url=data.get("ollama_base_url", "http://localhost:11434"),
            skills=data.get("skills", []),
            skills_dir=Path(data["skills_dir"]) if data.get("skills_dir") else None,
            max_retries=data.get("max_retries", 3),
            early_fail=data.get("early_fail", True),
            escalate=data.get("escalate", False),
            escalate_model=data.get("escalate_model", "claude-3-sonnet-20240229"),
            escalate_api_key=data.get("escalate_api_key") or os.environ.get("ESCALATE_API_KEY"),
            verbose=data.get("verbose", False),
            log_file=Path(data["log_file"]) if data.get("log_file") else None,
        )

    def to_yaml(self, path: Path) -> None:
        """Save configuration to YAML file."""
        data = {
            "host": self.host,
            "port": self.port,
            "backend": self.backend,
            "model": self.model,
            "ollama_base_url": self.ollama_base_url,
            "skills": self.skills,
            "skills_dir": str(self.skills_dir) if self.skills_dir else None,
            "max_retries": self.max_retries,
            "early_fail": self.early_fail,
            "escalate": self.escalate,
            "escalate_model": self.escalate_model,
            "verbose": self.verbose,
            "log_file": str(self.log_file) if self.log_file else None,
        }
        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False)


def create_parser() -> argparse.ArgumentParser:
    """Create the argument parser."""
    parser = argparse.ArgumentParser(
        prog="petsitter",
        description="PetSitter - A lightweight proxy and babysitter for local LLMs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic usage with default settings
  petsitter serve

  # With stacked skills
  petsitter serve --skills github://emoRobot/programming:qwen3 github://emoRobot/soc-2:qwen3

  # Custom model and port
  petsitter serve --model nanbeige4.1:3b --port 9000

  # With verbose logging
  petsitter serve --verbose

  # Load config from YAML
  petsitter serve --config config.yaml
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Serve command
    serve_parser = subparsers.add_parser("serve", help="Start the PetSitter proxy server")
    serve_parser.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="Host to bind to (default: 0.0.0.0)",
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to bind to (default: 8000)",
    )
    serve_parser.add_argument(
        "--backend",
        type=str,
        choices=["ollama", "llamacpp"],
        default="ollama",
        help="LLM backend to use (default: ollama)",
    )
    serve_parser.add_argument(
        "--model",
        type=str,
        default="qwen3",
        help="Default model to use (default: qwen3)",
    )
    serve_parser.add_argument(
        "--ollama-url",
        type=str,
        default="http://localhost:11434",
        help="Ollama base URL (default: http://localhost:11434)",
    )
    serve_parser.add_argument(
        "--skills",
        type=str,
        nargs="*",
        default=[],
        help="Skills to load (local paths or github:// URLs)",
    )
    serve_parser.add_argument(
        "--skills-dir",
        type=str,
        default=None,
        help="Directory to search for local skills",
    )
    serve_parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Maximum retry attempts (default: 3)",
    )
    serve_parser.add_argument(
        "--no-early-fail",
        action="store_true",
        help="Disable early-fail optimization",
    )
    serve_parser.add_argument(
        "--escalate",
        action="store_true",
        help="Enable escalation to remote models on max retries",
    )
    serve_parser.add_argument(
        "--escalate-model",
        type=str,
        default="claude-3-sonnet-20240229",
        help="Model to escalate to (default: claude-3-sonnet-20240229)",
    )
    serve_parser.add_argument(
        "--escalate-api-key",
        type=str,
        default=None,
        help="API key for escalation (or set ESCALATE_API_KEY env var)",
    )
    serve_parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    serve_parser.add_argument(
        "--log-file",
        type=str,
        default=None,
        help="Path to log file",
    )
    serve_parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to YAML config file",
    )

    # Search command (placeholder for future)
    subparsers.add_parser("search", help="Search for skills (not yet implemented)")

    return parser


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = create_parser()
    return parser.parse_args(args)

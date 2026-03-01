"""Logging and metrics for PetSitter."""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from petsitter.config import Config


@dataclass
class RequestMetrics:
    """Metrics for a single request."""

    request_id: str
    timestamp: datetime = field(default_factory=datetime.now)
    model: str = ""
    skills: list[str] = field(default_factory=list)
    retries: int = 0
    validators_run: int = 0
    validators_passed: int = 0
    validators_failed: int = 0
    escalated: bool = False
    duration_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class AggregateMetrics:
    """Aggregate metrics across all requests."""

    total_requests: int = 0
    total_retries: int = 0
    total_escalations: int = 0
    total_validator_runs: int = 0
    total_validator_failures: int = 0
    requests_with_retries: int = 0
    requests_escalated: int = 0
    avg_retries_per_request: float = 0.0
    avg_duration_ms: float = 0.0
    _durations: list[float] = field(default_factory=list, repr=False)

    def add_request(self, metrics: RequestMetrics) -> None:
        """Add a request's metrics to the aggregate."""
        self.total_requests += 1
        self.total_retries += metrics.retries
        self.total_escalations += 1 if metrics.escalated else 0
        self.total_validator_runs += metrics.validators_run
        self.total_validator_failures += metrics.validators_failed

        if metrics.retries > 0:
            self.requests_with_retries += 1
        if metrics.escalated:
            self.requests_escalated += 1

        self._durations.append(metrics.duration_ms)
        self.avg_retries_per_request = self.total_retries / self.total_requests
        self.avg_duration_ms = sum(self._durations) / len(self._durations)


class PetSitterLogger:
    """Custom logger for PetSitter with metrics tracking."""

    def __init__(self, config: Config):
        self.config = config
        self.logger = logging.getLogger("petsitter")
        self.logger.setLevel(logging.DEBUG if config.verbose else logging.INFO)

        # Console handler
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.DEBUG if config.verbose else logging.INFO)
        console_format = logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        console_handler.setFormatter(console_format)
        self.logger.addHandler(console_handler)

        # File handler (optional)
        if config.log_file:
            config.log_file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(config.log_file)
            file_handler.setLevel(logging.DEBUG)
            file_format = logging.Formatter(
                "%(asctime)s | %(levelname)-8s | %(name)s | %(funcName)s:%(lineno)d | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
            file_handler.setFormatter(file_format)
            self.logger.addHandler(file_handler)

        # Metrics tracking
        self.metrics = AggregateMetrics()
        self._request_metrics: dict[str, RequestMetrics] = {}

    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """Log debug message."""
        self.logger.debug(msg, *args, **kwargs)

    def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """Log info message."""
        self.logger.info(msg, *args, **kwargs)

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """Log warning message."""
        self.logger.warning(msg, *args, **kwargs)

    def error(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """Log error message."""
        self.logger.error(msg, *args, **kwargs)

    def start_request(self, request_id: str, model: str, skills: list[str]) -> None:
        """Track the start of a request."""
        self._request_metrics[request_id] = RequestMetrics(
            request_id=request_id,
            model=model,
            skills=skills,
        )
        self.debug(f"Starting request {request_id} with model={model}, skills={skills}")

    def end_request(
        self,
        request_id: str,
        retries: int = 0,
        validators_run: int = 0,
        validators_passed: int = 0,
        validators_failed: int = 0,
        escalated: bool = False,
        duration_ms: float = 0.0,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        """Track the end of a request."""
        if request_id in self._request_metrics:
            metrics = self._request_metrics[request_id]
            metrics.retries = retries
            metrics.validators_run = validators_run
            metrics.validators_passed = validators_passed
            metrics.validators_failed = validators_failed
            metrics.escalated = escalated
            metrics.duration_ms = duration_ms
            metrics.input_tokens = input_tokens
            metrics.output_tokens = output_tokens

            self.metrics.add_request(metrics)
            del self._request_metrics[request_id]

            self.info(
                f"Request {request_id} completed: retries={retries}, "
                f"validators={validators_passed}/{validators_run}, "
                f"duration={duration_ms:.1f}ms"
                + (", ESCALATED" if escalated else ""),
            )

    def log_validator_result(
        self,
        request_id: str,
        validator_name: str,
        passed: bool,
        errors: list[str] | None = None,
    ) -> None:
        """Log a validator result."""
        status = "PASSED" if passed else "FAILED"
        msg = f"Validator {validator_name}: {status}"
        if errors and not passed:
            msg += f" - {errors[0][:100]}"
        self.debug(msg)

    def log_retry(self, request_id: str, attempt: int, reason: str) -> None:
        """Log a retry attempt."""
        self.info(f"Request {request_id}: Retry {attempt} - {reason}")

    def log_escalation(self, request_id: str, target_model: str) -> None:
        """Log an escalation event."""
        self.warning(f"Request {request_id}: Escalating to {target_model}")

    def get_metrics(self) -> AggregateMetrics:
        """Get current aggregate metrics."""
        return self.metrics


# Global logger instance (set by main app)
_logger: PetSitterLogger | None = None


def get_logger() -> PetSitterLogger:
    """Get the global logger instance."""
    if _logger is None:
        raise RuntimeError("Logger not initialized. Call init_logger() first.")
    return _logger


def init_logger(config: Config) -> PetSitterLogger:
    """Initialize the global logger."""
    global _logger
    _logger = PetSitterLogger(config)
    return _logger

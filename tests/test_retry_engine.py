"""Tests for PetSitter retry engine."""

from __future__ import annotations

import pytest

from petsitter.models import Message, MessageRole, RetryState, ValidatorResult
from petsitter.retry.engine import RetryEngine


class TestRetryEngine:
    """Tests for RetryEngine."""

    def test_create_engine(self) -> None:
        """Test creating retry engine."""
        engine = RetryEngine(max_retries=3)
        assert engine.max_retries == 3
        assert engine.early_fail is True

    def test_create_state(self) -> None:
        """Test creating retry state."""
        engine = RetryEngine()
        state = engine.create_state()

        assert isinstance(state, RetryState)
        assert state.attempt == 0
        assert state.max_retries == 3

    def test_should_retry_true(self) -> None:
        """Test should_retry when retries available."""
        engine = RetryEngine(max_retries=3)
        state = engine.create_state()

        assert engine.should_retry(state) is True

    def test_should_retry_false(self) -> None:
        """Test should_retry when no retries left."""
        engine = RetryEngine(max_retries=2)
        state = engine.create_state()
        state.attempt = 2

        assert engine.should_retry(state) is False

    def test_record_attempt(self) -> None:
        """Test recording an attempt."""
        engine = RetryEngine()
        state = engine.create_state()

        engine.record_attempt(state)
        assert state.attempt == 1

        engine.record_attempt(state)
        assert state.attempt == 2

    def test_process_validator_results_all_pass(self) -> None:
        """Test processing results when all pass."""
        engine = RetryEngine()
        state = engine.create_state()

        results = [
            ValidatorResult(validator_name="v1", passed=True),
            ValidatorResult(validator_name="v2", passed=True),
        ]

        all_passed, feedback = engine.process_validator_results(state, results)

        assert all_passed is True
        assert feedback == ""

    def test_process_validator_results_some_fail(self) -> None:
        """Test processing results when some fail."""
        engine = RetryEngine()
        state = engine.create_state()

        results = [
            ValidatorResult(validator_name="v1", passed=True),
            ValidatorResult(
                validator_name="v2",
                passed=False,
                errors=["Error 1"],
            ),
        ]

        all_passed, feedback = engine.process_validator_results(state, results)

        assert all_passed is False
        assert "v2" in feedback
        assert "Error 1" in feedback

    def test_process_results_updates_state(self) -> None:
        """Test that processing results updates state."""
        engine = RetryEngine()
        state = engine.create_state()

        results = [
            ValidatorResult(
                validator_name="v1",
                passed=False,
                errors=["Error"],
            ),
        ]

        engine.process_validator_results(state, results)

        assert len(state.validator_failures) == 1
        assert len(state.accumulated_feedback) == 1

    def test_create_retry_messages(self, sample_messages: list[Message]) -> None:
        """Test creating retry messages."""
        engine = RetryEngine()
        state = engine.create_state()
        state.attempt = 1

        last_response = "Here's the code..."

        messages = engine.create_retry_messages(
            sample_messages,
            state,
            last_response,
        )

        # Should have original messages + assistant response + user feedback
        assert len(messages) == len(sample_messages) + 2
        assert messages[-1].role == MessageRole.USER

    def test_build_feedback_message(self) -> None:
        """Test building feedback message."""
        engine = RetryEngine()
        state = engine.create_state()
        state.attempt = 1

        state.add_validator_result(
            ValidatorResult(
                validator_name="test_validator",
                passed=False,
                errors=["Test error"],
            ),
        )

        feedback = engine._build_feedback_message(state)

        assert "Attempt 2" in feedback
        assert "test_validator" in feedback
        assert "Test error" in feedback

    def test_build_final_attempt_message(self) -> None:
        """Test building final attempt message."""
        engine = RetryEngine(max_retries=1)
        state = engine.create_state()
        state.attempt = 1  # At max

        feedback = engine._build_feedback_message(state)

        assert "CRITICAL" in feedback
        assert "final attempt" in feedback.lower()

    def test_should_early_fail_disabled(self) -> None:
        """Test early fail when disabled."""
        engine = RetryEngine(early_fail=False)

        results = [
            ValidatorResult(
                validator_name="bandit_security",
                passed=False,
                errors=["Security issue"],
            ),
        ]

        assert engine.should_early_fail(results) is False

    def test_should_early_fail_security_issue(self) -> None:
        """Test early fail on security issue."""
        engine = RetryEngine(early_fail=True)

        results = [
            ValidatorResult(
                validator_name="bandit_security",
                passed=False,
                errors=["Security issue"],
            ),
        ]

        assert engine.should_early_fail(results) is True

    def test_should_early_fail_unsafe_code(self) -> None:
        """Test early fail on unsafe code."""
        engine = RetryEngine(early_fail=True)

        results = [
            ValidatorResult(
                validator_name="no_eval_exec",
                passed=False,
                errors=["eval() detected"],
            ),
        ]

        assert engine.should_early_fail(results) is True

    def test_should_early_fail_non_critical(self) -> None:
        """Test no early fail on non-critical issue."""
        engine = RetryEngine(early_fail=True)

        results = [
            ValidatorResult(
                validator_name="ruff_lint",
                passed=False,
                errors=["Line too long"],
            ),
        ]

        assert engine.should_early_fail(results) is False

    def test_get_accumulated_feedback(self) -> None:
        """Test getting accumulated feedback."""
        engine = RetryEngine()
        state = engine.create_state()

        state.add_feedback("Feedback 1")
        state.add_feedback("Feedback 2")

        feedback = engine.get_accumulated_feedback(state)

        assert "Feedback 1" in feedback
        assert "Feedback 2" in feedback

    def test_finalize(self) -> None:
        """Test finalizing retry state."""
        engine = RetryEngine(max_retries=2)
        state = engine.create_state()
        state.attempt = 2

        state.add_validator_result(
            ValidatorResult(
                validator_name="v1",
                passed=False,
                errors=["Error"],
            ),
        )

        summary = engine.finalize(state)

        assert summary["total_attempts"] == 3
        assert summary["max_retries"] == 2
        assert summary["validator_failures"] == 1
        assert summary["escalation_needed"] is True

    def test_finalize_no_escalation_needed(self) -> None:
        """Test finalizing when no escalation needed."""
        engine = RetryEngine()
        state = engine.create_state()

        summary = engine.finalize(state)

        assert summary["escalation_needed"] is False

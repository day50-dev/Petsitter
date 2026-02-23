"""Integration tests for PetSitter."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from petsitter.models import (
    AnthropicMessage,
    AnthropicRequest,
    HealthResponse,
    OpenAIMessage,
    OpenAIRequest,
)


class TestHealthEndpoint:
    """Tests for health check endpoint."""

    def test_health_check(self) -> None:
        """Test health check returns healthy status."""
        # Note: This test requires the app to be initialized
        # For now, we test the response model directly
        response = HealthResponse()
        assert response.status == "healthy"
        assert response.version == "0.1.0"


class TestValidatorIntegration:
    """Integration tests for validators."""

    def test_no_eval_validator_integration(self) -> None:
        """Test no_eval_exec validator end-to-end."""
        from petsitter.validators.no_eval_exec import NoEvalExecValidator

        validator = NoEvalExecValidator()

        # Safe code should pass
        safe_code = "def add(a, b): return a + b"
        result = validator.validate(safe_code, "")
        assert result.passed is True

        # Unsafe code should fail
        unsafe_code = "eval(user_input)"
        result = validator.validate(unsafe_code, "")
        assert result.passed is False

    def test_validator_feedback_format(self) -> None:
        """Test validator feedback is properly formatted."""
        from petsitter.validators.no_eval_exec import NoEvalExecValidator

        validator = NoEvalExecValidator()
        result = validator.validate("exec(code)", "")

        feedback = result.to_feedback_string()
        assert "failed" in feedback.lower()
        assert "no_eval_exec" in feedback


class TestSkillIntegration:
    """Integration tests for skills."""

    def test_skill_loading_and_stacking(self, test_skills_dir) -> None:
        """Test loading and stacking skills."""
        from petsitter.skills.loader import load_skills
        from petsitter.skills.stack import stack_skills

        skills = load_skills([str(test_skills_dir / "test_skill")])
        assert len(skills) == 1

        stacked = stack_skills(skills)
        assert len(stacked) == 1
        assert stacked.validators == ["no_eval_exec"]

    def test_skill_system_prompt_integration(self, sample_skill) -> None:
        """Test skill system prompt is properly merged."""
        from petsitter.skills.stack import create_system_message, stack_skills

        stacked = stack_skills([sample_skill])
        message = create_system_message(stacked)

        assert "test_skill" in message
        assert "Validation" in message


class TestRetryIntegration:
    """Integration tests for retry engine."""

    def test_retry_flow_with_validator_failures(self) -> None:
        """Test complete retry flow with validator failures."""
        from petsitter.models import ValidatorResult
        from petsitter.retry.engine import RetryEngine

        engine = RetryEngine(max_retries=3)
        state = engine.create_state()

        # Simulate first attempt failure
        result1 = ValidatorResult(
            validator_name="ruff_lint",
            passed=False,
            errors=["Line too long"],
        )
        all_passed, feedback = engine.process_validator_results(state, [result1])
        assert all_passed is False

        # Simulate retry
        engine.record_attempt(state)

        # Simulate second attempt success
        result2 = ValidatorResult(
            validator_name="ruff_lint",
            passed=True,
        )
        all_passed, feedback = engine.process_validator_results(state, [result2])
        assert all_passed is True

    def test_early_fail_integration(self) -> None:
        """Test early fail with critical validator."""
        from petsitter.models import ValidatorResult
        from petsitter.retry.engine import RetryEngine

        engine = RetryEngine(early_fail=True)

        # Security failure should trigger early fail
        security_result = ValidatorResult(
            validator_name="bandit_security",
            passed=False,
            errors=["High severity issue"],
        )

        assert engine.should_early_fail([security_result]) is True

        # Lint failure should not trigger early fail
        lint_result = ValidatorResult(
            validator_name="ruff_lint",
            passed=False,
            errors=["Missing import"],
        )

        assert engine.should_early_fail([lint_result]) is False


class TestModelIntegration:
    """Integration tests for data models."""

    def test_message_flow(self) -> None:
        """Test message creation and conversion flow."""
        from petsitter.models import Message, MessageRole

        # Create messages
        messages = [
            Message(role=MessageRole.SYSTEM, content="Be helpful"),
            Message(role=MessageRole.USER, content="Hello"),
        ]

        # Convert to string roles (as would happen in API)
        for msg in messages:
            assert msg.role.value in ["system", "user", "assistant"]

    def test_validator_result_chain(self) -> None:
        """Test chaining validator results."""
        from petsitter.models import RetryState, ValidatorResult

        state = RetryState()

        results = [
            ValidatorResult(validator_name="v1", passed=True),
            ValidatorResult(
                validator_name="v2",
                passed=False,
                errors=["Error"],
            ),
            ValidatorResult(validator_name="v3", passed=True),
        ]

        for result in results:
            state.add_validator_result(result)

        assert len(state.validator_failures) == 3
        assert sum(1 for r in state.validator_failures if r.passed) == 2


class TestBackendInterface:
    """Tests for backend interface."""

    def test_ollama_backend_creation(self) -> None:
        """Test Ollama backend creation."""
        from petsitter.backends.ollama import OllamaBackend

        backend = OllamaBackend(
            base_url="http://localhost:11434",
            model="test-model",
        )

        assert backend.name == "ollama"
        assert backend.model == "test-model"

    def test_backend_interface(self) -> None:
        """Test backend interface compliance."""
        from petsitter.backends.base import LLMBackend
        from petsitter.backends.ollama import OllamaBackend

        # OllamaBackend should implement LLMBackend
        backend = OllamaBackend()
        assert isinstance(backend, LLMBackend)


@pytest.mark.asyncio
class TestAsyncBackend:
    """Async tests for backend."""

    async def test_ollama_is_available_mock(self) -> None:
        """Test Ollama availability check (mocked)."""
        from petsitter.backends.ollama import OllamaBackend

        backend = OllamaBackend(base_url="http://nonexistent:11434")
        # Should return False for nonexistent server
        available = await backend.is_available()
        assert available is False

"""Retry engine for PetSitter."""

from __future__ import annotations

from typing import TYPE_CHECKING

from petsitter.models import Message, MessageRole, RetryState, ValidatorResult

if TYPE_CHECKING:
    from petsitter.logging.metrics import PetSitterLogger


class RetryEngine:
    """Handles retry logic with validator feedback."""

    def __init__(self, max_retries: int = 3, early_fail: bool = True):
        self.max_retries = max_retries
        self.early_fail = early_fail
        self._logger: PetSitterLogger | None = None

    @property
    def logger(self) -> "PetSitterLogger | None":
        """Get logger, initialized lazily."""
        if self._logger is None:
            try:
                from petsitter.logging.metrics import get_logger

                self._logger = get_logger()
            except RuntimeError:
                # Logger not initialized (e.g., in tests)
                return None
        return self._logger

    def create_state(self) -> RetryState:
        """Create a new retry state."""
        return RetryState(max_retries=self.max_retries)

    def should_retry(self, state: RetryState) -> bool:
        """Check if another retry should be attempted."""
        return state.can_retry

    def record_attempt(self, state: RetryState) -> None:
        """Record a retry attempt."""
        state.attempt += 1
        if self.logger:
            self.logger.log_retry(
                f"request_{state.started_at.timestamp()}",
                state.attempt,
                f"{len(state.validator_failures)} validator failures",
            )

    def process_validator_results(
        self,
        state: RetryState,
        results: list[ValidatorResult],
    ) -> tuple[bool, str]:
        """Process validator results and update state.

        Args:
            state: Current retry state
            results: List of validator results

        Returns:
            Tuple of (all_passed, feedback_message)
        """
        all_passed = True
        feedback_parts = []

        for result in results:
            state.add_validator_result(result)

            if not result.passed:
                all_passed = False
                feedback_parts.append(result.to_feedback_string())

        if all_passed:
            return True, ""

        feedback = "\n\n".join(feedback_parts)
        return False, feedback

    def create_retry_messages(
        self,
        original_messages: list[Message],
        state: RetryState,
        last_response: str,
    ) -> list[Message]:
        """Create messages for a retry attempt.

        Args:
            original_messages: Original conversation messages
            state: Current retry state
            last_response: The last (failed) response

        Returns:
            New message list for retry
        """
        # Start with original messages
        messages = original_messages.copy()

        # Add the failed response
        messages.append(Message(role=MessageRole.ASSISTANT, content=last_response))

        # Create feedback message
        feedback = self._build_feedback_message(state)
        messages.append(Message(role=MessageRole.USER, content=feedback))

        return messages

    def _build_feedback_message(self, state: RetryState) -> str:
        """Build a feedback message for retry.

        Args:
            state: Current retry state

        Returns:
            Feedback message string
        """
        parts = []

        # Header
        if state.attempt >= state.max_retries:
            parts.append(
                "**CRITICAL: This is your final attempt.** "
                "The response must pass all validators."
            )
        else:
            parts.append(f"**Attempt {state.attempt + 1} of {state.max_retries + 1}**")

        # List specific failures
        parts.append("\n## Validation Failures\n")

        for result in state.validator_failures:
            if not result.passed:
                parts.append(f"### {result.validator_name}")
                if result.errors:
                    parts.append("**Errors:**")
                    for error in result.errors:
                        parts.append(f"- {error}")
                if result.feedback:
                    parts.append(f"\n{result.feedback}")
                parts.append("")

        # General guidance
        parts.append(
            "\n## Instructions\n"
            "Please revise your response to address all the validation failures above. "
            "Be specific and ensure your code passes all checks."
        )

        return "\n".join(parts)

    def should_early_fail(
        self,
        results: list[ValidatorResult],
        critical_validators: list[str] | None = None,
    ) -> bool:
        """Check if we should fail early based on validator results.

        Args:
            results: Validator results
            critical_validators: List of validator names considered critical

        Returns:
            True if should fail early
        """
        if not self.early_fail:
            return False

        # Default critical validators
        if critical_validators is None:
            critical_validators = ["bandit_security", "no_eval_exec"]

        for result in results:
            if not result.passed and result.validator_name in critical_validators:
                # Security or unsafe code - fail early
                return True

        return False

    def get_accumulated_feedback(self, state: RetryState) -> str:
        """Get all accumulated feedback from failed attempts."""
        return state.feedback_text

    def finalize(self, state: RetryState) -> dict:
        """Finalize retry state and return summary.

        Args:
            state: Final retry state

        Returns:
            Summary dict with retry statistics
        """
        return {
            "total_attempts": state.attempt + 1,
            "max_retries": state.max_retries,
            "validator_failures": len(state.validator_failures),
            "escalation_needed": not state.can_retry and len(state.validator_failures) > 0,
            "started_at": state.started_at.isoformat(),
        }

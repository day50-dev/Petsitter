"""Ruff lint validator for Python code."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from petsitter.models import ValidatorResult
from petsitter.validators.base import BaseValidator, extract_python_code_blocks


class RuffLintValidator(BaseValidator):
    """Validator that runs ruff linter on Python code."""

    name = "ruff_lint"
    description = "Runs ruff linter to check Python code style and errors"

    def __init__(self, ruff_path: str = "ruff", select: list[str] | None = None):
        self.ruff_path = ruff_path
        self.select = select or ["E", "F", "W", "I"]  # Default: errors, pyflakes, warnings, imports

    def validate(self, code: str, content: str) -> ValidatorResult:
        """Validate Python code using ruff.

        Args:
            code: Python code to validate
            content: Full response content

        Returns:
            ValidatorResult with pass/fail status
        """
        if not code.strip():
            return ValidatorResult(
                validator_name=self.name,
                passed=True,
                errors=[],
                feedback="No code to validate",
                code_block=code,
            )

        errors = []

        try:
            # Write code to temp file
            with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
                f.write(code)
                temp_path = Path(f.name)

            try:
                # Run ruff
                cmd = [self.ruff_path, "check", "--output-format=json"]
                for rule in self.select:
                    cmd.extend(["--select", rule])
                cmd.append(str(temp_path))

                result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

                if result.stdout:
                    import json

                    try:
                        issues = json.loads(result.stdout)
                        for issue in issues:
                            line = issue.get("location", {}).get("row", "?")
                            col = issue.get("location", {}).get("column", "?")
                            code = issue.get("code", "UNKNOWN")
                            message = issue.get("message", "")
                            errors.append(f"Line {line}:{col} [{code}]: {message}")
                    except json.JSONDecodeError:
                        # Fallback to text parsing
                        for line in result.stdout.strip().split("\n"):
                            if line:
                                errors.append(line)

            finally:
                # Clean up temp file
                temp_path.unlink()

        except subprocess.TimeoutExpired:
            errors.append("Ruff linting timed out")
        except FileNotFoundError:
            # Ruff not installed, skip validation
            return ValidatorResult(
                validator_name=self.name,
                passed=True,
                errors=[],
                feedback="Ruff not installed, skipping lint validation",
                code_block=code,
            )
        except Exception as e:
            errors.append(f"Ruff error: {str(e)}")

        passed = len(errors) == 0

        feedback = ""
        if not passed:
            feedback = (
                "Ruff linting found issues. Run 'ruff check --fix' to auto-fix some issues, "
                "or manually address the errors above."
            )

        return ValidatorResult(
            validator_name=self.name,
            passed=passed,
            errors=errors,
            feedback=feedback,
            code_block=code,
        )

    def validate_content(self, content: str) -> list[ValidatorResult]:
        """Validate all Python code blocks in content.

        Args:
            content: Full response content

        Returns:
            List of ValidatorResults for each code block
        """
        code_blocks = extract_python_code_blocks(content)
        results = []

        for code in code_blocks:
            result = self.validate(code, content)
            results.append(result)

        return results

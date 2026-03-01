"""Mypy type checking validator for Python code."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from petsitter.models import ValidatorResult
from petsitter.validators.base import BaseValidator, extract_python_code_blocks


class MypyTypesValidator(BaseValidator):
    """Validator that runs mypy type checker on Python code."""

    name = "mypy_types"
    description = "Runs mypy to check Python type hints"

    def __init__(self, mypy_path: str = "mypy", strict: bool = False):
        self.mypy_path = mypy_path
        self.strict = strict

    def validate(self, code: str, content: str) -> ValidatorResult:
        """Validate Python code using mypy.

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
                # Run mypy
                cmd = [self.mypy_path, "--no-error-summary", "--show-column-numbers"]
                if self.strict:
                    cmd.append("--strict")
                cmd.append(str(temp_path))

                result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

                # mypy outputs errors to stdout
                if result.stdout:
                    for line in result.stdout.strip().split("\n"):
                        if line and "Success" not in line:
                            errors.append(line)

            finally:
                # Clean up temp file
                temp_path.unlink()

        except subprocess.TimeoutExpired:
            errors.append("Mypy type checking timed out")
        except FileNotFoundError:
            # Mypy not installed, skip validation
            return ValidatorResult(
                validator_name=self.name,
                passed=True,
                errors=[],
                feedback="Mypy not installed, skipping type validation",
                code_block=code,
            )
        except Exception as e:
            errors.append(f"Mypy error: {str(e)}")

        passed = len(errors) == 0

        feedback = ""
        if not passed:
            feedback = (
                "Mypy found type errors. Add proper type hints or use # type: ignore "
                "for intentional type issues."
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

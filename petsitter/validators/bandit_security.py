"""Bandit security validator for Python code."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from petsitter.models import ValidatorResult
from petsitter.validators.base import BaseValidator, extract_python_code_blocks


class BanditSecurityValidator(BaseValidator):
    """Validator that runs bandit security scanner on Python code."""

    name = "bandit_security"
    description = "Runs bandit to find common security issues in Python code"

    def __init__(self, bandit_path: str = "bandit", skip_tests: bool = True):
        self.bandit_path = bandit_path
        self.skip_tests = skip_tests

    def validate(self, code: str, content: str) -> ValidatorResult:
        """Validate Python code using bandit.

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
        warnings = []

        try:
            # Write code to temp file
            with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
                f.write(code)
                temp_path = Path(f.name)

            try:
                # Run bandit
                cmd = [
                    self.bandit_path,
                    "-f",
                    "json",
                    "-q",  # Quiet mode
                    str(temp_path),
                ]
                if self.skip_tests:
                    cmd.append("--skip=B101")  # Skip assert_used test

                result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

                if result.stdout:
                    import json

                    try:
                        data = json.loads(result.stdout)
                        issues = data.get("results", [])

                        for issue in issues:
                            line = issue.get("line_number", "?")
                            test_id = issue.get("test_id", "UNKNOWN")
                            severity = issue.get("issue_severity", "LOW")
                            message = issue.get("issue_text", "")

                            error_msg = f"Line {line} [{test_id}/{severity}]: {message}"

                            if severity in ("HIGH", "MEDIUM"):
                                errors.append(error_msg)
                            else:
                                warnings.append(error_msg)

                    except json.JSONDecodeError:
                        # Fallback to text parsing
                        for line in result.stdout.strip().split("\n"):
                            if line and not line.startswith("Run started"):
                                errors.append(line)

            finally:
                # Clean up temp file
                temp_path.unlink()

        except subprocess.TimeoutExpired:
            errors.append("Bandit security scan timed out")
        except FileNotFoundError:
            # Bandit not installed, skip validation
            return ValidatorResult(
                validator_name=self.name,
                passed=True,
                errors=[],
                feedback="Bandit not installed, skipping security validation",
                code_block=code,
            )
        except Exception as e:
            errors.append(f"Bandit error: {str(e)}")

        passed = len(errors) == 0

        feedback = ""
        if not passed:
            feedback = (
                "Bandit found security issues. Review and fix the high/medium severity "
                "issues before using this code."
            )
        elif warnings:
            feedback = f"Found {len(warnings)} low-severity warnings (passed with warnings)"

        all_issues = errors + warnings
        return ValidatorResult(
            validator_name=self.name,
            passed=passed,
            errors=all_issues,
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

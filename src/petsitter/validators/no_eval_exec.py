"""No eval/exec validator - detects unsafe Python patterns."""

from __future__ import annotations

import re

from petsitter.models import ValidatorResult
from petsitter.validators.base import BaseValidator, extract_python_code_blocks


class NoEvalExecValidator(BaseValidator):
    """Validator that detects unsafe eval/exec usage."""

    name = "no_eval_exec"
    description = "Detects unsafe eval(), exec(), and similar dangerous patterns"

    # Patterns to detect
    UNSAFE_PATTERNS = [
        (r"\beval\s*\(", "eval() is unsafe and can execute arbitrary code"),
        (r"\bexec\s*\(", "exec() is unsafe and can execute arbitrary code"),
        (r"\b__import__\s*\(", "__import__() can be used to import malicious modules"),
        (r"\bcompile\s*\(", "compile() can be used to create executable code"),
        (r"\bgetattr\s*\([^)]*,\s*['\"][^'\"]*['\"]\s*\)", "getattr() with dynamic attribute names can be unsafe"),
        (r"\bsubprocess\.[a-zA-Z_]+\s*\(", "subprocess calls should use shell=False"),
        (r"\bos\.system\s*\(", "os.system() is unsafe, use subprocess with shell=False"),
        (r"\bos\.popen\s*\(", "os.popen() is unsafe, use subprocess instead"),
    ]

    def validate(self, code: str, content: str) -> ValidatorResult:
        """Validate code for unsafe patterns.

        Args:
            code: Python code to validate
            content: Full response content

        Returns:
            ValidatorResult with pass/fail status
        """
        errors = []

        # Check for shell=True in subprocess
        if re.search(r"shell\s*=\s*True", code):
            errors.append("shell=True in subprocess is unsafe, use shell=False")

        for pattern, message in self.UNSAFE_PATTERNS:
            matches = re.finditer(pattern, code)
            for match in matches:
                # Get line number
                line_num = code[: match.start()].count("\n") + 1
                errors.append(f"Line {line_num}: {message}")

        # Also check content for code blocks
        code_blocks = extract_python_code_blocks(content)
        for block in code_blocks:
            if block != code:  # Avoid double-checking
                for pattern, message in self.UNSAFE_PATTERNS:
                    if re.search(pattern, block):
                        if message not in errors:
                            errors.append(f"In code block: {message}")

        passed = len(errors) == 0

        feedback = ""
        if not passed:
            feedback = (
                "Found unsafe code patterns. Please refactor to use safer alternatives:\n"
                "- Use ast.literal_eval() instead of eval() for parsing literals\n"
                "- Avoid exec() and __import__() with user input\n"
                "- Use subprocess with shell=False instead of os.system()"
            )

        return ValidatorResult(
            validator_name=self.name,
            passed=passed,
            errors=errors,
            feedback=feedback,
            code_block=code,
        )

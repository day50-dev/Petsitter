"""Tests for PetSitter validators."""

from __future__ import annotations

import pytest

from petsitter.validators.base import (
    BaseValidator,
    extract_all_code_blocks,
    extract_python_code_blocks,
)
from petsitter.validators.no_eval_exec import NoEvalExecValidator
from petsitter.validators.registry import ValidatorRegistry, get_registry, run_validators


class TestExtractCodeBlocks:
    """Tests for code block extraction."""

    def test_extract_python_block(self) -> None:
        """Test extracting Python code block."""
        content = """
Here's the code:

```python
def hello():
    print("Hello")
```
"""
        blocks = extract_python_code_blocks(content)
        assert len(blocks) == 1
        assert "def hello():" in blocks[0]

    def test_extract_py_block(self) -> None:
        """Test extracting py code block."""
        content = """
```py
x = 1
```
"""
        blocks = extract_python_code_blocks(content)
        assert len(blocks) == 1
        assert "x = 1" in blocks[0]

    def test_extract_multiple_blocks(self) -> None:
        """Test extracting multiple code blocks."""
        content = """
First:
```python
def one():
    pass
```

Second:
```python
def two():
    pass
```
"""
        blocks = extract_python_code_blocks(content)
        assert len(blocks) == 2

    def test_extract_no_blocks(self) -> None:
        """Test extracting when no code blocks exist."""
        content = "Just some text without code."
        blocks = extract_python_code_blocks(content)
        assert len(blocks) == 0

    def test_extract_all_blocks_with_language(self) -> None:
        """Test extracting all code blocks with language info."""
        content = """
```python
def py():
    pass
```

```javascript
function js() {}
```
"""
        blocks = extract_all_code_blocks(content)
        assert len(blocks) == 2
        assert blocks[0][0] == "python"
        assert blocks[1][0] == "javascript"


class TestNoEvalExecValidator:
    """Tests for NoEvalExecValidator."""

    def test_safe_code_passes(self, sample_python_code: str) -> None:
        """Test that safe code passes validation."""
        validator = NoEvalExecValidator()
        result = validator.validate(sample_python_code, "")

        assert result.passed is True
        assert result.validator_name == "no_eval_exec"

    def test_eval_detected(self, unsafe_python_code: str) -> None:
        """Test that eval() is detected."""
        validator = NoEvalExecValidator()
        result = validator.validate(unsafe_python_code, "")

        assert result.passed is False
        assert any("eval" in err.lower() for err in result.errors)

    def test_exec_detected(self) -> None:
        """Test that exec() is detected."""
        code = """
def run(code):
    exec(code)
"""
        validator = NoEvalExecValidator()
        result = validator.validate(code, "")

        assert result.passed is False
        assert any("exec" in err.lower() for err in result.errors)

    def test_shell_true_detected(self) -> None:
        """Test that shell=True is detected."""
        code = """
import subprocess
subprocess.run("ls", shell=True)
"""
        validator = NoEvalExecValidator()
        result = validator.validate(code, "")

        assert result.passed is False
        assert any("shell=True" in err for err in result.errors)

    def test_os_system_detected(self) -> None:
        """Test that os.system() is detected."""
        code = """
import os
os.system("ls")
"""
        validator = NoEvalExecValidator()
        result = validator.validate(code, "")

        assert result.passed is False
        assert any("os.system" in err for err in result.errors)

    def test_empty_code_passes(self) -> None:
        """Test that empty code passes."""
        validator = NoEvalExecValidator()
        result = validator.validate("", "")

        assert result.passed is True

    def test_feedback_for_failure(self, unsafe_python_code: str) -> None:
        """Test that feedback is provided for failed validation."""
        validator = NoEvalExecValidator()
        result = validator.validate(unsafe_python_code, "")

        assert result.passed is False
        assert result.feedback != ""
        assert "safer alternatives" in result.feedback.lower()


class TestValidatorRegistry:
    """Tests for ValidatorRegistry."""

    def test_create_registry(self) -> None:
        """Test creating a registry."""
        registry = ValidatorRegistry()
        assert registry is not None

    def test_register_validator(self) -> None:
        """Test registering a validator."""
        registry = ValidatorRegistry()
        validator = NoEvalExecValidator()
        registry.register(validator)

        assert registry.get("no_eval_exec") is not None

    def test_get_nonexistent_validator(self) -> None:
        """Test getting nonexistent validator."""
        registry = ValidatorRegistry()
        result = registry.get("nonexistent")
        assert result is None

    def test_list_validators(self) -> None:
        """Test listing validators."""
        registry = ValidatorRegistry()
        registry.register(NoEvalExecValidator())

        validators = registry.list_validators()
        assert "no_eval_exec" in validators

    def test_run_validator(self) -> None:
        """Test running a validator."""
        registry = ValidatorRegistry()
        registry.register(NoEvalExecValidator())

        result = registry.run("no_eval_exec", "x = 1", "")
        assert result is not None
        assert result.passed is True

    def test_run_nonexistent_validator(self) -> None:
        """Test running nonexistent validator."""
        registry = ValidatorRegistry()
        result = registry.run("nonexistent", "code", "")
        assert result is None

    def test_run_all_validators(self) -> None:
        """Test running multiple validators."""
        registry = ValidatorRegistry()
        registry.register(NoEvalExecValidator())

        results = registry.run_all(["no_eval_exec"], "x = 1", "")
        assert len(results) == 1

    def test_run_all_with_missing_validators(self) -> None:
        """Test running validators when some don't exist."""
        registry = ValidatorRegistry()
        registry.register(NoEvalExecValidator())

        results = registry.run_all(
            ["no_eval_exec", "nonexistent"],
            "x = 1",
            "",
        )
        # Only existing validators should run
        assert len(results) == 1


class TestGlobalRegistry:
    """Tests for global registry functions."""

    def test_get_registry(self) -> None:
        """Test getting global registry."""
        registry = get_registry()
        assert registry is not None

    def test_register_validator_global(self) -> None:
        """Test registering validator in global registry."""
        from petsitter.validators import register_validator

        validator = NoEvalExecValidator()
        register_validator(validator)

        registry = get_registry()
        assert registry.get("no_eval_exec") is not None

    def test_run_validators_global(self) -> None:
        """Test running validators from global registry."""
        results = run_validators(["no_eval_exec"], "x = 1", "")
        # May be empty if validator not registered yet
        assert isinstance(results, list)

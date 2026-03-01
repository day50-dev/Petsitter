"""Validator registry for PetSitter."""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path
from typing import Callable

from petsitter.validators.base import BaseValidator, ValidatorFunction


class ValidatorRegistry:
    """Registry for validators."""

    def __init__(self):
        self._validators: dict[str, BaseValidator | ValidatorFunction] = {}
        self._register_builtin_validators()

    def _register_builtin_validators(self) -> None:
        """Register built-in validators."""
        # Import and register built-in validators
        try:
            from petsitter.validators.no_eval_exec import NoEvalExecValidator

            self.register(NoEvalExecValidator())
        except ImportError:
            pass

        try:
            from petsitter.validators.ruff_lint import RuffLintValidator

            self.register(RuffLintValidator())
        except ImportError:
            pass

        try:
            from petsitter.validators.mypy_types import MypyTypesValidator

            self.register(MypyTypesValidator())
        except ImportError:
            pass

        try:
            from petsitter.validators.bandit_security import BanditSecurityValidator

            self.register(BanditSecurityValidator())
        except ImportError:
            pass

    def register(self, validator: BaseValidator | ValidatorFunction) -> None:
        """Register a validator.

        Args:
            validator: Validator instance or function
        """
        if isinstance(validator, BaseValidator):
            name = validator.name
        else:
            # Function-based validator
            name = getattr(validator, "__name__", "unknown")

        self._validators[name] = validator

    def get(self, name: str) -> BaseValidator | ValidatorFunction | None:
        """Get a validator by name."""
        return self._validators.get(name)

    def list_validators(self) -> list[str]:
        """List all registered validator names."""
        return list(self._validators.keys())

    def run(self, name: str, code: str, content: str) -> ValidatorResult | None:
        """Run a validator by name.

        Args:
            name: Validator name
            code: Code to validate
            content: Full content

        Returns:
            ValidatorResult or None if validator not found
        """
        validator = self.get(name)
        if validator is None:
            return None

        if isinstance(validator, BaseValidator):
            return validator.validate(code, content)
        else:
            return validator(code, content)

    def run_all(
        self,
        validator_names: list[str],
        code: str,
        content: str,
    ) -> list[ValidatorResult]:
        """Run multiple validators.

        Args:
            validator_names: List of validator names to run
            code: Code to validate
            content: Full content

        Returns:
            List of ValidatorResults
        """
        results = []
        for name in validator_names:
            result = self.run(name, code, content)
            if result is not None:
                results.append(result)
        return results

    def discover_from_directory(self, directory: Path) -> None:
        """Discover and register validators from a directory.

        Args:
            directory: Directory containing validator modules
        """
        if not directory.exists():
            return

        for validator_file in directory.glob("*.py"):
            if validator_file.name.startswith("_"):
                continue

            module_name = validator_file.stem
            spec = importlib.util.spec_from_file_location(module_name, validator_file)
            if spec is None or spec.loader is None:
                continue

            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            # Look for validator classes/functions
            for name, obj in inspect.getmembers(module):
                if inspect.isclass(obj) and issubclass(obj, BaseValidator) and obj != BaseValidator:
                    try:
                        instance = obj()
                        self.register(instance)
                    except Exception:
                        pass
                elif inspect.isfunction(obj) and hasattr(obj, "__name__"):
                    # Check if it looks like a validator function
                    sig = inspect.signature(obj)
                    if len(sig.parameters) >= 2:
                        self.register(obj)


# Global registry instance
_registry: ValidatorRegistry | None = None


def get_registry() -> ValidatorRegistry:
    """Get the global validator registry."""
    global _registry
    if _registry is None:
        _registry = ValidatorRegistry()
    return _registry


def register_validator(validator: BaseValidator | ValidatorFunction) -> None:
    """Register a validator in the global registry."""
    get_registry().register(validator)


def run_validators(
    validator_names: list[str],
    code: str,
    content: str,
) -> list[ValidatorResult]:
    """Run validators from the global registry."""
    return get_registry().run_all(validator_names, code, content)

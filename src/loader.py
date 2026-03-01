"""Dynamic loading of trick modules."""

import importlib.util
import sys
from pathlib import Path
from typing import Type

from src.trick import Trick


def load_trick_from_path(path: str) -> Type[Trick]:
    """Load a Trick class from a Python file path.

    Args:
        path: File path to the trick module (e.g., 'tricks/tools.py').

    Returns:
        The Trick subclass defined in the module.

    Raises:
        FileNotFoundError: If the path doesn't exist.
        ImportError: If no Trick subclass is found.
    """
    trick_path = Path(path).resolve()
    if not trick_path.exists():
        raise FileNotFoundError(f"Trick file not found: {path}")

    module_name = trick_path.stem
    spec = importlib.util.spec_from_file_location(module_name, trick_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load module: {path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    # Find the Trick subclass (not the base Trick itself)
    for attr_name in dir(module):
        attr = getattr(module, attr_name)
        if (
            isinstance(attr, type)
            and issubclass(attr, Trick)
            and attr is not Trick
        ):
            return attr

    raise ImportError(f"No Trick subclass found in {path}")


def load_tricks(paths: list[str]) -> list[Trick]:
    """Load multiple tricks from file paths.

    Args:
        paths: List of file paths to trick modules.

    Returns:
        List of instantiated Trick objects.
    """
    tricks = []
    for path in paths:
        trick_class = load_trick_from_path(path)
        tricks.append(trick_class())
    return tricks

"""Context manipulation utilities for petsitter."""

from typing import Any


def get_system_prompt(context: list) -> str:
    """Extract the system prompt from context if present.

    Args:
        context: List of message dicts.

    Returns:
        System prompt content or empty string.
    """
    if context and context[0].get("role") == "system":
        return context[0].get("content", "")
    return ""


def set_system_prompt(context: list, content: str) -> list:
    """Set or update the system prompt in context.

    Args:
        context: List of message dicts.
        content: New system prompt content.

    Returns:
        Modified context (may be new list).
    """
    if not context:
        return [{"role": "system", "content": content}]

    if context[0].get("role") == "system":
        context[0]["content"] = content
    else:
        context.insert(0, {"role": "system", "content": content})

    return context


def append_to_system_prompt(context: list, addition: str) -> list:
    """Append text to the system prompt.

    Args:
        context: List of message dicts.
        addition: Text to append.

    Returns:
        Modified context.
    """
    current = get_system_prompt(context)
    if current:
        new_content = current + "\n" + addition
    else:
        new_content = addition
    return set_system_prompt(context, new_content)


def get_last_message(context: list) -> dict | None:
    """Get the last message in context.

    Args:
        context: List of message dicts.

    Returns:
        Last message dict or None.
    """
    return context[-1] if context else None


def set_last_message_content(context: list, content: str) -> list:
    """Replace the content of the last message.

    Args:
        context: List of message dicts.
        content: New content.

    Returns:
        Modified context.
    """
    if context:
        context[-1]["content"] = content
    return context


def add_message(context: list, role: str, content: str) -> list:
    """Add a new message to the context.

    Args:
        context: List of message dicts.
        role: Message role (user, assistant, system, tool).
        content: Message content.

    Returns:
        Modified context.
    """
    context.append({"role": role, "content": content})
    return context

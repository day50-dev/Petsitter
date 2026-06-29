"""Base Trick class and callmodel utility for petsitter."""

import json
from typing import Any

import httpx


_model_url = ""
_model_name = ""
_api_key = ""


def configure(model_url: str, model_name: str = "", api_key: str = ""):
    """Configure global model settings for sync calls."""
    global _model_url, _model_name, _api_key
    _model_url = model_url
    _model_name = model_name
    _api_key = api_key


def callmodel_sync(context: list, user_message: str = "") -> list:
    """Synchronously call the model and get a response.
    
    Simple helper for tricks that need to loop back to the model.
    Appends the user_message and calls the model, returning updated context.
    """
    if not _model_url:
        raise ValueError("Model URL not configured")
    
    messages = context.copy()
    if user_message:
        messages.append({"role": "user", "content": user_message})
    
    payload = {
        "model": _model_name or "default",
        "messages": messages,
    }
    
    headers = {"Content-Type": "application/json"}
    if _api_key:
        headers["Authorization"] = f"Bearer {_api_key}"
    
    with httpx.Client() as client:
        response = client.post(
            f"{_model_url}/v1/chat/completions",
            json=payload,
            headers=headers,
            timeout=60.0,
        )
        response.raise_for_status()
        result = response.json()
    
    assistant_message = result["choices"][0]["message"]
    return messages + [assistant_message]


class Trick:
    """Base class for all petsitter tricks.

    Subclass this and implement any of the hooks to add functionality.

    Set `keywords` to a list of strings to make this trick keyword-activated.
    When the user includes a keyword in their message, the trick is invoked
    and the keyword is stripped before sending to the model.
    Tricks with no keywords are always active (when their trickset matches).
    """

    keywords: list[str] = []

    def system_prompt(self, to_add: str) -> str:
        """Add instructions to the system prompt.

        Args:
            to_add: The current system prompt content.

        Returns:
            Modified system prompt content.
        """
        return ""

    def pre_hook(self, context: list, params: dict) -> list:
        """Modify context before it reaches the model.

        Args:
            context: The conversation context (list of messages).
            params: Request parameters (tools, model, etc.).

        Returns:
            Modified context.
        """
        return context

    def post_hook(self, context: list) -> list:
        """Modify context after model processes but before returning upstream.

        Args:
            context: The conversation context including model response.

        Returns:
            Modified context.
        """
        return context

    def info(self, capabilities: dict) -> dict:
        """Declare capabilities added by this trick.

        Args:
            capabilities: Current capabilities dict.

        Returns:
            Modified capabilities dict.
        """
        return capabilities


async def callmodel(
    context: list,
    instruction: str = "",
    model_url: str = "",
    model_name: str = "",
    api_key: str = "",
) -> list:
    """Make a follow-up call to the model.

    Used by tricks that need to retry or refine model output.

    Args:
        context: Current conversation context.
        instruction: Optional system instruction to append.
        model_url: Base URL of the model endpoint.
        model_name: Name of the model to use.
        api_key: API key if required.

    Returns:
        Updated context with model response.
    """
    if not model_url:
        raise ValueError("model_url is required for callmodel")

    messages = context.copy()
    if instruction:
        # Add instruction as system message or append to existing
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] += f"\n{instruction}"
        else:
            messages.insert(0, {"role": "system", "content": instruction})

    payload = {
        "model": model_name or "default",
        "messages": messages,
    }

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{model_url}/v1/chat/completions",
            json=payload,
            headers=headers,
            timeout=60.0,
        )
        response.raise_for_status()
        result = response.json()

    # Extract the assistant's response
    assistant_message = result["choices"][0]["message"]
    return context + [assistant_message]

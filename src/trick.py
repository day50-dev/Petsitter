"""Base Trick class and callmodel utility for petsitter."""

import json
import urllib.parse
from typing import Any

import httpx


_model_url = ""
_model_name = ""
_api_key = ""
_modelset: dict[str, dict[str, str]] = {}


def configure(model_url: str, model_name: str = "", api_key: str = ""):
    """Configure global model settings for sync calls."""
    global _model_url, _model_name, _api_key
    _model_url = model_url
    _model_name = model_name
    _api_key = api_key


def parse_mas_uri(mas_uri: str) -> tuple[str, str]:
    """Parse a MAS URI into (base_url, model_name).

    MAS format: https://host[:port][/path]#m=model-name
    Follows MAS.md spec: extracts fragment, finds 'm' param, percent-decodes.
    """
    if "#" not in mas_uri:
        return (mas_uri.rstrip("/"), "")
    base_url, fragment = mas_uri.rsplit("#", 1)
    params = {}
    for param in fragment.split("&"):
        if "=" in param:
            key, _, value = param.partition("=")
            params[key] = urllib.parse.unquote(value)
    model_name = params.get("m", "")
    return (base_url.rstrip("/"), model_name)


def configure_modelset(modelset_raw: dict[str, str]) -> dict[str, dict[str, str]]:
    """Parse and store a modelset dict (key → MAS URI).

    Each value is a MAS URI like "http://host#m=model-name".
    Returns the parsed dict for inspection.
    """
    global _modelset
    parsed: dict[str, dict[str, str]] = {}
    for key, uri in modelset_raw.items():
        url, name = parse_mas_uri(uri)
        parsed[key] = {"model_url": url, "model_name": name}
    _modelset = parsed
    return parsed


def get_model_config(key: str = "default") -> dict[str, str]:
    """Get model config (model_url, model_name) for a modelset key.

    Falls back to the globally configured defaults if key is "default"
    and no modelset entry exists.
    """
    if key in _modelset:
        return dict(_modelset[key])
    if key == "default":
        return {"model_url": _model_url, "model_name": _model_name}
    raise KeyError(
        f"Model key {key!r} not found in modelset. "
        f"Available keys: {list(_modelset.keys()) or '(none)'}"
    )


def callmodel_sync(
    context: list,
    user_message: str = "",
    model_url: str = "",
    model_name: str = "",
    api_key: str = "",
) -> list:
    """Synchronously call the model and get a response.
    
    Simple helper for tricks that need to loop back to the model.
    Appends the user_message and calls the model, returning updated context.
    Can target a different model by passing model_url/model_name.
    """
    if not model_url:
        model_url = _model_url
    if not model_name:
        model_name = _model_name
    if not api_key:
        api_key = _api_key

    if not model_url:
        raise ValueError("Model URL not configured")
    
    messages = context.copy()
    if user_message:
        messages.append({"role": "user", "content": user_message})
    
    payload = {
        "model": model_name or "default",
        "messages": messages,
    }
    
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    
    with httpx.Client() as client:
        response = client.post(
            f"{model_url}/v1/chat/completions",
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

    Set `required_models` to declare what model keys the trick needs from
    a modelset. Default is ["default"] — the single model configured via
    --model_url/--model_name. Multi-model tricks (e.g. KennelTrick) should
    override with additional keys like ["default", "thinker", "toolcall"].
    """

    keywords: list[str] = []
    required_models: list[str] = ["default"]

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


def build_modelset_example(required_keys: list[str]) -> str:
    """Build a helpful example JSON string for a set of required model keys."""
    lines = ["{"]
    for i, key in enumerate(required_keys):
        comma = "," if i < len(required_keys) - 1 else ""
        lines.append(f'    "{key}": "http://localhost:11434#m=your-model-here"{comma}')
    lines.append("}")
    return "\n".join(lines)


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

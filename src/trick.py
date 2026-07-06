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


def update_model_config(key: str, model_url: str, model_name: str = "") -> dict[str, dict[str, str]]:
    """Add or update a single model config entry by key."""
    global _modelset
    _modelset[key] = {"model_url": model_url, "model_name": model_name}
    return _modelset


def remove_model_config(key: str) -> bool:
    """Remove a model config entry by key. Returns True if removed."""
    global _modelset
    return _modelset.pop(key, None) is not None


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

    Subclasses should set:
        __brief__: Short one-line description shown in the dashboard.
        __display_name__: Human-readable name (defaults to class name).

    Lifecycle hooks (called automatically by the framework):
        install()    — when the trick is first added to a trickset
        startup()    — when the first concurrent request uses this trick (0→1)
        shutdown()   — when the last concurrent request finishes (1→0)
        uninstall()  — when the trick is removed from a trickset
    """

    keywords: list[str] = []
    prompt_keyword: str = ""
    required_models: list[str] = ["default"]
    __brief__: str = ""
    __display_name__: str = ""

    def install(self) -> None:
        """Called when the trick is first added to a trickset.
        Use for one-time setup: clone repos, download files, create resources.
        """

    def startup(self) -> None:
        """Called when the first concurrent request starts using this trick
        (run counter goes 0→1). Use for per-session initialization like
        opening connections or preloading models.
        """

    def shutdown(self) -> None:
        """Called when the last concurrent request finishes using this trick
        (run counter goes 1→0). Use for per-session cleanup like closing
        connections. Also called during server shutdown for all active tricks.
        """

    def uninstall(self) -> None:
        """Called when the trick is removed from a trickset.
        Undo anything done during install().
        """

    def handle_prompt_keyword(self, request: str) -> dict | None:
        """Handle a prompt keyword detected in the user message.

        When the framework finds `(<prompt_keyword>: <request>)` in a user
        message, it strips the pattern from the message and calls this method
        with the extracted request text.

        Args:
            request: The text after the keyword and colon, e.g. "add a thinking mode".

        Returns:
            An assistant message dict like ``{"role": "assistant", "content": "..."}``
            to inject as the model response, or ``None`` to let the normal pipeline
            continue after stripping.
        """
        return None

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

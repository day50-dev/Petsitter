"""Known upstream providers, and live model discovery against them.

The catalog below is *endpoints only* - base URL, how to authenticate, and
where to get a key. It deliberately carries no model names. Providers ship and
retire models on their own schedule, so a hardcoded list here would be wrong
within weeks. :func:`discover_models` asks the provider what it is actually
serving right now and the dashboard renders that.

Two auth styles cover everything in the catalog:

``bearer``
    ``Authorization: Bearer <key>``, the OpenAI-compatible default.
``x-api-key``
    ``x-api-key: <key>`` plus ``anthropic-version``, for Anthropic's native
    API.
``query``
    ``?key=<key>``, for Google's native API.
``none``
    No credentials, for servers running on your own machine.
"""

from typing import Any

import httpx

from petsitter.trick import api_root_candidates, learn_api_root

# petsitter appends "/v1/chat/completions" to whatever URL it is given, so a
# provider is only directly routable when its OpenAI-compatible surface sits at
# exactly "<base_url>/v1/chat/completions". Anthropic has no such surface, and
# Google's sits at "/v1beta/openai/chat/completions", so both are listed for
# their live model lists but flagged `routable: False`. The dashboard says so
# rather than quietly writing a URL that will 404 on the first request.
PROVIDERS: list[dict[str, Any]] = [
    # ---- pay-per-token ----
    {
        "id": "openai",
        "label": "OpenAI",
        "group": "pay",
        "base_url": "https://api.openai.com/v1",
        "auth": "bearer",
        "routable": True,
    },
    {
        "id": "anthropic",
        "label": "Anthropic",
        "group": "pay",
        "base_url": "https://api.anthropic.com",
        "auth": "x-api-key",
        "discover_url": "https://api.anthropic.com/v1/models",
        "routable": False,
    },
    {
        "id": "gemini",
        "label": "Google Gemini",
        "group": "pay",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "auth": "query",
        "discover_url": "https://generativelanguage.googleapis.com/v1beta/models",
        "routable": False,
    },
    {
        "id": "mistral",
        "label": "Mistral",
        "group": "pay",
        "base_url": "https://api.mistral.ai/v1",
        "auth": "bearer",
        "routable": True,
    },
    {
        "id": "xai",
        "label": "xAI (Grok)",
        "group": "pay",
        "base_url": "https://api.x.ai/v1",
        "auth": "bearer",
        "routable": True,
    },
    {
        "id": "deepseek",
        "label": "DeepSeek",
        "group": "pay",
        "base_url": "https://api.deepseek.com/v1",
        "auth": "bearer",
        "routable": True,
    },
    {
        "id": "groq",
        "label": "Groq",
        "group": "pay",
        "base_url": "https://api.groq.com/openai/v1",
        "auth": "bearer",
        "routable": True,
    },
    {
        "id": "cerebras",
        "label": "Cerebras",
        "group": "pay",
        "base_url": "https://api.cerebras.ai/v1",
        "auth": "bearer",
        "routable": True,
    },
    {
        "id": "sambanova",
        "label": "SambaNova",
        "group": "pay",
        "base_url": "https://api.sambanova.ai/v1",
        "auth": "bearer",
        "routable": True,
    },
    {
        "id": "nebius",
        "label": "Nebius AI Studio",
        "group": "pay",
        "base_url": "https://api.studio.nebius.ai/v1",
        "auth": "bearer",
        "routable": True,
    },
    # ---- gateways ----
    {
        "id": "openrouter",
        "label": "OpenRouter",
        "group": "gateway",
        "base_url": "https://openrouter.ai/api/v1",
        "auth": "bearer",
        "routable": True,
    },
    {
        "id": "together",
        "label": "Together AI",
        "group": "gateway",
        "base_url": "https://api.together.xyz/v1",
        "auth": "bearer",
        "routable": True,
    },
    {
        "id": "fireworks",
        "label": "Fireworks AI",
        "group": "gateway",
        "base_url": "https://api.fireworks.ai/inference/v1",
        "auth": "bearer",
        "routable": True,
    },
    {
        "id": "llamacloud",
        "label": "LlamaCloud",
        "group": "gateway",
        "base_url": "https://api.llama.com/compat/v1",
        "auth": "bearer",
        "routable": True,
    },
    {
        "id": "github",
        "label": "GitHub Models",
        "group": "gateway",
        "base_url": "https://models.github.ai/inference",
        "auth": "bearer",
        "routable": False,
    },
    # ---- local ----
    {
        "id": "ollama",
        "label": "Ollama",
        "group": "local",
        "base_url": "http://localhost:11434/v1",
        "auth": "none",
        "routable": True,
    },
    {
        "id": "lmstudio",
        "label": "LM Studio",
        "group": "local",
        "base_url": "http://127.0.0.1:1234/v1",
        "auth": "none",
        "routable": True,
    },
    {
        "id": "llamacpp",
        "label": "llama.cpp",
        "group": "local",
        "base_url": "http://127.0.0.1:8080/v1",
        "auth": "none",
        "routable": True,
    },
    {
        "id": "vllm",
        "label": "vLLM",
        "group": "local",
        "base_url": "http://127.0.0.1:8000/v1",
        "auth": "none",
        "routable": True,
    },
]

GROUPS = {
    "pay": "Pay per token",
    "gateway": "Gateways",
    "local": "Local & self-hosted",
}

# Served to the browser: everything the picker renders, and nothing that is a
# secret. The API key is never part of the catalog.
CATALOG_FIELDS = (
    "id", "label", "group", "base_url", "auth",
    "discover_url", "routable",
)


def provider_catalog() -> list[dict[str, Any]]:
    """Return the provider list as plain dicts, ready to JSON-encode."""
    return [{f: p.get(f, "") for f in CATALOG_FIELDS} for p in PROVIDERS]


def find_provider(provider_id: str) -> dict[str, Any] | None:
    """Look up one provider by id, or None if it isn't in the catalog."""
    for p in PROVIDERS:
        if p["id"] == provider_id:
            return p
    return None


def provider_for_url(url: str) -> dict[str, Any] | None:
    """Which catalog entry a configured base URL belongs to, if any.

    Lets the picker show the provider someone already chose instead of making
    them pick it again on every reload. Matching is on a normalized URL because
    a config written by hand often has a trailing slash or no /v1 suffix.
    """
    target = _normalize_base(url)
    if not target:
        return None
    for p in PROVIDERS:
        if _normalize_base(p["base_url"]) == target:
            return p
    return None


def _normalize_base(url: str) -> str:
    u = (url or "").strip().rstrip("/")
    if not u:
        return ""
    # petsitter appends /v1/chat/completions, so a base without /v1 is the same
    # endpoint to anyone setting this up by hand.
    if u.endswith("/chat/completions"):
        u = u[: -len("/chat/completions")]
    if not u.endswith("/v1"):
        u += "/v1"
    return u


def discover_url_for(url: str, provider: dict[str, Any] | None = None) -> str:
    """Work out which URL lists models for a given base URL.

    Defaults to ``<base_url>/models``, which is right for everything
    OpenAI-compatible. Providers that need their own path carry
    ``discover_url``.
    """
    if provider and provider.get("discover_url"):
        return str(provider["discover_url"])
    return f"{url.rstrip('/')}/models"


def _build_auth(auth: str, api_key: str) -> tuple[dict[str, str], dict[str, str]]:
    """Return (headers, query params) for an auth style."""
    headers: dict[str, str] = {}
    params: dict[str, str] = {}
    if not api_key or auth == "none":
        return headers, params
    if auth == "x-api-key":
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
    elif auth == "query":
        params["key"] = api_key
    else:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers, params


def _normalize(payload: Any) -> list[dict[str, str]]:
    """Pull model ids out of whatever shape the provider replied with.

    Handles the three shapes in the wild: OpenAI's ``{"data": [{"id": ...}]}``,
    Google's ``{"models": [{"name": "models/..."}]}``, and a bare list. A
    gateway that invents its own envelope still gets a fair shot rather than an
    error.
    """
    if isinstance(payload, list):
        entries: Any = payload
    elif isinstance(payload, dict):
        entries = payload.get("data")
        if not isinstance(entries, list):
            entries = payload.get("models")
        if not isinstance(entries, list):
            entries = []
    else:
        entries = []

    models: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in entries:
        if isinstance(entry, str):
            mid, label = entry, entry
        elif isinstance(entry, dict):
            raw = entry.get("id") or entry.get("name") or entry.get("model") or ""
            if not raw:
                continue
            # Google prefixes ids with "models/", and the proxy needs the bare
            # name to send upstream.
            mid = str(raw)
            if mid.startswith("models/"):
                mid = mid[len("models/"):]
            label = str(entry.get("display_name") or entry.get("displayName") or mid)
        else:
            continue
        mid = mid.strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        models.append({"id": mid, "label": label.strip() or mid})

    models.sort(key=lambda m: m["id"])
    return models


async def discover_models(
    base_url: str,
    api_key: str = "",
    auth: str = "",
    provider: dict[str, Any] | None = None,
    timeout: float = 20.0,
) -> list[dict[str, str]]:
    """Ask a provider which models it is serving right now.

    ``base_url`` is the URL that would be saved as the model config's ``url``.
    ``auth`` may be left empty, in which case the catalog entry for ``provider``
    decides - that is the normal path, since the whole point of the catalog is
    that the user does not have to know that Anthropic authenticates with a
    header and Google with a query parameter.

    Returns a list of ``{"id", "label"}`` dicts, sorted by id.

    Raises ``ValueError`` with something worth showing a human: a rejected key
    and an unreachable host are very different problems, and the dashboard
    needs to say which one happened.
    """
    base_url = (base_url or "").strip().rstrip("/")
    if not base_url:
        raise ValueError("No base URL to query.")
    if not base_url.startswith(("http://", "https://")):
        raise ValueError("Base URL has to start with http:// or https://")

    if not auth:
        auth = str((provider or {}).get("auth") or "bearer")
    headers, params = _build_auth(auth, api_key or "")
    if provider and provider.get("discover_url"):
        roots = [""]
        tries = [str(provider["discover_url"])]
    else:
        # With or without /v1, whichever answers (see api_root_candidates).
        roots = api_root_candidates(base_url)
        tries = [f"{r}/models" for r in roots]

    for root, target in zip(roots, tries):
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(target, headers=headers, params=params, timeout=timeout)
        except httpx.TransportError as e:
            raise ValueError(f"Can't reach {target} - is it running, and is the URL right?") from e
        if response.status_code != 404:
            if root:
                learn_api_root(base_url, root)
            break

    status = response.status_code
    if status in (401, 403):
        raise ValueError("That API key was rejected. Check it, and that it has access to models.")
    if status == 404:
        raise ValueError(f"No model list at {' or '.join(tries)}. Check the base URL.")
    if status >= 400:
        raise ValueError(f"The provider answered {status}. Check the base URL and key.")

    try:
        payload = response.json()
    except ValueError as e:
        raise ValueError(f"{target} didn't answer with JSON, so it isn't a model endpoint.") from e

    models = _normalize(payload)
    if not models:
        raise ValueError("Connected, but that provider listed no models.")
    return models


def api_host_for(host: str) -> str:
    """The API host for a bare provider domain, from the catalog:
    "openai.com" -> "api.openai.com", "anthropic.com" -> "api.anthropic.com".
    Anything the catalog doesn't know (or already an API host) comes back as is."""
    from urllib.parse import urlparse
    h = (host or "").lower()
    name, _, port = h.partition(":")
    if port:
        return host
    matches = []
    for p in provider_catalog():
        api = (urlparse(p.get("base_url") or "").hostname or "").lower()
        if api == name:
            return host
        if api.endswith("." + name):
            matches.append(api)
    return matches[0] if len(set(matches)) == 1 else host

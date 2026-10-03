"""Base Trick class and callmodel utility for petsitter."""

import html
import contextvars
import json
import re
import uuid
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import httpx

from petsitter import raw as raw_http

from petsitter.observability import current_request_id, get_logger, request_tag


_model_url = ""
_model_name = ""
_api_key = ""
_modelset: dict[str, dict[str, Any]] = {}


def configure(model_url: str, model_name: str = "", api_key: str = ""):
    """Configure global model settings for sync calls."""
    global _model_url, _model_name, _api_key
    _model_url = model_url
    _model_name = model_name
    _api_key = api_key


def configure_modelset(modelset_raw: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Store a modelset dict (key → {url, model, key}).

    Each value should be a dict with optional keys ``url``, ``model``, ``key``.
    ``model`` and ``key`` may be boolean ``false`` to indicate passthrough.
    Returns the stored dict for inspection.
    """
    global _modelset
    parsed: dict[str, dict[str, Any]] = {}
    for key, cfg in modelset_raw.items():
        if isinstance(cfg, dict):
            parsed[key] = {
                "url": cfg.get("url", "").rstrip("/"),
                "model": cfg.get("model"),
                "key": cfg.get("key"),
            }
        else:
            parsed[key] = {"url": str(cfg).rstrip("/"), "model": "", "key": ""}
    _modelset = parsed
    return parsed


def update_model_config(key: str, model_url: str, model_name: Any = "", api_key: Any = "") -> dict[str, dict[str, Any]]:
    """Add or update a single model config entry by key.

    ``model_name`` and ``api_key`` may be boolean ``false`` for passthrough,
    or a string value.
    """
    global _modelset
    _modelset[key] = {"url": model_url.rstrip("/"), "model": model_name, "key": api_key}
    return _modelset


def remove_model_config(key: str) -> bool:
    """Remove a model config entry by key. Returns True if removed."""
    global _modelset
    return _modelset.pop(key, None) is not None


def get_model_config(key: str = "default") -> dict[str, Any]:
    """Get model config (url, model, key) for a modelset key.

    Falls back to the globally configured defaults if key is "default"
    and no modelset entry exists.
    ``model`` and ``key`` may be boolean ``false`` (passthrough).
    """
    if key in _modelset:
        return dict(_modelset[key])
    if key == "default":
        return {"url": _model_url, "model": _model_name, "key": _api_key}
    raise KeyError(
        f"Model key {key!r} not found in modelset. "
        f"Available keys: {list(_modelset.keys()) or '(none)'}"
    )


# A configured model URL can be written with /v1 ("http://localhost:11434/v1"),
# without it ("http://localhost:11434"), or as the whole endpoint. Usually the
# API lives under /v1 either way, but not always: GitHub Models is at
# ".../inference/chat/completions". So a base without /v1 has two candidate
# API roots, tried in order; whichever answers first (a model list, or a chat)
# is remembered here and used from then on.
_API_ROOTS: dict[str, str] = {}


def _base_key(base: str) -> str:
    u = (base or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/models"):
        if u.endswith(suffix):
            u = u[: -len(suffix)]
    return u


def api_root_candidates(base: str) -> list[str]:
    """Where the OpenAI-style API may live for this base, most likely first."""
    u = _base_key(base)
    if u.endswith("/v1"):
        return [u]
    found = _API_ROOTS.get(u)
    rest = [r for r in (u + "/v1", u) if r != found]
    return ([found] if found else []) + rest


def learn_api_root(base: str, root: str) -> None:
    """Remember that this base's API answered at root."""
    _API_ROOTS[_base_key(base)] = root


# Set by the proxy while a prompt keyword's handler runs: a function giving the
# conversation as the channel's extensions would send it to the model.
_preview: contextvars.ContextVar = contextvars.ContextVar("petsitter_transform_preview", default=None)


def transformed_messages() -> list | None:
    """For a prompt keyword's handler: the conversation as the channel's
    extensions would send it to the model (system prompts added, pre_hooks
    run), worked out on a copy. The model isn't called, and extensions that
    only watch (``needs_window = 0``) don't run, so nothing is logged for a
    request that never happens. None outside a handler."""
    fn = _preview.get()
    return fn() if fn else None


def chat_completions_url(base: str) -> str:
    """The chat endpoint for a configured model URL, whichever way it was written."""
    u = (base or "").strip().rstrip("/")
    if u.endswith("/chat/completions"):
        return u
    return api_root_candidates(u)[0] + "/chat/completions"


def build_upstream_payload(model_cfg: dict[str, Any], messages: list, extra: dict | None = None) -> dict:
    """Build the upstream request payload from a model config.

    If ``model_cfg["model"]`` is boolean ``false``, the ``model`` field is
    omitted from the payload (passthrough).  ``""`` or missing means the
    upstream default.
    """
    payload: dict[str, Any] = {"messages": messages}
    model_val = model_cfg.get("model")
    if model_val is not False:
        payload["model"] = (model_val if model_val else "default")
    if extra:
        for k in ("temperature", "max_tokens", "tools", "tool_choice"):
            if k in extra:
                payload[k] = extra[k]
    return payload


def build_upstream_headers(model_cfg: dict[str, Any], extra_headers: dict | None = None) -> dict[str, str]:
    """Build headers for an upstream request from a model config.

    If ``model_cfg["key"]`` is boolean ``false``, no Authorization header is
    set (passthrough).  ``""`` or missing also means no header.
    """
    headers = dict(extra_headers) if extra_headers else {}
    headers.setdefault("Content-Type", "application/json")
    api_key = model_cfg.get("key")
    if api_key is not False and api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def callmodel_sync(
    context: list,
    user_message: str = "",
    model_url: str = "",
    model_name: str = "",
    api_key: str = "",
    tools: list | None = None,
) -> list:
    """Synchronously call the model and get a response.

    Simple helper for tricks that need to loop back to the model.
    Appends the user_message and calls the model, returning updated context.
    Can target a different model by passing model_url/model_name. ``tools``
    are offered to the model; its reply (the last message) then may carry
    ``tool_calls``.
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
    if tools:
        payload["tools"] = tools

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    get_logger().info(
        "%scallmodel_sync: %s model=%r messages=%d",
        request_tag(), chat_completions_url(model_url), model_name or "default", len(messages),
    )
    with raw_http.sync_client() as client:
        response = client.post(
            chat_completions_url(model_url),
            json=payload,
            headers=headers,
        )
        response.raise_for_status()
        result = response.json()

    assistant_message = result["choices"][0]["message"]
    return messages + [assistant_message]


# petsitter's reserved id. Anything that starts with it was put there by
# petsitter, not by the user, their tool or the model: a secret's stand-in,
# the name of a tool petsitter answers itself. One fixed string, so something
# that comes back later (a stand-in the model echoes turns afterwards) is still
# recognisably petsitter's, and a trick can sniff for it. Twelve base64
# characters (about 71 bits), found nowhere on Google or GitHub code search.
DEFAULT_PREFIX = "gRefWg2D7zO8"
_PREFIX = DEFAULT_PREFIX
PREFIX_RE = re.compile(r"[0-9A-Za-z]{8,32}")
# A reserved name's own id: 128 bits in base62, always 22 characters. Hex
# with hyphens (a classic UUID) takes 36 for the same bits and costs more
# tokens; base62 carries about 5.95 bits a character to hex's 4.
_B62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_ID_LEN = 22
_ID = r"[0-9A-Za-z]{22}"


def reserved_id(data: bytes | None = None) -> str:
    """128 bits as 22 base62 characters: random, or the first 16 bytes of
    ``data`` (to derive the same id from the same input)."""
    n = int.from_bytes(data[:16], "big") if data is not None else uuid.uuid4().int
    out = []
    for _ in range(_ID_LEN):
        n, r = divmod(n, 62)
        out.append(_B62[r])
    return "".join(reversed(out))


def get_prefix() -> str:
    """petsitter's reserved id (see _PREFIX). Anything petsitter puts into a
    conversation starts with it; see reserved()."""
    return _PREFIX


def set_prefix(prefix: str) -> None:
    """Use a different reserved id (petsitter's Settings page). Applied at
    startup, before any trick is loaded: tricks build tool names and patterns
    from it when they load. 8 to 32 letters and digits."""
    global _PREFIX
    if not PREFIX_RE.fullmatch(prefix or ""):
        raise ValueError("The reserved id is 8 to 32 letters and digits")
    _PREFIX = prefix


def reserved(context: str, ident: str | None = None) -> str:
    """A reserved name: ``<prefix>-<context>-<id>``, e.g.
    ``gRefWg2D7zO8-sp-3Lw9rTqXc0VbN7mK2pZs4a``, the id 22 base62
    characters (reserved_id()). A fresh id unless ``ident`` is given."""
    return f"{_PREFIX}-{context}-{ident if ident is not None else reserved_id()}"


def reserved_pattern(context: str) -> "re.Pattern":
    """Matches the reserved names for one context (see reserved())."""
    return re.compile(re.escape(f"{_PREFIX}-{context}-") + _ID + r"(?![0-9A-Za-z])")


def get_raw() -> dict | None:
    """The current request's raw HTTP: the client's request as it arrived,
    and every call petsitter made to a provider for it, bodies included. See
    petsitter.raw for the shape. None outside a request."""
    return raw_http.get_raw()


def call_upstream_sync(messages: list, tools: list | None = None) -> dict:
    """Ask the model this request is going to, again, and return its reply
    (an OpenAI-shaped assistant message).

    Unlike ``callmodel_sync``, which always uses the default model, this goes
    where the current request goes: the same provider, model, key and API
    (a ``/use/`` upstream, Anthropic for Claude Code). For a trick that answers
    some of the model's tool calls itself and then lets it carry on. Outside a
    request it falls back to the default model.
    """
    from petsitter.observability import request_meta
    resend = request_meta().get("resend")
    if resend is not None:
        return resend(messages, tools)
    return callmodel_sync(messages, tools=tools)[-1]


def find_prompt_keyword_patterns(text: str) -> list[dict]:
    """Find every prompt keyword pattern in text, with its span.

    Three forms:
        ``(keyword)``
        ``(keyword: request)``   request may nest balanced parens; trimmed
        ``(keyword=DrequestD)``  sed-style: D is any character the user picks
                                 that doesn't occur in the request, which is
                                 taken verbatim -- no trimming, and parens or
                                 anything else allowed. One optional space on
                                 either side of ``=``.

    Delimited matches carry ``"delimited": True``.
    """
    results: list[dict] = []
    i = 0
    while i < len(text):
        if text[i] != '(':
            i += 1
            continue
        j = i + 1
        if j >= len(text) or not (text[j].isalnum() or text[j] == '_'):
            i += 1
            continue
        while j < len(text) and (text[j].isalnum() or text[j] in ('_', '/')):
            j += 1
        kw_end = j
        if j >= len(text):
            i += 1
            continue
        if text[j] == ':':
            j += 1
            while j < len(text) and text[j].isspace():
                j += 1
            depth = 1
            k = j
            while k < len(text) and depth > 0:
                if text[k] == '(':
                    depth += 1
                elif text[k] == ')':
                    depth -= 1
                k += 1
            if depth != 0:
                i += 1
                continue
            request = text[j:k - 1].strip()
            delimited = False
        elif text[j] == '=' or (text[j] == ' ' and text[j + 1:j + 2] == '='):
            j += 2 if text[j] == ' ' else 1
            if text[j:j + 1] == ' ':
                j += 1
            if j >= len(text) or text[j].isspace() or text[j] == ')':
                i += 1
                continue
            close = text.find(text[j] + ')', j + 1)
            if close == -1:
                i += 1
                continue
            request = text[j + 1:close]
            k = close + 2
            delimited = True
        elif text[j] == ')':
            k = j + 1
            request = ""
            delimited = False
        else:
            i += 1
            continue
        keyword = text[i + 1:kw_end].strip()
        results.append({
            "start": i,
            "end": k,
            "keyword": keyword,
            "request": request,
            "delimited": delimited,
        })
        i = k
    return results


class LiveFeed:
    """Recent events a trick published for its Live page, newest last.

    Deliberately dumb: whatever the trick hands publish() is kept as-is, no
    schema. A page that opens late still sees the recent past, which is the
    point -- you use your tool, then come and look.
    """

    KEEP = 500

    def __init__(self):
        self._lock = threading.Lock()
        self._events: deque = deque(maxlen=self.KEEP)
        self._seq = 0

    def publish(self, event: Any) -> None:
        with self._lock:
            self._seq += 1
            self._events.append((self._seq, event))

    def since(self, seq: int) -> list[tuple[int, Any]]:
        with self._lock:
            return [(n, e) for n, e in self._events if n > seq]

    def clear(self) -> None:
        with self._lock:
            self._events.clear()


class Trick:
    """Base class for all petsitter tricks.

    Subclass this and implement any of the hooks to add functionality.

    Set ``keywords`` to a list of strings to make this trick keyword-activated.
    When the user includes a keyword in their message, the trick is invoked
    and the keyword is stripped before sending to the model.
    Tricks with no keywords are always active (when their trickset matches).

    Set ``required_models`` to declare what model keys the trick needs from
    a modelset. Default is ["default"] — the single model configured via
    --url/--model/--key. Multi-model tricks (e.g. KennelTrick) should
    override with additional keys like ["default", "thinker", "toolcall"].
    Set ``optional_models`` for models the trick uses when they're set up and
    does without otherwise (Politeify's "rephraser" falls back to "default").
    Both are shown on the extension's page, so people know what to set up.

    Subclasses should set:
        __brief__: Short one-line description shown in the dashboard.
        __display_name__: Human-readable name (defaults to class name).
        __category__: Free-form grouping label (e.g. "Tool Calling",
            "Diagnostics"). There is no fixed list -- the dashboard just
            groups the Available Tricks list by whatever values the
            installed tricks happen to use, alphabetically, with uncategorized
            tricks under "Other". Reuse an existing category where one fits;
            check the other tricks in this directory before inventing a new one.
        config_fields: Optional list of configurable key/value settings
            (see ``Trick.configure`` for the schema). Tricks with config
            fields get a gear icon in the dashboard to edit them.

    Observing and reporting:
        Hooks only see their own slice of a request. A trick that wants the
        rest — what the tricks ahead of it changed, which ones went dormant —
        subscribes to the pipeline with
        ``petsitter.observability.subscribe(callback)``, conventionally in
        ``startup()``, and drops it again in ``shutdown()``.

        A trick should also *report* anything a viewer could not otherwise
        infer, with ``petsitter.observability.trace_event``. Removing a tool
        from the payload is visible on its own; the reason for removing it is
        not, so say it::

            trace_event("gate", self, withheld=dropped, reason=f"phase={phase}")

        Both directions are inert when nothing is watching, so instrumenting a
        trick costs nothing until someone turns an observer on.

    Live page (optional):
        A trick can ship a page that shows it working, in a "Live" tab on its
        extension page in the dashboard. It's a dumb container: petsitter
        serves the page and two pipes, and imposes no schema on either.

            ui_page = "my_trick.html"   # file next to the trick's module

        or override ``ui_html()`` to return the HTML directly. The page is
        served at ``.../ui/<id>/``, so it reaches its pipes by relative URL:

            new EventSource("events")         # recent past, then live
            fetch("action", {method: "POST", body: JSON.stringify({...})})

        ``self.publish(anything_json_able)`` feeds ``events``; ``ui_action(data)``
        answers ``action`` and returns a JSON-able reply (or None).

        Most tricks need none of that: call ``self.report("what I did")`` and
        the standard Live page shows it as a log. A page of your own replaces
        the standard one.

    Lifecycle hooks (called automatically by the framework):
        install()    — when the trick is first added to a trickset
        startup()    — when the first concurrent request uses this trick (0→1)
        shutdown()   — when the last concurrent request finishes (1→0)
        uninstall()  — when the trick is removed from a trickset
    """

    keywords: list[str] = []
    prompt_keyword: str = ""
    # False leaves "(prompt_keyword: request)" where the user typed it, on
    # every turn, for the trick's own pre_hook to rewrite in place;
    # handle_prompt_keyword is not called for it. The pattern then reaches
    # the model verbatim unless the pre_hook deals with it.
    strip_prompt_keyword: bool = True
    required_models: list[str] = ["default"]
    optional_models: list[str] = []
    replace_system_prompt: bool = False
    __brief__: str = ""
    __display_name__: str = ""
    __category__: str = ""
    config_fields: list[dict] = []

    ui_page: str = ""

    # Streaming: how much of the reply this trick's post_hook needs to see at
    # once. -1: all of it (the reply is held until it's complete). 0: none;
    # the post_hook only looks, and runs once the reply has been sent, on the
    # reassembled reply (whatever it changes is ignored). N: the reply streams
    # with its last N characters held back, and the post_hook runs on each
    # stretch as it passes, so anything it looks for that's at most N long is
    # always seen whole. It must then be right on any stretch of the reply and
    # change nothing when run again on its own output. Tool calls always reach
    # it whole. The channel uses the largest window of its tricks; see
    # reply_window.py. Only matters for tricks with a post_hook.
    needs_window: int = -1

    @property
    def request_id(self) -> str:
        """The ID of the request being handled ("" outside one). The same from
        the moment the request arrives until its reply goes back, so a trick
        that logs can tag every line with it and lines from different places
        can be put side by side."""
        return current_request_id()

    def problems(self) -> list[str]:
        """What's wrong with how this trick is set up, as sentences a person
        can act on: what's missing and how to fix it. Empty when all's well.

        The dashboard shows these on the extension and its channel, so a trick
        that's quietly doing less than it says (a missing dependency, a
        setting that can't work) doesn't go unnoticed. Called whenever the
        dashboard lists extensions: keep it cheap.
        """
        return []

    @property
    def live_feed(self) -> LiveFeed:
        # Created on first use: subclasses don't call super().__init__().
        feed = self.__dict__.get("_live_feed")
        if feed is None:
            feed = self.__dict__.setdefault("_live_feed", LiveFeed())
        return feed

    def publish(self, event: Any) -> None:
        """Send one event to this trick's Live page. Cheap, never blocks."""
        self.live_feed.publish(event)

    def report(self, message: str, **details: Any) -> None:
        """Say what you just did, in one sentence, for the Live page.

        The standard Live page shows these as a timestamped log, so a trick
        gets a readable view of itself without writing any UI::

            self.report("Replaced 3 em-dashes")
            self.report("Rephrased a message", before=old[:80], after=new[:80])

        Never put anything in a report you wouldn't show on screen.
        """
        self.publish({"event": "report", "message": str(message),
                      "details": details or None, "ts": time.time()})

    def ui_html(self) -> str | None:
        """The Live page's HTML.

        Defaults to the standard page (a log of ``report()`` calls); set
        ``ui_page`` or override this for a page of your own.
        """
        if self.ui_page:
            module = sys.modules.get(type(self).__module__)
            base = Path(getattr(module, "__file__", "") or ".").parent
            return (base / self.ui_page).read_text(encoding="utf-8")
        page = (Path(__file__).resolve().parent / "gui" / "live_default.html").read_text(encoding="utf-8")
        name = getattr(self, "__display_name__", "") or type(self).__name__
        return page.replace("__TRICK_NAME__", html.escape(name))

    def current_settings(self) -> list[dict]:
        """The trick's settings as it's actually running: one entry per
        ``config_fields`` key, with the value in use (defaults included, so a
        blank path shows the real default path)."""
        out = []
        for f in type(self).config_fields or []:
            key = f.get("key") if isinstance(f, dict) else None
            if not key:
                continue
            value = getattr(self, key, f.get("default"))
            if not isinstance(value, (str, int, float, bool, type(None), list, dict)):
                value = str(value)
            out.append({"key": key, "label": f.get("label") or key,
                        "type": f.get("type", "text"), "value": value})
        return out

    def ui_action(self, data: Any) -> Any:
        """Answer a POST from the Live page. Return anything JSON-able.

        The standard page sends ``{"action": "clear"}`` and
        ``{"action": "settings"}``; overriding pages can send whatever they like.
        """
        action = data.get("action") if isinstance(data, dict) else None
        if action == "clear":
            self.live_feed.clear()
        elif action == "settings":
            return {"settings": self.current_settings()}
        return {"ok": True}

    @classmethod
    def has_ui(cls) -> bool:
        """Every trick has a Live page: its own, or the standard report log."""
        return True

    @classmethod
    def has_custom_ui(cls) -> bool:
        return bool(cls.ui_page) or cls.ui_html is not Trick.ui_html

    def configure(self, config: dict) -> None:
        """Apply per-trick key/value config to this instance.

        Keys must match entries in ``config_fields``. Values are stored on
        the instance as attributes of the same name, so hooks can read them
        from ``self``. Subclasses that need to react to a change (e.g. reload
        a file) should override and call ``super().configure(config)``.

        Each ``config_fields`` entry is a dict:
            key (str, required)         — config key / attribute name
            label (str, required)       — friendly name shown in the dashboard
            description (str, optional) — help text shown under the label
            type (str, optional)        — "text" | "number" | "boolean" (default "text")
            default (optional)          — fallback value when none is stored
            required (bool, optional)   — whether a value must be provided
        """
        for key, value in config.items():
            setattr(self, key, value)

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

    def handle_prompt_keyword(self, request: str, messages: list | None = None, payload: dict | None = None) -> dict | None:
        """Handle a prompt keyword detected in the user message.

        When the framework finds ``(<prompt_keyword>: <request>)`` in a user
        message, it strips the pattern from the message and calls this method
        with the extracted request text.

        Args:
            request: The text after the keyword and colon, e.g. "add a thinking mode".
            messages: The full conversation context (list of message dicts) at the
                time the keyword was detected. May be ``None`` if not provided.
            payload: The original request payload dict (contains tools, temperature, etc.).
                May be ``None`` if not provided.

        Returns:
            An assistant message dict like ``{"role": "assistant", "content": "..."}``
            to inject as the model response, or ``None`` to let the normal pipeline
            continue after stripping.
        """
        return None

    def system_prompt(self, to_add: str) -> str:
        """Add instructions to the system prompt.

        By default the returned text is *appended* to the existing system
        prompt (deduplicated, so repeated injection doesn't stack). Set
        ``replace_system_prompt = True`` on the trick to instead have the
        return value *replace* the whole system prompt (e.g. swapharness,
        which genuinely swaps in a complete harness prompt).

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
        lines.append(f'    "{key}": {{')
        lines.append(f'      "url": "http://localhost:11434",')
        lines.append(f'      "model": "your-model-here"{comma}')
        lines.append(f'    }}{comma}')
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

    get_logger().info(
        "%scallmodel: %s model=%r messages=%d",
        request_tag(), chat_completions_url(model_url), model_name or "default", len(messages),
    )
    async with raw_http.async_client() as client:
        response = await client.post(
            chat_completions_url(model_url),
            json=payload,
            headers=headers,
        )
        response.raise_for_status()
        result = response.json()

    assistant_message = result["choices"][0]["message"]
    return context + [assistant_message]

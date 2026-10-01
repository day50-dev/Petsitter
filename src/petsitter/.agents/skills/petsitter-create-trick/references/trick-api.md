# Trick API Reference

## Trick base class (`petsitter.trick.Trick`)

```python
class Trick:
    __brief__: str = ""
    __display_name__: str = ""
    keywords: list[str] = []
    required_models: list[str] = ["default"]

    def system_prompt(self, to_add: str) -> str: ...
    def pre_hook(self, context: list, params: dict) -> list: ...
    def post_hook(self, context: list) -> list: ...
    def info(self, capabilities: dict) -> dict: ...
```

All hooks default to returning their input unchanged. Override only the hooks you need.

### Class attributes

| Attribute | Type | Description |
|-----------|------|-------------|
| `__brief__` | `str` | One-line summary shown in the dashboard GUI. Every trick should set this. |
| `__display_name__` | `str` | Human-readable name for the GUI. Falls back to the class name if empty. |
| `keywords` | `list[str]` | If set, the trick only activates when at least one keyword appears in the user's message. Keywords are stripped from the message before sending to the model. |
| `required_models` | `list[str]` | Model keys this trick needs from a modelset. Default is `["default"]`. Multi-model tricks override with additional keys like `["default", "thinker", "toolcall"]`. |

### `system_prompt(to_add: str) -> str`

Called once per request, before anything is sent to the upstream model.

- `to_add`: The current system prompt content (or `""` if none).
- Return: Modified system prompt. Return `""` to leave unchanged.
- Multiple tricks each get a chance to modify the system prompt in order.
- Use this to inject formatting rules, tool calling instructions, or behavior constraints.

### `pre_hook(context: list, params: dict) -> list`

Called after the system prompt is finalized but before the request is sent to the model.

- `context`: List of message dicts `[{"role": str, "content": str}, ...]`. The system prompt is the first message if present.
- `params`: The full request parameters dict. Contains `tools`, `temperature`, `max_tokens`, etc.
- Return: Modified context list.
- Use this to inject tool definitions into `params["tools"]`, modify messages, or add additional context.

### `post_hook(context: list) -> list`

Called after the upstream model responds, with the assistant's response appended to the context.

- `context`: Messages list with the model's response as the last entry: `context[-1]` is `{"role": "assistant", "content": "...", ...}`.
- Return: Modified context. The last message becomes the final response.
- Use this to validate output (e.g. JSON parsing), retry with feedback via `callmodel`, detect and reformat tool calls, or transform the response content.

Note that `post_hook` is not given `params`. Anything it needs to know about the request that produced the response comes from the request metadata channel below — **not** from state cached on `self`.

## Live page (optional)

Every trick gets a **Live** tab on its extension page in the dashboard, and
installing it (or clicking it once installed) opens straight onto that tab.

Most tricks only need to say what they did. Call `self.report()` and the
standard Live page shows it as a timestamped log; no UI to write:

```python
def post_hook(self, context):
    n = reply.count("\u2014")
    if n:
        ...
        self.report(f"Replaced {n} em-dashes in a reply")
    return context

self.report("Rephrased a message", before=old[:300], after=new[:300])  # details expand
```

Only the trick knows what it changed (it can edit anything in flight however it
likes), so reporting is its job; the framework can't infer it.

A trick that wants more can ship its own page instead. It is a dumb container:
petsitter serves your HTML and two pipes, and puts no schema on either.

```python
class MyTrick(Trick):
    ui_page = "my_trick.html"          # a file next to my_trick.py

    def post_hook(self, context):
        self.publish({"saw": len(context)})   # anything JSON-able
        return context

    def ui_action(self, data):         # optional: POSTs from the page
        if data.get("action") == "clear":
            self.live_feed.clear()
        return {"ok": True}
```

The page is served at `/api/tricks/ui/<id>/`, so it uses relative URLs:

```js
const events = new EventSource("events");   // the recent past, then live
events.onmessage = m => draw(JSON.parse(m.data));
fetch("action", {method: "POST", headers: {"Content-Type": "application/json"},
                 body: JSON.stringify({action: "clear"})});
```

- `publish()` is cheap and never blocks. The last 500 events are kept in memory,
  so opening the tab after using your tool still shows what happened.
- For a single-file trick, override `ui_html()` and return the HTML as a string.
- The page runs with the dashboard's own access, same as your trick's Python.
- Never publish anything you wouldn't show on screen. Secrets Protector publishes
  what kind of secret it hid and the stand-in, never the value.
- Examples: `tricks/tool_monitor.py` + `tool_monitor.html` (a full viewer, with a
  built-in demo) and `tricks/secrets_protector.py` + `secrets_protector.html`
  (a small activity log).

## Request metadata (`petsitter.observability`)

### `request_meta() -> dict`

Per-request metadata, carried alongside the payload for the life of one request. Backed by a `contextvar`, so concurrent requests each get their own and cannot see each other's.

The proxy populates it before any hook runs:

| Key | Value |
|---|---|
| `request_id` | Short correlation id, the same one `request_tag()` prints |
| `payload` | The full incoming request body |
| `tools` | `payload["tools"]`, or `[]` |
| `model` | The requested model string |
| `stream` | Whether the client asked for a stream |

Tricks may also write to it as scratch space to carry their own state from one hook to another within a single request:

```python
def pre_hook(self, context, params):
    request_meta()["my_trick_saw_tools"] = bool(params.get("tools"))
    return context

def post_hook(self, context):
    if not request_meta().get("my_trick_saw_tools"):
        return context
    ...
```

**Do not use instance attributes for per-request state.** A trick object is shared across every concurrent request in its trickset, so `self._something = ...` in `pre_hook` can be overwritten by another request before `post_hook` reads it. Instance attributes are for configuration and for state that is deliberately long-lived (caches, counters, tallies).

Outside a request — a lifecycle hook, or a direct call in a test — `request_meta()` returns an inert empty dict, so reads are safe and writes are discarded. Tests that exercise `pre_hook`/`post_hook` together should open one explicitly with `start_request_meta()` / `reset_request_meta(token)`.

### `info(capabilities: dict) -> dict`

Called when building the final response to declare capabilities.

- `capabilities`: Accumulated dict from earlier tricks' `info()` calls.
- Return: Updated capabilities dict. Add keys but don't remove existing ones.
- Example: `capabilities["json_mode"] = True`

## Context utilities (`petsitter.context`)

```python
from petsitter.context import (
    get_system_prompt,
    set_system_prompt,
    append_to_system_prompt,
    get_last_message,
    set_last_message_content,
    add_message,
)
```

| Function | Signature | Description |
|----------|-----------|-------------|
| `get_system_prompt` | `(context) -> str` | Extract system prompt content from first message |
| `set_system_prompt` | `(context, content) -> list` | Set or replace the system prompt |
| `append_to_system_prompt` | `(context, addition) -> list` | Append text to the system prompt |
| `get_last_message` | `(context) -> dict\|None` | Get the last message in context |
| `set_last_message_content` | `(context, content) -> list` | Replace the last message's content |
| `add_message` | `(context, role, content) -> list` | Append a new message |

## `callmodel` utilities (`petsitter.trick`)

Two helpers for making follow-up calls to the upstream model from within a trick.

### `callmodel_sync(context, user_message="") -> list`

Synchronous. Uses the globally configured model URL (set during ProxyHandler init).

- Appends `user_message` as a user message, calls the model, returns the updated context with the assistant's response appended.
- Best for simple retry loops from `post_hook`.

### `callmodel(context, instruction="", model_url="", model_name="", api_key="") -> list`

Async. Requires `model_url`.

- Appends `instruction` to the system prompt (or creates one), calls the model, returns updated context.
- Use when you need to pass a custom model URL or need async operation.

## Message format

Each message is a dict:

```python
{"role": "system" | "user" | "assistant" | "tool", "content": str}
```

For tool calls, the assistant message may also contain:

```python
{
    "role": "assistant",
    "content": None,
    "tool_calls": [
        {
            "id": "call_abc123",
            "type": "function",
            "function": {"name": "tool_name", "arguments": '{"key": "val"}'}
        }
    ]
}
```

## File structure

```
tricks/
├── __init__.py
├── your_trick.py      # <-- your trick goes here
├── code_validator.py   # self-healing code validation via model self-description
├── tool_call.py        # built-in examples
├── json_mode.py        # JSON output enforcement
├── xml_tool.py         # XML-style tool calling
└── ...
```

The file must define exactly one class that subclasses `Trick`. Helper functions and additional classes are fine as long as they don't subclass `Trick`.

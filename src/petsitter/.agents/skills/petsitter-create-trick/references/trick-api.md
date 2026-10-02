# Trick API Reference

## Trick base class (`petsitter.trick.Trick`)

```python
class Trick:
    __brief__: str = ""
    __display_name__: str = ""
    __category__: str = ""
    keywords: list[str] = []
    required_models: list[str] = ["default"]
    prompt_keyword: str = ""
    strip_prompt_keyword: bool = True
    config_fields: list[dict] = []
    needs_window: int = -1
    ui_page: str = ""
    replace_system_prompt: bool = False

    # request hooks
    def handle_prompt_keyword(self, request: str) -> dict | None: ...
    def system_prompt(self, to_add: str) -> str: ...
    def pre_hook(self, context: list, params: dict) -> list: ...
    def post_hook(self, context: list) -> list: ...
    def info(self, capabilities: dict) -> dict: ...

    # lifecycle hooks
    def install(self) -> None: ...
    def startup(self) -> None: ...
    def shutdown(self) -> None: ...
    def uninstall(self) -> None: ...

    # settings, the dashboard, and diagnostics
    def configure(self, config: dict) -> None: ...
    def problems(self) -> list[str]: ...
    def report(self, message: str, **details) -> None: ...
    def publish(self, event) -> None: ...
    def ui_action(self, data) -> Any: ...
    request_id: str    # property: this request's ID
```

All hooks default to returning their input unchanged. Override only the hooks you need.
The module's docstring is the extension's page in the dashboard: its first line
is the tagline, the rest is Markdown (`## How to use`, `## How it works`).

### Class attributes

| Attribute | Type | Description |
|-----------|------|-------------|
| `__brief__` | `str` | One-line summary shown in the dashboard GUI. Every trick should set this. |
| `__display_name__` | `str` | Human-readable name for the GUI. Falls back to the class name if empty. |
| `__category__` | `str` | Grouping in the dashboard's extension list. Reuse an existing one if it fits. |
| `keywords` | `list[str]` | If set, the trick only activates when at least one keyword appears in the user's message. Keywords are stripped from the message before sending to the model. |
| `required_models` | `list[str]` | Model keys this trick needs from a modelset. Default is `["default"]`. Multi-model tricks override with additional keys like `["default", "thinker", "toolcall"]`. |
| `prompt_keyword` | `str` | Registers `(keyword: request)` in user messages; see Prompt keywords. |
| `strip_prompt_keyword` | `bool` | `False` leaves the pattern in the message for the trick's own `pre_hook` to rewrite. |
| `config_fields` | `list[dict]` | Settings the dashboard shows a form for; see Settings. |
| `needs_window` | `int` | How much of a streamed reply `post_hook` needs at once; see Streaming replies. |
| `ui_page` | `str` | An HTML file next to the trick, to replace the standard Live page. |
| `replace_system_prompt` | `bool` | `True` makes `system_prompt`'s return value replace the whole system prompt instead of being appended. |

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

## Prompt keywords

`prompt_keyword = "mycommand"` lets the user type `(mycommand: some request)`
anywhere in a message. The framework finds it, removes it before the model sees
it, and calls:

```python
def handle_prompt_keyword(self, request: str) -> dict | None:
    return {"role": "assistant", "content": f"You asked: {request}"}
```

- Return a message dict and it becomes the reply; the model isn't called.
- Return `None` and the request carries on to the model without the pattern.
- `(mycommand)` and `(mycommand:)` give an empty `request`.
- `(mycommand=|value with ) parens|)` takes everything between a delimiter of
  the user's choosing, verbatim.
- With `strip_prompt_keyword = False` the pattern stays where the user typed
  it, on every turn, and `handle_prompt_keyword` isn't called: the trick's own
  `pre_hook` rewrites it in place (Secrets Protector replaces
  `(secret: value)` with a stand-in this way). `find_prompt_keyword_patterns`
  from `petsitter.trick` is the parser the framework uses.

## Settings

```python
config_fields = [
    {"key": "path", "label": "Log folder", "type": "path",
     "default": "~/.cache/petsitter/traffic",
     "description": "Where to write the log files."},
]
```

| Field key | Meaning |
|---|---|
| `key` (required) | The attribute name the value arrives as on `self` |
| `label` (required) | Shown in the dashboard |
| `description` | Help text under the field |
| `type` | `"text"` (default), `"number"`, `"boolean"` or `"path"` |
| `default` | Used when nothing is stored |
| `required` | Whether a value must be given |

Values are set as attributes by `configure(config)`, when the trick is loaded and
whenever they change. Override `configure` (and call `super().configure(config)`)
to react to a change, such as reloading a file. Read settings as
`getattr(self, "path", default)` in hooks, since nothing may be stored yet. The
values in use are shown on the extension's page and its Live tab.

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

## The request ID

`self.request_id` is the ID of the request being handled. Petsitter assigns it
the moment a request arrives and keeps it until the reply goes back, so it's
the same in `pre_hook`, `post_hook` and anything they log, and the same one
petsitter's own log lines carry (`[ab12cd34] ...`). Tag your log lines with it
so lines about one request, from different places, can be lined up: the
Traffic Logger does, so two of them (one first in a channel, one last) show
exactly what the tricks in between changed. It's `""` outside a request.

## Reporting problems

If a trick can't do all it says (a missing optional dependency, a setting
that can't work), return what's wrong from `problems()`:

```python
def problems(self) -> list[str]:
    if not shutil.which("rg"):
        return ["ripgrep isn't installed, so searches fall back to grep and are "
                "much slower. Install it with `apt install ripgrep`."]
    return []
```

Each entry is a sentence saying what's wrong and how to fix it. Text in
backticks is shown as code. The dashboard puts a "!" on the extension and on
its channel in the sidebar, and shows the sentences in a card on the
extension's page. It's called whenever the dashboard lists extensions, so keep
it cheap. Return `[]` when all is well (the default).

## Streaming replies

A reply streams to the client as the model writes it. A trick with a
`post_hook` says how much of the reply it needs to see at once with
`needs_window`:

| `needs_window` | Meaning | Examples |
|---|---|---|
| `-1` (default) | The whole reply. It's held until complete. | Code Validator, JSON mode |
| `0` | None. The `post_hook` only looks, and runs once the reply has been sent, on the reassembled reply. What it changes is ignored. | Traffic Logger, Tool Monitor |
| `N` | The last `N` characters. The reply streams with only those held back. | No em-dash (`1`), Secrets Protector (`52`, the length of one stand-in) |

The channel uses the largest window among its tricks, and `-1` wins outright.
With a window of `N`, the held-back tail plus the newly arrived text goes through
the rewriting `post_hook`s as an ordinary assistant message. Everything but the
last `N` characters of the result is sent. So anything a trick looks for that is
at most `N` characters long is always seen whole before any of it is sent. Tool
calls are held to the end and reach the `post_hook` whole.

A trick with a window must meet two conditions:

- **Local:** its `post_hook` is right on any stretch of the reply, not only on
  the whole thing. Replacing a character is local; capitalizing the first word
  isn't.
- **Idempotent:** run again on its own output, it changes nothing, because the
  held-back tail goes through more than once.

`needs_window` can be a property when the size depends on state the trick has
at the start of the reply (after `pre_hook`).

While a reply is held whole, petsitter sends heartbeats so the client's timeout
doesn't fire during a long generation. These are SSE comment lines on
`/v1/chat/completions` and Anthropic `ping` events on `/v1/messages`. On
`/v1/messages`, thinking blocks always pass through unchanged, and with no
window the whole event stream does.

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

`content` isn't always a string. It can be `None` (an assistant turn that only
calls tools) or a list of parts, which some clients send for text and images:

```python
{"role": "user", "content": [{"type": "text", "text": "what's this?"},
                             {"type": "image_url", "image_url": {"url": "data:..."}}]}
```

A trick that reads or rewrites text should handle all three. A tool's result
comes back as a `tool` message tied to the call by its id:

```python
{"role": "tool", "tool_call_id": "call_abc123", "content": "{\"rows\": 3}"}
```

Requests from Anthropic-style clients (Claude Code, on `/v1/messages`) are
converted to this same shape before any hook runs, and back afterwards, so a
trick only ever deals with one format.

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

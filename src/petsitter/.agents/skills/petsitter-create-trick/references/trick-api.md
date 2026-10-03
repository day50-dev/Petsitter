# Trick API Reference

## Trick base class (`petsitter.trick.Trick`)

```python
class Trick:
    __brief__: str = ""
    __display_name__: str = ""
    __category__: str = ""
    keywords: list[str] = []
    required_models: list[str] = ["default"]
    optional_models: list[str] = []
    prompt_keyword: str = ""
    strip_prompt_keyword: bool = True
    config_fields: list[dict] = []
    needs_window: int = -1
    ui_page: str = ""
    replace_system_prompt: bool = False

    # request hooks
    def handle_prompt_keyword(self, request: str, messages: list | None = None,
                              payload: dict | None = None) -> dict | None: ...
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
    def ui_html(self) -> str | None: ...
    live_feed: LiveFeed  # property: what publish() fills; .clear() empties it
    request_id: str      # property: this request's ID
```

All hooks default to returning their input unchanged. Override only the hooks you need.
The framework creates the trick with `cls()`, so an `__init__` needs defaults for
all its arguments; it doesn't have to call `super().__init__()`.
The module's docstring is the extension's page in the dashboard: its first line
is the tagline, the rest is Markdown (`## How to use`, `## How it works`).

### Class attributes

| Attribute | Type | Description |
|-----------|------|-------------|
| `__brief__` | `str` | One-line summary shown in the dashboard GUI. Every trick should set this. |
| `__display_name__` | `str` | Human-readable name for the GUI. Falls back to the class name if empty. |
| `__category__` | `str` | Grouping in the dashboard's extension list. Reuse an existing one if it fits: `Agents`, `Context & Prompts`, `Diagnostics`, `Output & Style`, `Reasoning & Quality`, `Safety & Privacy`, `Tool Calling`. |
| `keywords` | `list[str]` | If set, the trick only activates when one of them appears in the user's latest message (whole word, any case). It's removed from the message before the model sees it. Keyword-activated tricks run after the channel's always-on ones. |
| `required_models` | `list[str]` | Model keys this trick needs; see Models. Default `["default"]`. |
| `optional_models` | `list[str]` | Model keys this trick uses if they're set up, otherwise the default model; see Models. |
| `prompt_keyword` | `str` | Registers `(keyword: request)` in user messages; see Prompt keywords. |
| `strip_prompt_keyword` | `bool` | `False` leaves the pattern in the message for the trick's own `pre_hook` to rewrite. |
| `config_fields` | `list[dict]` | Settings the dashboard shows a form for; see Settings. |
| `needs_window` | `int` | How much of a streamed reply `post_hook` needs at once; see Streaming replies. |
| `ui_page` | `str` | An HTML file next to the trick, to replace the standard Live page. |
| `replace_system_prompt` | `bool` | `True` makes `system_prompt`'s return value replace the whole system prompt instead of being appended. |

### `system_prompt(to_add: str) -> str`

Called once per request, before anything is sent to the upstream model.

- `to_add`: The current system prompt content (or `""` if none).
- Return: Text to append (skipped if the prompt already contains it), or `""`
  to leave it unchanged. With `replace_system_prompt = True` the return value
  replaces the whole prompt.
- Multiple tricks each get a chance to modify the system prompt in order.
- Use this to inject formatting rules, tool calling instructions, or behavior constraints.

### `pre_hook(context: list, params: dict) -> list`

Called after the system prompt is finalized but before the request is sent to the model.

- `context`: List of message dicts `[{"role": str, "content": str}, ...]`. The system prompt is the first message if present.
- `params`: The request body itself (`tools`, `temperature`, `max_tokens`, ...). Its `messages` is the conversation before the hooks; use `context`.
- Return: Modified context list. This is what the model gets.
- Use this to inject tool definitions into `params["tools"]`, modify messages, or add additional context.
- The client resends the whole history every request, so a trick that rewrites history (Context Editor) does it again in every `pre_hook`.

### `post_hook(context: list) -> list`

Called after the upstream model responds, with the assistant's response appended to the context.

- `context`: Messages list with the model's response as the last entry: `context[-1]` is `{"role": "assistant", "content": "...", ...}`.
- Return: Modified context. The last message becomes the final response. If it carries `tool_calls`, `finish_reason` is set to `"tool_calls"` for you.
- Use this to validate output (e.g. JSON parsing), retry with feedback via `callmodel`, detect and reformat tool calls, or transform the response content.

Note that `post_hook` is not given `params`. Anything it needs to know about the request that produced the response comes from the request metadata channel below — **not** from state cached on `self`.

## Prompt keywords

`prompt_keyword = "mycommand"` lets the user type `(mycommand: some request)`
anywhere in a message. The framework finds it, removes it before the model sees
it, and calls:

```python
def handle_prompt_keyword(self, request: str, messages: list | None = None,
                          payload: dict | None = None) -> dict | None:
    return {"role": "assistant", "content": f"You asked: {request}"}
```

- All three arguments are passed positionally, so the signature must accept
  them. `messages` is the conversation as the tool sent it (pattern already
  removed); `payload` is the request body.
- Return a message dict and it becomes the reply; the model isn't called.
- Return `None` and the request carries on to the model without the pattern.
- If it raises, the error becomes the reply.
- Only the latest user message fires handlers. Older turns are only cleaned of
  the pattern. Several keywords run in the order typed; the first to return a
  message answers.
- `(mycommand)`, `(mycommand:)`, or a message that is just `mycommand` give an
  empty `request`.
- `(mycommand=|value with ) parens|)` takes everything between a delimiter of
  the user's choosing, verbatim.
- A keyword works whichever channel the request goes to. Unknown keywords are
  removed and the model is told, in the system prompt, that they were ignored.
- People can rename the keyword per channel (the extension's Settings); the
  renamed one reaches the same handler.
- If the handler doesn't `report()`, its Live tab gets `Ran (mycommand: ...)`.
- Inside `handle_prompt_keyword`, `petsitter.trick.transformed_messages()`
  gives the conversation as the channel's extensions would send it to the
  model (system prompts added, pre_hooks run, on a copy; no model call, and
  extensions with `needs_window = 0` are skipped so they record nothing). The
  `messages` argument is the conversation as the tool sent it. Export It uses
  both for `(exportit: both)`.
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
| `type` | `"text"` (default), `"number"`, `"boolean"`, `"path"` (a text box) or `"choice"` (pick one of `options`) |
| `options` | For `"choice"`: the values to pick from |
| `default` | Shown in the form when nothing is stored |
| `required` | Whether a value must be given |

Values are set as attributes by `configure(config)`, when the trick is loaded and
whenever they change. Override `configure` (and call `super().configure(config)`)
to react to a change, such as reloading a file. Nothing may be stored yet, and a
field the user cleared arrives as `""`, so read settings as
`getattr(self, "path", None) or DEFAULT`. The values in use are shown on the
extension's page and its Live tab.

## Models

`required_models` names the model keys a trick needs, `optional_models` the ones
it uses if they're set up. Both are shown on the extension's page ("Needs a model
named ...", "Uses ... if you set one up, otherwise your default model"). Nothing
enforces them; the trick looks a model up when it needs it:

```python
from petsitter.trick import callmodel_sync, get_model_config

try:
    cfg = get_model_config("rephraser")   # {"url", "model", "key"}
except KeyError:                          # not set up
    cfg = get_model_config("default")
reply = callmodel_sync(ctx, model_url=cfg["url"], model_name=cfg["model"] or "",
                       api_key=cfg["key"] or "")[-1]
```

`model` and `key` may be `False` (passthrough), hence the `or ""`. Falling back
to `"default"` is the trick's job, as above (Politeify).

To ask **the model this request is going to** again, rather than the default
one, use `call_upstream_sync(messages, tools=None)`. It goes to the same
provider, model, key and API as the request (a `/use/` upstream, Anthropic for
Claude Code) and returns the reply as an assistant message. It's for a trick
that answers some of the model's tool calls itself and then lets it carry on,
as Expose Petsitter does. Outside a request it falls back to the default model.

Anything petsitter itself puts into a conversation starts with its reserved
id, `get_prefix()` (`gRefWg2D7zO8`), in the form `<id>-<context>-<uuid>`:
`reserved("redacted")` gives `gRefWg2D7zO8-redacted-c74a3c40-...` (Secrets
Protector's stand-ins), and `reserved_pattern("redacted")` matches them.
Expose Petsitter's tool names are `gRefWg2D7zO8-<name>`. Use these for yours,
and sniff for them to tell petsitter's own things from the user's.

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

- `EventSource("events")` doesn't cost a connection of its own: petsitter adds
  a small script when it serves the page, and the stream goes over the one
  connection the whole browser shares with the dashboard. (A browser allows
  only 6 connections to a host across all its tabs.) Write it as a normal
  `EventSource`; nothing else changes.
- `publish()` is cheap and never blocks. The last 500 events are kept in memory,
  so opening the tab after using your tool still shows what happened.
- For a single-file trick, override `ui_html()` and return the HTML as a string.
- The page runs with the dashboard's own access, same as your trick's Python.
- `ui_action` gets the POSTed JSON (or `None`) and its return value is the
  response (`None` sends `{"ok": true}`). The standard page sends
  `{"action": "clear"}` and `{"action": "settings"}`.
- A Live page can change the extension's settings: return
  `{"save_config": {"key": value, ...}}` from `ui_action`. The server merges it
  into the extension's stored settings, calls `configure()` with the result,
  saves the channel, and strips `save_config` from the reply (adding
  `save_error` if saving failed). Use `config_fields` keys, so the change shows
  in Settings and lasts across restarts.

```python
def ui_action(self, data):
    if (data or {}).get("action") == "block":
        self.blocked_tools = data["names"]
        return {"ok": True, "save_config": {"blocked_tools": self.blocked_tools}}
    return super().ui_action(data)
```

- Never publish anything you wouldn't show on screen. Secrets Protector publishes
  what kind of secret it hid and the stand-in, never the value.
- Examples: `tricks/tool_monitor.py` + `tool_monitor.html` (Tool Dashboard: a
  full viewer with a built-in demo, and per-tool switches saved with
  `save_config`), `tricks/context_editor.py` + `context_editor.html` (edits the
  conversation through `ui_action`), and `tricks/secrets_protector.py` +
  `secrets_protector.html` (a small activity log).

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
| `0` | None. The `post_hook` only looks and must change nothing. When the reply streams, it runs once the reply has been sent, on the reassembled reply, and changes are ignored; when the reply is held whole (not streamed, or another trick needs `-1`), it runs in order with the others. | Traffic Logger, Tool Dashboard, Context Editor |
| `N` | The last `N` characters. The reply streams with only those held back. | No em-dash (`1`), Secrets Protector (`52`, the length of one stand-in) |

The channel uses the largest window among its tricks, and `-1` wins outright.
With a window of `N`, the held-back tail plus the newly arrived text goes through
the rewriting `post_hook`s as an ordinary assistant message. Everything but the
last `N` characters of the result is sent. So anything a trick looks for that is
at most `N` characters long is always seen whole before any of it is sent. Tool
calls are held to the end and reach the `post_hook` whole.

If you rebuild streamed tool calls yourself, use
`petsitter.proxy.add_tool_call_pieces(calls, pieces)`: a piece with a name starts
a new call, names are never appended, and `index` is ignored (upstreams fill it
with anything).

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
| `payload` | The full incoming request body (on `/v1/messages`, converted to the OpenAI shape) |
| `x_title` | The client's `X-Title` header |
| `tools` | `payload["tools"]`, or `[]` |
| `model` | The requested model string |
| `stream` | Whether the client asked for a stream |
| `api` | `"anthropic"` on `/v1/messages`; absent otherwise |

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

### Watching the pipeline

Hooks only see their own slice. A trick that reports on the whole pipeline (what
earlier tricks changed, which went dormant) subscribes, conventionally in
`startup()`, and unsubscribes in `shutdown()`:

```python
from petsitter.observability import subscribe, unsubscribe, trace_event

def startup(self):
    subscribe(self._on_event)    # called with {"stage", "trick", "request_id", ...}

def shutdown(self):
    unsubscribe(self._on_event)
```

The callback runs on the request's own task: keep it quick. A trick should also
say what a viewer couldn't otherwise infer, such as why it removed a tool:
`trace_event("gate", self, withheld=dropped, reason="...")`. Both cost nothing
while nothing is watching. Tool Dashboard is the example.

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
Hooks are synchronous, so use `callmodel_sync` from them.

### `callmodel_sync(context, user_message="", model_url="", model_name="", api_key="", tools=None) -> list`

- Appends `user_message` (if given) as a user message, calls the model, and
  returns a new list: the context plus the assistant's reply. `context` isn't
  changed.
- Without `model_url`/`model_name`/`api_key` it uses the model petsitter was
  started with. For another model, pass the values from `get_model_config(key)`
  (see Models).
- `tools` are offered to the model; the reply (`[-1]`) may then carry
  `tool_calls`.
- 60-second timeout; raises on HTTP errors, so catch exceptions where a failed
  call shouldn't break the request.

### `callmodel(context, instruction="", model_url="", model_name="", api_key="") -> list`

Async; `model_url` is required. Appends `instruction` to the system prompt (or
creates one), calls the model, returns the context plus the reply. Only usable
from async code, not from a hook.

### `transformed_messages() -> list | None`

From `petsitter.trick`, for prompt keyword handlers; see Prompt keywords. `None`
outside a handler.

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

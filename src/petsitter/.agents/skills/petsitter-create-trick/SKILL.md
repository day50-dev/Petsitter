---
name: petsitter-create-trick
description: Create new petsitter tricks. Use when the user asks to add, create, write, or implement a new trick module for the petsitter proxy. A trick is a Python class that intercepts LLM requests/responses to add capabilities like tool calling, JSON enforcement, or custom logic.
---

## How tricks work

A trick is a Python class that subclasses `Trick` from `petsitter.trick`. The
dashboard calls them **extensions**; the code and these docs call them tricks.
Tricks are installed into **channels** (tricksets in the code): every request
that matches a channel runs through its tricks, top to bottom.

A trick hooks into the request pipeline at up to five points, all optional:

| Hook | When it runs | Purpose |
|------|-------------|---------|
| `handle_prompt_keyword(request, messages, payload)` | When the user types `(keyword: ...)` | Answer or act on an inline command, before the model is called |
| `system_prompt(to_add)` | Once per request, before the model call | Add instructions to the system prompt |
| `pre_hook(context, params)` | After the system prompt, before the model | Change messages, add or remove tools |
| `post_hook(context)` | After the model responds | Validate, retry, detect tool calls, rewrite or just observe the reply |
| `info(capabilities)` | When building the response | Declare what the trick adds |

Each hook runs for every trick in the channel, in channel order (tricks
activated by `keywords` run after the always-on ones).

Beyond the hooks, a trick can:

- **Have settings** (`config_fields`): the dashboard builds a form, and values
  arrive as attributes on `self`.
- **Show what it does**: `self.report("...")` writes to its Live tab in the
  dashboard, or ship a custom page (`ui_page`) that can also change its
  settings (`ui_action` returning `{"save_config": {...}}`).
- **Use more models** (`required_models`, `optional_models`): shown on the
  extension's page so people know what to set up in Models.
- **Say how much of a streamed reply it needs** (`needs_window`), so replies
  stream instead of being held when they can.
- **Report a broken setup** (`problems()`): the dashboard shows a "!" with your
  explanation.
- **Tag its logs** with `self.request_id`, the ID the request keeps from
  arrival to reply.

## Creating a trick

1. Start from [the template](assets/trick-template.py). Put the file anywhere;
   built-ins live in `src/petsitter/tricks/`.
2. Write the module docstring as the extension's page (see below).
3. Implement only the hooks you need.
4. Decide `needs_window` if you have a `post_hook` (see Gotchas).
5. Call `self.report()` whenever the trick actually does something, so its
   Live tab isn't empty.
6. Load it, then exercise it from the dashboard's **Try it** panel (see Testing).

## Required metadata

| Attribute | Purpose | Example |
|-----------|---------|---------|
| module docstring | The extension's page in the dashboard: the first line is its tagline, the rest is Markdown | see below |
| `__brief__` | One-line summary for lists | `"Enforces valid JSON output with automatic retry on failure"` |
| `__display_name__` | Human-readable name | `"JSON Mode"` |
| `__category__` | Grouping in the dashboard. Reuse an existing one from `src/petsitter/tricks/` if one fits | `"Diagnostics"` |

The module docstring is shown as the extension's README, so write it for the
person installing it: what it does and why, then `## How to use`, then
`## How it works`.

```python
"""Replaces em-dashes in the model's replies with plain hyphens.

Some models lean on em-dashes heavily. ...

## How to use

Install it. Nothing to type; it works on every reply in the channel.

## How it works

- `post_hook` replaces each em-dash in the reply with a hyphen.
"""
```

File names are `snake_case.py`, class names `PascalCaseTrick`. A file must
contain exactly one `Trick` subclass. Helper functions and other classes are fine.
The class is instantiated with no arguments, so any `__init__` needs defaults
(it doesn't have to call `super().__init__()`).

## Gotchas

- **Never keep per-request state on `self`.** One trick instance serves every
  concurrent request in its channel. Carry state from `pre_hook` to
  `post_hook` in `request_meta()` (see trick-api.md). `self` is for settings
  and deliberately long-lived state.
- **`needs_window`** matters for any trick with a `post_hook`:
  - `-1` (the default) holds the whole reply until it's complete. Right for
    anything that validates or retries.
  - `0` means the trick only looks, so it must not change anything. When the
    reply streams, the `post_hook` runs after it's sent and changes are
    ignored; when it's held whole, it runs in order with the others.
  - `N` streams the reply with the last N characters held back. The
    `post_hook` is then run on each stretch as it passes, so it must be
    *local* (right on any part of the reply) and *idempotent* (changes
    nothing when run again on its own output).
- **Content isn't always a string.** A message's `content` can be a string,
  `None` (an assistant turn that only calls tools), or a list of parts like
  `[{"type": "text", "text": "..."}, {"type": "image_url", ...}]`. Handle all
  three when you read or rewrite messages.
- `system_prompt(to_add)` receives the current system prompt. Return text to
  append, or `""` to leave it unchanged.
- `pre_hook` gets `params` (the request body: `tools`, `temperature`, ...).
  Mutate `params["tools"]` to change the tool list the model sees.
- `post_hook` gets the context with the reply as `context[-1]`. It isn't
  given `params`; use `request_meta()`.
- `info` gets the capabilities accumulated so far. Add keys, never remove them.
- Hooks are synchronous: make follow-up model calls with `callmodel_sync()`
  (it returns the context with the new reply appended, and takes `tools=`).
  `callmodel()` is async and can't be awaited from a hook.
- Keep hooks fast. They run on every request, often on long conversations.
- Never `report()` or `publish()` something you wouldn't show on screen
  (secrets, full prompts of other people).
- `keywords = [...]` makes a trick run only when one of the words is in the
  user's latest message (whole word, any case; the word is removed first).
  `prompt_keyword = "x"` is different: the user types `(x: request)` and
  `handle_prompt_keyword(request, messages, payload)` is called. Accept all
  three arguments; the framework passes them positionally. People can rename
  the keyword per channel in the extension's Settings.
- A setting cleared in the dashboard arrives as `""`, not the default: read
  it as `getattr(self, "key", None) or DEFAULT`.

## Loading it

From the command line, into a channel (`_default` is the Default channel):

```
pet add _default path/to/my_trick.py
curl -X POST localhost:8080/readconfig     # a running petsitter picks it up
```

Or into a running petsitter directly:

```
POST /api/tricks/load {"path": "path/to/my_trick.py", "trickset": "_default"}
```

A trick can be loaded before the models it needs (`required_models`) are set
up. Nothing checks them up front: `get_model_config(key)` raises `KeyError`
when the trick asks for one that isn't there.

## Testing

- **Unit tests:** instantiate the trick and call its hooks with plain lists of
  message dicts. See `tests/` for many examples. Wrap calls that read
  `request_meta()` in `start_request_meta()` / `reset_request_meta(token)`.
- **In the dashboard:** open the channel, click the extension, and use
  **Try it**. Each reply shows a pill per extension, lit when it changed
  something. The extension's **Live** tab shows its `report()`s. Try it sends
  its own system prompt first (a test-harness note so models don't refuse
  fake credentials); your `system_prompt` is appended to it.
- **Tool calls:** Try it has a **Table** the model can use through
  `get_table()`, `get_value(key)` and `set_value(key, value)`. Put values in
  it and ask the model to read or change them. That exercises a trick on tool
  results (`role: "tool"` messages) and on the model's tool calls without a
  real agent.

## Reference

- [trick-api.md](references/trick-api.md): the full `Trick` API, settings,
  Live pages, streaming, request metadata, and the helpers.
- [hook-examples.md](references/hook-examples.md): annotated examples from
  the built-in tricks.
- Built-in tricks worth reading (in `src/petsitter/tricks/`):
  - `no_emdash.py`: the smallest complete one.
  - `secrets_protector.py`: prompt keyword, windowed `post_hook`, tool calls,
    `problems()`, custom Live page.
  - `logger.py`: settings, `needs_window = 0`, `request_id`.
  - `tool_monitor.py` (Tool Dashboard): a custom Live page whose switches are
    saved as settings via `save_config`; pipeline `subscribe()`.
  - `context_editor.py`: rewrites the history in `pre_hook` on every request.
  - `politeify.py`: an optional model (`optional_models`).
  - `exportit.py`: `transformed_messages()` in a prompt keyword handler.

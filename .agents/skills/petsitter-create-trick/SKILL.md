---
name: petsitter-create-trick
description: Create new petsitter tricks. Use when the user asks to add, create, write, or implement a new trick module for the petsitter proxy. A trick is a Python class that intercepts LLM requests/responses to add capabilities like tool calling, JSON enforcement, or custom logic.
---

## How tricks work

A trick is a Python class that subclasses `Trick` from `src.trick`. It lives in the `tricks/` directory and hooks into the proxy pipeline at up to 4 points:

| Hook | When it runs | Purpose |
|------|-------------|---------|
| `system_prompt(to_add)` | Once per request, before model call | Inject instructions into the system prompt |
| `pre_hook(context, params)` | After system prompt, before model | Modify conversation, inject tool definitions |
| `post_hook(context)` | After model responds | Validate output, retry, detect tool calls, transform response |
| `info(capabilities)` | When building response | Declare what capabilities the trick adds |

The execution order of hooks matches the list order in the ProxyHandler. Tricks are applied sequentially for each hook phase.

## Creating a trick

1. Create a `.py` file in `tricks/` (e.g. `tricks/my_trick.py`)
2. Import `from src.trick import Trick`
3. Create a class that inherits from `Trick`
4. Implement any of the 4 hook methods (you only need the ones relevant to your use case)
5. If needed, use `callmodel` or `callmodel_sync` to make follow-up calls to the model

## Required conventions

- The class must be a direct subclass of `Trick` (not `Trick` itself)
- The file path must be loadable via `importlib` - use a `.py` extension
- Keep hooks free of `await` if the trick needs sync-only support - tricks that use `callmodel_sync` work in both contexts
- Store per-trick state on `self` - each loaded trick is a fresh instance

## Required metadata

Every trick class **must** set these class attributes:

| Attribute | Purpose | Example |
|-----------|---------|---------|
| `__doc__` | Module-level docstring explaining what the trick does | `"""Enforces valid JSON output with retry."""` |
| `__brief__` | One-line summary shown in the dashboard | `"Enforces valid JSON output with automatic retry on failure"` |
| `__display_name__` | Human-readable name for the GUI | `"JSON Mode"` |

The class itself should also have a docstring. The file name convention is `snake_case.py` with the class name as `PascalCaseTrick`.

See the [template](assets/trick-template.py) for the exact structure.

## Lifecycle hooks (optional)

Every trick can implement up to 4 lifecycle hooks that the framework calls automatically:

| Hook | When it runs | Purpose |
|------|-------------|---------|
| `install()` | Once when the trick is first added to a trickset | Clone repos, download files, create resources |
| `startup()` | When the first concurrent request uses this trick (run counter 0→1) | Open connections, preload models |
| `shutdown()` | When the last concurrent request finishes (run counter 1→0), or on server shutdown | Close connections, release resources |
| `uninstall()` | When the trick is removed from a trickset | Undo install actions |

The startup/shutdown hooks use a reference counter: `startup()` is called when the first concurrent request begins, and `shutdown()` is called when the last finishes. Multiple concurrent requests to the same trick won't trigger repeated startup/shutdown calls. On server exit, `shutdown()` is called for all active tricks.

Example:
```python
class MyTrick(Trick):
    def install(self):
        self.model = download_model("some-pipeline")
        logger.info("Model downloaded")

    def startup(self):
        self.session = create_session()

    def shutdown(self):
        self.session.close()

    def uninstall(self):
        import shutil
        shutil.rmtree(self.cache_dir, ignore_errors=True)
```

## Keyword activation (optional)

Set `keywords` on your trick class to make it only activate when a keyword appears in the user's message. The keyword is stripped from the message before sending to the model. Tricks without `keywords` are always active (when their trickset matches).

```python
class MyTrick(Trick):
    keywords = ["multiround"]  # activates only when user says "multiround"
```

Multiple keywords per trick are supported:

```python
class MyTrick(Trick):
    keywords = ["multiround", "crossval"]
```

## Gotchas

- `system_prompt(to_add)` receives the *current* system prompt text. Return modified text or append to it. Return `""` to leave unchanged.
- `pre_hook` receives `context` (list of messages) and `params` (dict with `tools`, `temperature`, etc.). Mutate `params["tools"]` directly to inject tool definitions.
- `post_hook` receives context with the assistant's response as the last message. Replace `context[-1]["content"]` or add `tool_calls` to transform output.
- `info` receives the accumulated capabilities dict from earlier tricks. Add keys but don't remove existing ones.
- `callmodel()` is async and needs `model_url` as a parameter. `callmodel_sync()` uses the globally configured model URL and is sync-only. Both return the updated context with the assistant's response appended.
- A trick file can contain helper functions and multiple classes, but only one `Trick` subclass per file will be detected by the loader.
- Use `from src.context import ...` for utilities like `get_last_message`, `add_message`, `set_last_message_content`.

## Dynamic loading without restart

Load your trick at startup:

```
petsitter --model_url http://localhost:11434 --trick tricks/my_trick.py
```

Or load it at runtime via the API:

```
POST /api/tricks/load {"path": "tricks/my_trick.py"}
```

Tricks can be loaded even without their required models configured. Model validation only happens at request time — a trick that needs a model key that isn't available will produce a runtime error when activated.

## Template

Read [the template file](assets/trick-template.py) as a starting point.

## Reference

Read [trick-api.md](references/trick-api.md) for the full Trick class API, callmodel utilities, and context helpers.

Read [hook-examples.md](references/hook-examples.md) for annotated examples from built-in tricks.

# Next version: extensions as packages

Not started. Written down so it isn't lost; nothing else we do in the meantime
makes it harder.

## The problem

An extension (a trick) was meant to be one Python file. It isn't any more:

- **Data files:** Secrets Protector reads `data/gitleaks.toml` and Politeify
  reads `data/profanity-en.txt`, each under its own license (MIT, CC BY 4.0).
- **Python dependencies:** Secrets Protector needs `detect-secrets`.
- **Shared code:** `secret_scan.py` and `reply_window.py` live outside the
  extensions that use them.
- **Live pages:** custom `.html` files next to the `.py`.
- **Metadata:** a README (the module docstring), category, version, the models
  it uses (`required_models`, `optional_models`), settings (`config_fields`).

The built-ins get away with this because they ship inside petsitter's own
package. Community extensions can't: `pet publish` and `pet install` move
exactly one pinned, checksummed `.py`, so a community Secrets Protector or
Politeify couldn't be published today. Its rules, word list and dependency
wouldn't come along.

The metadata has no single home either. It's class attributes read three
ways: the dashboard imports the file (`gui_routes._introspect_trick_file`),
`pet` imports it, and the community crawler parses it with `ast` into
`index.json`. The fields have already drifted between them.

## Don't invent a format

This has been solved many times: distro packages, wheels, npm, APKs, VS Code
`.vsix`, browser extensions. Inventing our own would be like inventing a new
way to store strings instead of using JSON.

For a Python program, the closest prior art is plugin systems built on
Python's own packaging: pytest, MkDocs, Datasette, Sphinx, and especially
Simon Willison's `llm`, whose plugins are ordinary pip packages that
`llm install` puts into llm's own environment. IDE-style archives
(`.vsix` = zip + `package.json`, JetBrains = zip + `plugin.xml`) are the same
idea, but would leave us writing our own Python dependency handling.

## Proposal

- **An extension is a normal Python package.** Its distribution format is a
  wheel: a standard zip with a standard manifest (`METADATA`) and a
  checksummed file list (`RECORD`).
- **Everything has a standard place:**
  - data files: package data;
  - licenses: `license-files`;
  - dependencies: `dependencies`, resolved by pip;
  - version, author, description: core metadata;
  - README: the package readme.
- **Discovery:** an entry point group, `[project.entry-points."petsitter.tricks"]`.
- **petsitter-specific metadata** (category, display name, models with what
  each is for, settings) goes in `[tool.petsitter]` in `pyproject.toml`, the
  standard place for one tool's settings. One documented schema, read the
  same way by the dashboard, `pet` and the crawler; `tricksinfo.json` (or the
  index) is generated from it, never hand-edited.
- **`pet install <name>`** becomes a thin wrapper around pip, aimed at a
  petsitter-owned location, so it never touches the system Python (the
  problem `detect-secrets` ran into with a checkout run on Debian's
  `/usr/bin/python3`).
- **Publishing:** PyPI, or a git URL as today. The GitHub-topic index can
  stay as the discovery layer.
- **A single `.py` file** can stay as a development convenience (a local
  folder), not the distribution format.
  (The dashboard's untested "Load from a file…" box under the extension list
  was removed for this reason; a local file still loads with
  `pet add <channel> path/to/file.py` or `POST /api/tricks/load`.)

## Open questions

- Models as `{key, required, purpose}` instead of name lists, so the
  extension page can say what each one is for.
- What else to disclose: whether it makes extra model calls per request
  (cost, latency), whether it reaches the network, its streaming behavior
  (`needs_window`), bundled data and licenses.
- Moving the built-ins: keep them inside petsitter, or make them packages
  too (dogfooding the format).
- Where installed packages live, and how a channel file refers to one
  (today: `pkg:owner/name@version`).

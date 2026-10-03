# Tricksets and routing

Grouping tricks, and deciding which requests they apply to.

[← back to the README](../README.md)

---

## Tricksets

A trickset bundles a group of tricks with routing filters. The dashboard calls tricksets **channels** and tricks **extensions**. When a request comes in, petsitter matches its `X-Title` header, `User-Agent` header and `model` field against each loaded trickset's filters, then runs only the tricks from matching sets.

Tricksets live as JSON files in the `tricksets/` directory:

```json
{
  "schema": "0.8.0",
  "name": "my-trickset",
  "enabled": true,
  "filters": {
    "X-Title": "opencode*",
    "Model": "*"
  },
  "tricks": [
    "tricks/json_mode.py",
    "tricks/tool_call.py",
    "pkg:dana/ollama-ctx@0.1.0"
  ],
  "parameters": {},
  "models": {},
  "logfile": "~/.cache/petsitter/tricksets/my-trickset.log",
  "loglevel": "INFO"
}
```

Entries are either a path to a `.py` (absolute, or relative to the package, which is how the built-ins are written) or a `pkg:<owner>/<slug>@<version>` spec pointing at a [community trick](community.md#community-tricks) you've installed. When petsitter saves a trickset, each entry becomes an object (`id`, `file`, `enabled`, and optionally `keyword` and `config`) so it can be turned off or configured on its own.

`filters` can hold any of `X-Title`, `User-Agent` and `Model`. Leave one out and it isn't checked.

The `parameters` field stores user-defined variables that tricks within the trickset can reference at runtime. The `models` field holds per-trickset model roles: each key maps to a `{url, model, key}` object (same format as the [global model config](models.md#model-configs)). Set `model` or `key` to `false` for passthrough. Manage both via the dashboard or the API.

Each loaded trickset is also exposed as a model named `trickset/<name>` (e.g., `trickset/gemma4`). Selecting this model in a client bypasses the filter matching and runs that trickset's tricks directly on every request.

A channel's own model roles are edited under **Models for this channel** on its Settings tab. Leave a role empty to use the global one from the Models page.

### Using tricksets

```bash
pet new opencode --x-title 'opencode*' -t json_mode -t tool_call
petsitter
```

Every trickset in `<config>/tricksets/` is loaded at startup, so there is
nothing to pass on the command line. `pet ts` lists what you have.

`pet new` and `pet filter` set `X-Title` and `Model`. Set a `User-Agent` filter from the dashboard (**Edit channel**) or the API.

### Managing tricksets at runtime

The control panel at `/` has a full trickset manager. **Edit channel** sets the name and the X-Title, User-Agent and Model filters, and has **Turn off** and **Delete channel**. A channel that's off stays saved (`"enabled": false` in its file) but isn't loaded, so it matches nothing until you turn it back on.

A channel's extensions run in the order listed. The dashboard shows each with a drag handle and its position number; drag to reorder.

You can also use the API:

```bash
# List loaded tricksets
curl http://localhost:8080/api/tricksets

# List available trickset files
curl http://localhost:8080/api/tricksets/available

# Load a trickset
curl -X POST http://localhost:8080/api/tricksets/load \
  -d '{"path": "tricksets/opencode.json"}'

# Update filters
curl -X PUT http://localhost:8080/api/tricksets/opencode \
  -d '{"filters": {"X-Title": "myagent*", "User-Agent": "*", "Model": "*"}}'

# Update model roles for a trickset
curl -X PUT http://localhost:8080/api/tricksets/opencode \
  -d '{"models": {"toolcall": {"url": "http://localhost:11434", "model": "lfm2.5:latest"}}}'

# Turn a trickset off (saved, and unloaded)
curl -X PUT http://localhost:8080/api/tricksets/opencode \
  -d '{"enabled": false}'

# Unload a trickset
curl -X POST http://localhost:8080/api/tricksets/unload \
  -d '{"name": "opencode"}'
```

### How routing works

1. Take `X-Title` and `User-Agent` from the request headers and `model` from the request body.
2. For each loaded trickset other than `_default`, check its filters with `fnmatch`. Matching is case-insensitive, and every filter present must match.
3. Collect the enabled tricks from every matching set, in each set's order.
4. If nothing matched, use `_default` (the dashboard's **Default** channel) instead.
5. Run the pipeline with only those tricks.

A matching channel claims the request even when it's empty or all its extensions are off; Default only runs when no other channel matches. A trickset created without filters matches `{"X-Title": "*", "Model": "*"}`, so it catches every request and Default never runs.

The Agents page lists every program petsitter has seen, with the X-Title (or User-Agent) it sends and the channel its requests went to. See [Discovered programs](agents.md#discovered-programs).

The `schema` field in a trickset JSON file records the petsitter version that wrote it. This tells tools how to interpret the file without needing an external lookup table.

### Logging

Each trickset has its own log file so you can inspect what a specific set of tricks did. The `logfile` field sets the path (default `~/.cache/petsitter/tricksets/<name>.log`) and `loglevel` sets the verbosity - `DEBUG`, `INFO`, `WARNING`, or `ERROR` (default `INFO`). Both are optional; if omitted, the defaults apply. Configure them from the Settings tab in the dashboard or via the API:

```bash
curl -X PUT http://localhost:8080/api/tricksets/my-trickset \
  -d '{"logfile": "~/.cache/petsitter/my-trickset.log", "loglevel": "DEBUG"}'
```

Every request gets a short ID the moment it arrives, kept until its reply goes back. It tags each line about that request in the matched trickset's log file and in the global activity log (the Logs tab). Tricks can read it as `self.request_id`:

```
[ab12cd34] trickset 'gemma4' matched (X-Title='', Model='gemma4') -> 2 enabled tricks
[ab12cd34] started MultiRoundTrick (run 0 -> 1)
[ab12cd34] calling upstream: http://localhost:11434/v1/chat/completions
```

Lifecycle events (install / uninstall / startup / shutdown) are written to the owning trickset's log file even when no request is running.

# Tricksets and routing

Grouping tricks, and deciding which requests they apply to.

[← back to the README](../README.md)

---

## Tricksets

A trickset bundles a group of tricks with routing filters. When a request comes in, petsitter matches the `X-Title` header and `model` field against each loaded trickset's filters, then runs only the tricks from matching sets.

Tricksets live as JSON files in the `tricksets/` directory:

```json
{
  "schema": "0.8.0",
  "name": "my-trickset",
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

Entries are either a path to a `.py` (absolute, or relative to the repo root) or a `pkg:<owner>/<slug>@<version>` spec pointing at a [community trick](community.md#community-tricks) you've installed.

The `parameters` field stores user-defined variables that tricks within the trickset can reference at runtime. The `models` field lets you override model routing for this trickset - each key maps to a `{url, model, key}` object (same format as the global model config), letting different tricksets use different models for the same role. Set `model` or `key` to `false` for passthrough. Manage both via the dashboard or the API.

Each loaded trickset is also exposed as a model named `trickset/<name>` (e.g., `trickset/gemma4`). Selecting this model in a client bypasses the filter matching and runs that trickset's tricks directly on every request.

The Models tab in the dashboard lets you configure model overrides per-trickset: select a trickset pill, then edit the model URL and name for each role. These overrides are stored in the trickset's `models` field and take precedence over the global model config when a trickset's tricks are running.

### Using tricksets

```bash
pet new opencode --x-title 'opencode*' -t json_mode -t tool_call
petsitter
```

Every trickset in `<config>/tricksets/` is loaded at startup, so there is
nothing to pass on the command line. `pet ts` lists what you have.

### Managing tricksets at runtime

The control panel at `/` has a full trickset manager. You can also use the API:

```bash
# List loaded tricksets
curl http://localhost:8080/api/tricksets

# List available trickset files
curl http://localhost:8080/api/tricksets/available

# Load a trickset
curl -X POST http://localhost:8080/api/tricksets/load \
  -d '{"path": "tricksets/gemma4.json"}'

# Update filters
curl -X PUT http://localhost:8080/api/tricksets/opencode \
  -d '{"filters": {"X-Title": "myagent*", "Model": "*"}}'

# Update model overrides for a trickset
curl -X PUT http://localhost:8080/api/tricksets/gemma4 \
  -d '{"models": {"default": "http://localhost:11434#m=llama3:8b", "toolcall": "http://localhost:11434#m=lfm2.5:latest"}}'

# Unload a trickset
curl -X POST http://localhost:8080/api/tricksets/unload \
  -d '{"name": "opencode"}'
```

### How routing works

1. Extract `X-Title` from the request header and `model` from the request body.
2. For each loaded trickset, check if its filters match using `fnmatch`.
3. Collect tricks from all matching sets, deduplicating by class name.
4. Run the pipeline with only those tricks.

A trickset created without filters matches `{"X-Title": "*", "Model": "*"}`, so it acts as a catch-all and its tricks run on every request.

The `schema` field in a trickset JSON file records the petsitter version that wrote it. This tells tools how to interpret the file without needing an external lookup table.

### Logging

Each trickset has its own log file so you can inspect what a specific set of tricks did. The `logfile` field sets the path (default `~/.cache/petsitter/tricksets/<name>.log`) and `loglevel` sets the verbosity - `DEBUG`, `INFO`, `WARNING`, or `ERROR` (default `INFO`). Both are optional; if omitted, the defaults apply. Configure them from the Settings tab in the dashboard or via the API:

```bash
curl -X PUT http://localhost:8080/api/tricksets/my-trickset \
  -d '{"logfile": "~/.cache/petsitter/my-trickset.log", "loglevel": "DEBUG"}'
```

Every request through the pipeline is tagged with a short correlation id so you can follow it end-to-end. The tag appears in the matched trickset's log file and in the global activity log (Logs tab / `GET /api/logs`):

```
[ab12cd34] trickset 'gemma4' matched (X-Title='*' Model='gemma4*')
[ab12cd34] started multiround.py (run 0 -> 1)
[ab12cd34] calling upstream http://localhost:11434/v1/chat/completions model='gemma4'
```

Lifecycle events (install / uninstall / startup / shutdown) are written to the owning trickset's log file even when no request is running.


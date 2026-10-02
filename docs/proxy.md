# Proxy behaviour

Host override, the config diagnostic, and the HTTP surface petsitter serves.

[← back to the README](../README.md)

---

## Your extensions, any provider (`/use/`)

Put `http://localhost:8080/use/<host>` in front of a provider's address and the
request goes to that provider, with your own key and model, through your
extensions: Traffic Logger, Tool Monitor, Context Monitor, Secrets Protector
and the rest. The model configured in petsitter isn't involved at all.

```bash
# Claude Code talks to Claude, through your extensions
ANTHROPIC_BASE_URL=http://localhost:8080/use/anthropic.com claude

# Codex talks to OpenAI, through the same extensions
OPENAI_BASE_URL=http://localhost:8080/use/openai.com/v1 codex
```

That makes it the quickest way to show petsitter to someone: "change your base
URL to this and open your tool as usual." They keep their own provider and model,
and get logging, tool monitoring and context analysis with nothing to install.

- **Bare provider domains work.** `openai.com`, `anthropic.com`, `groq.com`,
  `x.ai` and the other providers in the catalog resolve to their API host
  (`api.openai.com`, ...). Any other host is used as written:
  `/use/build.nvidia.com/v1`.
- **HTTPS**, except `localhost` and `127.0.0.1`, which use plain HTTP
  (`/use/localhost:11434/v1`).
- **Your key and model go through untouched**: `Authorization` for OpenAI-style
  APIs; `x-api-key`, `anthropic-version` and `anthropic-beta` for Anthropic.
- **Both APIs run through the pipeline**, streaming included: chat completions
  (`.../chat/completions`) and Anthropic's Messages API (`.../v1/messages`).
  Model listings are served too; any other path is forwarded as is.
- **Channels** are picked the usual way (X-Title, User-Agent, model), so the same
  extensions apply whichever provider a tool uses.

```bash
curl http://localhost:8080/use/build.nvidia.com/v1/chat/completions \
  -H "Authorization: Bearer <your-nvidia-key>" \
  -d '{"model":"meta/llama-3.3-70b-instruct","messages":[{"role":"user","content":"hi"}]}'
```

## Config Diagnostic (`__petsitter_config__`)

Send a single user message containing exactly `__petsitter_config__` and petsitter answers with a snapshot of its configuration instead of calling the upstream model:

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"my-model","messages":[{"role":"user","content":"__petsitter_config__"}]}'
```

The request is an **exact copy of the real traversal**: it runs the same keyword filtering, trickset selection, system-prompt injection, and pre-hooks, and builds the actual upstream URL, payload, and headers that a real request would use — then returns a snapshot in place of the upstream HTTP call. No upstream request is made, and your API key is never included (only its presence as `"set"`, `"bearer"`, or `"none"`).

The snapshot includes:

- **`model`** - configured upstream URL, model name, key presence, and the resolved target URL
- **`request`** - `X-Title`, model, `stream`, plus `original_messages` vs `transformed_messages` (exactly what upstream would receive) and the would-be upstream `payload`/`url`/`auth`
- **`trickset`** - the matched trickset and each active trick (class, display name, keywords, per-trick config)
- **`tricksets`** - every loaded trickset and `capabilities`

It works on `/v1/chat/completions` and `/use/<host>/.../chat/completions`, and honors `stream: true` (returned as a normal chunked stream). It only triggers on an exact full-message match, so ordinary conversation is unaffected.

## API Endpoints

Petsitter exposes OpenAI-compatible endpoints plus management endpoints:

**Proxy:**
- `POST /v1/chat/completions` - Chat completions (proxied + transformed)
- `GET /v1/models` - List available models (proxied)
- `GET /health` - Health check
- `* /use/{host}/{path}` - Your extensions in front of `https://{host}/{path}`, with the caller's own key and model (any method)

**Management:**
- `GET /api/info` - Server information
- `GET /api/tricks` - List loaded tricks
- `GET /api/tricks/available` - List available trick modules
- `POST /api/tricks/load` - Load a trick
- `POST /api/tricks/unload` - Unload a trick
- `POST /api/tricks/reorder` - Reorder loaded tricks
- `GET /api/stream` - The dashboard's live updates (logs, pause state, Live pages), one stream shared by every tab; `POST /api/stream/{sid}` with `{"subscribe": [...], "unsubscribe": [...]}` picks topics
- `GET /api/tricksets` - List loaded tricksets
- `GET /api/tricksets/available` - List available trickset files
- `POST /api/tricksets/load` - Load a trickset
- `POST /api/tricksets/unload` - Unload a trickset
- `GET /api/tricksets/{name}` - Get trickset details
- `PUT /api/tricksets/{name}` - Update trickset filters, tricks, parameters, models, or logging config (`logfile` / `loglevel`)

**Community index:**
- `GET /api/registry` - Search the index (`?q=`, `?all=1`, `?refresh=1`); entries are annotated with the installed version
- `POST /api/registry/install` - Install `{name, version, trickset}` and optionally wire it into a trickset
- `GET /api/registry/source` - Source of a trick (`?name=&version=`), from disk if installed

**Playground:**
- `POST /api/playground` - Run `{messages, trickset}` through the real pipeline; returns the reply plus a `trace` of which tricks ran which hooks

A Swagger UI is available at `/docs` and the OpenAPI spec at `/static/openapi.json`.


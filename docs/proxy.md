# Proxy behaviour

The routes (`/use/`, `/ignore/`, `/bypass/`), streaming, the config diagnostic, and the HTTP surface petsitter serves.

[← back to the README](../README.md)

---

## Base URL

Point tools at `http://localhost:8080/v1`. `http://localhost:8080` works too:
`/chat/completions`, `/v1/chat/completions` and `/v1/v1/chat/completions` all reach
the same endpoint (likewise `messages` and `models`).

The same goes for the provider URL you configure: with or without `/v1`,
petsitter tries both and remembers which one answered.

To let other machines use your instance, listen on every interface:
`petsitter -l 0.0.0.0:8080`.

## Your extensions, any provider (`/use/`)

Put `http://localhost:8080/use/<host>` in front of a provider's address and the
request goes to that provider, with your own key and model, through your
extensions: Traffic Logger, Tool Dashboard, Context Monitor, Secrets Protector
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

## A word in the URL (`/ignore/`)

`/ignore/<anything>/` is dropped from the path: `http://localhost:8080/ignore/openrouter.ai/v1`
is the same as `http://localhost:8080/v1`, your default model and all. The segment
is only there to be seen in the URL by tools that look at it.

Open WebUI, for example, identifies itself (`X-Title: Open WebUI`) only when the
URL it calls contains `openrouter.ai`. Give it `http://localhost:8080/ignore/openrouter.ai/v1`
and it sends the header, so a channel can match it by X-Title and it shows up by
name in Discovered programs. It works in front of any route, `/use/` included.

## Straight to the provider (`/bypass/`)

`http://localhost:8080/bypass/v1` skips petsitter's processing: no extensions, no
channel matching, the request goes as is to the configured provider (or to
Anthropic, for `/messages`). It serves `chat/completions`, `models` and
`messages`. The caller's own key is used if it sent one; otherwise the configured
key fills in. Pause does the same for every tool at once.

## Streaming

Replies stream as they arrive. A trick with a `post_hook` declares how much of the
reply it needs to see at once with `needs_window` (see
[writing tricks](writing-tricks.md)); if any extension in the channel needs the
whole reply (`-1`, the default), petsitter holds it until it's complete and sends
heartbeats every 5 seconds (SSE comments, or `ping` events on the Messages API)
so the client doesn't time out. Petsitter waits up to 15 minutes for the model to
answer.

## Config Diagnostic (`__petsitter_config__`)

Send a request whose last message is a user message containing exactly `__petsitter_config__` and petsitter answers with a snapshot of its configuration instead of calling the upstream model:

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"my-model","messages":[{"role":"user","content":"__petsitter_config__"}]}'
```

The request is an **exact copy of the real traversal**: it runs the same keyword filtering, trickset selection, system-prompt injection, and pre-hooks, and builds the actual upstream URL, payload, and headers that a real request would use — then returns a snapshot in place of the upstream HTTP call. No upstream request is made, and your API key is never included (only whether one is set: `model.api_key` is `"set"` or empty, `request.upstream.auth` is `"bearer"` or `"none"`).

The snapshot includes:

- **`model`** - configured upstream URL, model name, key presence, and the resolved target URL
- **`request`** - `X-Title`, model, `stream`, plus `original_messages` vs `transformed_messages` (exactly what upstream would receive) and the would-be upstream `payload`/`url`/`auth`
- **`trickset`** - the matched trickset and each active trick (class, display name, keywords, per-trick config)
- **`tricksets`** - every loaded trickset and `capabilities`

It works on `/v1/chat/completions` and `/use/<host>/.../chat/completions`, and honors `stream: true` (returned as a normal chunked stream). It only triggers on an exact full-message match (surrounding whitespace aside), so ordinary conversation is unaffected.

## API Endpoints

Petsitter exposes OpenAI- and Anthropic-compatible endpoints plus management endpoints:

**Proxy:**
- `POST /v1/chat/completions` - Chat completions (proxied + transformed). A model of `trickset/<name>` sends the request through that channel
- `POST /v1/messages` - Anthropic's Messages API, through the pipeline to `api.anthropic.com` with the caller's own key
- `GET /v1/models` - The provider's models, plus a `trickset/<name>` entry per channel
- `GET /health` - Health check (`{"status": "ok", "paused": ...}`)
- `* /use/{host}/{path}` - Your extensions in front of `https://{host}/{path}`, with the caller's own key and model (any method)
- `* /ignore/{word}/{path}` - Same as `/{path}`; the word is only there to appear in the URL
- `GET|POST /bypass/v1/{chat/completions,models,messages}` - Straight to the provider, no processing

**Management:**
- `GET /api/info` - Server information (version, listen address, model, provider, last upstream status)
- `GET /api/tricks` - List loaded tricks
- `GET /api/tricks/available` - List available trick modules
- `GET /api/tricks/source` - A bundled trick's source (`?path=`)
- `GET /api/tricks/{name}` - A trick's details
- `POST /api/tricks/{name}/toggle` - Turn a trick on or off in a channel
- `POST /api/tricks/load` - Load a trick
- `POST /api/tricks/unload` - Unload a trick
- `POST /api/tricks/reorder` - Reorder loaded tricks
- `GET /api/tricks/ui/{id}/` - An extension's Live page; `GET .../events` its feed, `POST .../action` calls its `ui_action()`
- `GET /api/stream` - The dashboard's live updates (logs, pause state, Live pages), one stream shared by every tab; `POST /api/stream/{sid}` with `{"subscribe": [...], "unsubscribe": [...]}` picks topics (`logs`, `pause`, `live:<id>`)
- `GET /api/tricksets` - List loaded tricksets
- `GET /api/tricksets/available` - List available trickset files
- `POST /api/tricksets/create` - Create a trickset
- `POST /api/tricksets/load` - Load a trickset
- `POST /api/tricksets/unload` - Unload a trickset
- `POST /api/tricksets/install-examples` - Install the example tricksets (`{"force": true}` overwrites)
- `GET /api/tricksets/{name}` - Get trickset details
- `PUT /api/tricksets/{name}` - Update trickset filters, tricks, parameters, models, or logging config (`logfile` / `loglevel`)
- `DELETE /api/tricksets/{name}` - Delete a trickset and its file (not `_default`)
- `GET /api/tricksets/{name}/log` - The tail of a trickset's log file (`?lines=`, default 200)
- `GET /api/models` / `POST /api/models` - Show or set the model config
- `POST /api/models/discover` - List the models a provider offers
- `GET /api/providers` - The provider catalog
- `GET /api/agents` - Coding tools petsitter knows how to connect; `GET /api/agents/registered` which are connected
- `POST /api/agents/{id}/register` / `unregister` - Connect or disconnect a tool; `POST /api/agents/{id}/trickset` creates its channel
- `GET /api/traffic` - Programs, models and channels seen; `POST /api/discovered/forget` drops a discovered program
- `GET /api/pause` / `POST /api/pause` - Read or set Pause (`{"paused": true}`)
- `POST /readconfig` - Re-read the config files and apply them without restarting
- `POST /api/shutdown` - Shut down, putting connected tools back

**Community index:**
- `GET /api/registry` - Search the index (`?q=`, `?all=1`, `?refresh=1`); entries are annotated with the installed version
- `POST /api/registry/install` - Install `{name, version, trickset}` and optionally wire it into a trickset
- `GET /api/registry/source` - Source of a trick (`?name=&version=`), from disk if installed
- `GET /api/registry/readme` - A community trick's readme

**Playground:**
- `POST /api/playground` - Run `{messages, trickset}` through the real pipeline; returns the reply plus a `trace` of which tricks ran which hooks

A Swagger UI is available at `/docs` and the OpenAPI spec at `/static/openapi.json`; it covers only the core endpoints.

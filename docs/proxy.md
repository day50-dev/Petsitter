# Proxy behaviour

Host override, the config diagnostic, and the HTTP surface petsitter serves.

[← back to the README](../README.md)

---

## Zero-Config Host Override (`/p/`)

The `/p/` route is the easy way to proxy an existing endpoint: prefix whatever host you already use with `http://localhost:8080/p/` and petsitter handles the rest. No trickset to create, no model config to swap.

```
# Instead of https://build.nvidia.com/...
http://localhost:8080/p/build.nvidia.com/...
```

Point your client's `base_url` at `http://localhost:8080/p/<host>` and petsitter forwards everything after the host to `https://<host>/<rest>` - whatever path the client appends. It's a dumb-client-friendly trick: the client just appends `/chat/completions`, `/v1/models`, or anything else to the base you give it, and petsitter proxies it through the normal trick pipeline.

Key behaviors:

- **HTTPS only** - the upstream host is always assumed `https://`. This is a convenience feature for public endpoints.
- **Auth passthrough** - your client's `Authorization` header is forwarded to the upstream, so each host's own API key works.
- **Model passthrough** - the request's `model` field goes upstream as-is.
- **Trickset selection** relies on the existing `X-Title`/`Model` filters - no special handling, the `/p/` path only overrides the upstream host.
- Chat completions (streaming included), model listings, and any other path under `/p/` are proxied.

```bash
curl http://localhost:8080/p/build.nvidia.com/v1/chat/completions \
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

It works on `/v1/chat/completions` and `/p/<host>/.../chat/completions`, and honors `stream: true` (returned as a normal chunked stream). It only triggers on an exact full-message match, so ordinary conversation is unaffected.

## API Endpoints

Petsitter exposes OpenAI-compatible endpoints plus management endpoints:

**Proxy:**
- `POST /v1/chat/completions` - Chat completions (proxied + transformed)
- `GET /v1/models` - List available models (proxied)
- `GET /health` - Health check
- `* /p/{host}/{path}` - Zero-config transparent proxy to `https://{host}/{path}` (any method)

**Management:**
- `GET /api/info` - Server information
- `GET /api/tricks` - List loaded tricks
- `GET /api/tricks/available` - List available trick modules
- `POST /api/tricks/load` - Load a trick
- `POST /api/tricks/unload` - Unload a trick
- `POST /api/tricks/reorder` - Reorder loaded tricks
- `GET /api/logs` - Activity log
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


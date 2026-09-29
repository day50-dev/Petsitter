# Model configuration

Naming upstream models so tricks can reach more than one.

[← back to the README](../README.md)

---

## Set your provider

Petsitter is a proxy to your provider. This is where you set where the traffic
goes: pick a provider, paste your key, pick a model.

The list of models comes from the provider itself, so it is never out of date.
Type to filter it, or press refresh to ask the provider again.

Picking a model fills the `url`, `model` and `key` fields, but saves nothing.
A wrong model is free to undo, and the list folds up after a pick. Touch the
model box again and it comes back.

Reload the page and the panel shows what you already have set: the provider,
the key (marked "saved", never re-shown), and the model.

The same panel shows on the Start Here tab until a provider is set, since
nothing can flow through petsitter without one.

An endpoint petsitter doesn't recognise (self-hosted, hand-written) still works.
Pick "Something else" and a base-URL box appears, pre-filled with what the role
already had. The bare `url` / `model` / `key` fields underneath still work on
their own.

Fetched models also show as suggestions on every role's model field. The field
stays free text, so a gateway with a partial list can't lock you out of a model
it didn't mention.

Your key goes to the provider, to fetch the list, and into your config when you
save. Nowhere else. Fetching the list saves nothing, so a key you're just trying
out never lands on disk.

### Providers listed as "models only"

Anthropic, Google Gemini and GitHub Models are listed so you can see their
current models, but requests can't be routed straight to them: petsitter appends
`/v1/chat/completions` to whatever base URL you give it, and these three don't
serve chat at that path. A gateway such as OpenRouter reaches all three over an
OpenAI-compatible endpoint.

---

## Model Configs

A model config JSON file lets you run multi-model tricks like [Kennel](tricks.md#kennel) that need different models for different subtasks. Each key maps to a `{url, model, key}` object:

```json
{
    "default": {
        "url": "http://localhost:11434",
        "model": "Qwen3.5:8b"
    },
    "thinker": {
        "url": "http://localhost:11434",
        "model": "VibeThinker-3B-GGUF:q4_K_M"
    },
    "toolcall": {
        "url": "http://localhost:11434",
        "model": "lfm2.5:latest",
        "key": "sk-custom-key"
    }
}
```

The `"default"` key sets the primary model, the one used when a trick doesn't ask for a specific role. Tricks declare what keys they need - for example, KennelTrick requires `["default", "thinker", "toolcall"]`. If a key is missing, petsitter prints a helpful error with the expected format.

The `model` and `key` fields accept:
- A string - use as the model name / API key in upstream requests.
- `false` (boolean) - passthrough, don't set the field at all.
- `""` (empty string) - explicitly clear the value.

Edit these from the Models tab, or with `pet model`:

```bash
pet model                                  # show every role as JSON
pet model thinker url http://localhost:11434
pet model thinker model VibeThinker-3B-GGUF:q4_K_M
pet model toolcall key false               # passthrough: use the client's key
pet model consultant --remove
```

The whole modelset can be dumped and swapped in one step — handy for backups and
for trying out a model configuration without hand-editing config.json:

```bash
pet model _default > old-default.json          # back up the modelset
cat new-model.json | pet --import model        # swap a new one in
cat old-default.json | pet --import model      # ...and back, whenever you like
pet --import model <trickset>                  # scope the swap to one trickset
```

`--import` reads a JSON object mapping model names to `{url, model, key}`
entries from stdin (exactly what `pet model _default` prints) and replaces that
scope's modelset wholesale.



Add `--trickset <name>` to scope a role to one trickset instead of the global
config; those overrides live in the trickset's `models` field and win while
that trickset's tricks are running.




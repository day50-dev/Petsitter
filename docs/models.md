# Model configuration

Naming upstream models so tricks can reach more than one.

[← back to the README](../README.md)

---

## Set your provider

Petsitter is a proxy to your provider. This is where you set where the traffic
goes: pick a provider, paste your key, pick a model. It's the **Simple** view of
the Models page, and step 1 on Start here.

The provider list is a built-in catalog: pay-per-token APIs (OpenAI, Mistral,
xAI, DeepSeek, Groq, Cerebras and more), gateways (OpenRouter, Together,
Fireworks), and local servers (Ollama, LM Studio, llama.cpp, vLLM). The catalog
holds endpoints only. The list of models comes from the provider itself, so it
is never out of date. Type to filter it, or press refresh to ask the provider
again.

Picking a model saves it as your `default` model (`url`, `model` and `key`),
and the list folds up. Touch the model box again and it comes back.

Reload the page and the panel shows what you already have set: the provider,
the key (marked "saved", never re-shown), and the model.

An endpoint petsitter doesn't recognise (self-hosted, hand-written) still works.
Pick "Something else" and a base-URL box appears, pre-filled with what the role
already had. The **Advanced** view has the bare `url` / `model` / `key` fields
for every role.

A base URL can be written with or without `/v1`. Without it, petsitter tries
`<url>/v1` first, then `<url>`, and remembers whichever answered.

Fetched models also show as suggestions on every role's model field. The field
stays free text, so a gateway with a partial list can't lock you out of a model
it didn't mention.

Fetching the list saves nothing; your key is only written to your config when
you pick a model.

### Providers listed as "models only"

Anthropic, Google Gemini and GitHub Models are listed so you can see their
current models, but requests can't be routed straight to them: they don't
serve OpenAI-style chat where petsitter looks for it. A gateway such as
OpenRouter reaches all three over an OpenAI-compatible endpoint.

---

## Model Configs

The `modelset` in `config.json` lets you run multi-model tricks like [Kennel](tricks.md#kennel) that need different models for different subtasks. Each key maps to a `{url, model, key}` object:

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

The `"default"` key sets the primary model, the one used when a trick doesn't ask for a specific role.

Tricks declare the keys they use: `required_models` for ones they can't work without, `optional_models` for ones they use when set and do without otherwise. The dashboard lists them under **Models** on each extension's page.

| Extension | Required | Optional |
|-----------|----------|----------|
| Kennel | `thinker`, `toolcall` | |
| Multi-Model Consultant | `consultant` | |
| Politeify | | `rephraser` (falls back to `default`) |

If a required key is missing, the trick fails with an error listing the keys that are set.

The `model` and `key` fields accept:
- A string - use as the model name / API key in upstream requests.
- `false` (boolean) - passthrough, don't set the field at all.
- `""` (empty string) - explicitly clear the value.

Edit these from the Models page (**Advanced**), or with `pet model`:

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
config; those overrides live in the trickset's `models` field. In the
dashboard they're under **Models for this channel** on the channel's Settings tab.

# Connecting your coding tools

Pointing Claude Code, Codex and opencode at petsitter, and putting them back.

[← back to the README](../README.md)

---

## Agents

Petsitter can route popular coding tools through the proxy in one click. When you click **Connect** on a tool's card on the Agents page, petsitter:

1. Detects the tool (its config file, or the env vars it uses)
2. Creates a trickset (channel) for it with a starter set of tricks: Context Monitor, Secrets Protector, Rules File, Export It and Tool Dashboard
3. Patches the tool's config file to point at petsitter (`http://localhost:8080` by default)
4. Saves the original config so it can be restored

**Configure** opens that tool's channel. Disconnecting restores the config, and removes the channel unless you've changed its tricks.

The **exit button** in the top-right restores every tool's original configuration and shuts petsitter down. Any other exit (Ctrl-C, `kill`) restores them too.

### Available agents

| Agent | Config | What gets patched | Channel | Checked with |
|-------|--------|-------------------|---------|--------------|
| [Hermes Agent](https://hermes-agent.nousresearch.com) | `~/.hermes/config.yaml` (`$HERMES_HOME`) | `model.provider: custom`, `model.base_url`, and `model.extra_headers` `X-Title: Hermes Agent` (it sends no title of its own) | `X-Title: Hermes Agent` | hermes-agent 0.19.0, CLI (`hermes -z`). Not yet: the desktop app, which uses a different HTTP client |
| [Claude Code](https://code.claude.com) | `~/.claude/settings.json` | `ANTHROPIC_BASE_URL` in the `env` block | `Model: claude*` | |
| [Kilo Code](https://kilo.ai) (`kilo`) | `~/.config/kilo/kilo.json` or `kilo.jsonc` (`$XDG_CONFIG_HOME`) | Provider `baseURL`, as for OpenCode (it's a fork) | `X-Title: Kilo Code*` (its own) | @kilocode/cli 7.8.3 (`kilo run`) |
| [Cline](https://cline.bot) (`cline`) | `~/.cline/data/settings/providers.json` (`$CLINE_DIR`, `$CLINE_DATA_DIR`, `$CLINE_PROVIDER_SETTINGS_PATH`) | `settings.baseUrl` and `settings.headers` `X-Title: Cline` on `lastUsedProvider` | `X-Title: Cline` | cline 3.0.68 (`cline "…"`) |
| [Codex](https://developers.openai.com/codex) | `~/.codex/config.toml` (`$CODEX_HOME`) | `openai_base_url` | `Model: gpt*` | |
| [OpenClaw](https://openclaw.ai) | `~/.openclaw/openclaw.json` (`$OPENCLAW_CONFIG_PATH`, `$OPENCLAW_STATE_DIR`) | `models.providers.<p>.baseUrl` and `headers` `X-Title: OpenClaw` on the provider of `agents.defaults.model.primary`. Plain JSON only: a hand-written JSON5 file (comments, trailing commas) is left alone, with a message saying so | `X-Title: OpenClaw` | openclaw 2026.9.9 (`openclaw agent exec`) |
| [omp](https://omp.sh) | `~/.omp/agent/models.yml` (`$PI_CODING_AGENT_DIR`) | `baseUrl` and `headers` `X-Title: omp` on the provider of `config.yml`'s `modelRoles.default` | `X-Title: omp` | @oh-my-pi/pi-coding-agent 18.5.0 (`omp -p`) |
| [pi](https://pi.dev) | `~/.pi/agent/models.json` (`$PI_CODING_AGENT_DIR`) | `baseUrl` and `headers` `X-Title: pi` on `settings.json`'s `defaultProvider` | `X-Title: pi` | @earendil-works/pi-coding-agent 1.0.0 (`pi -p`) |
| [OpenCode](https://opencode.ai) | `~/.config/opencode/opencode.json` | Provider `baseURL` | `X-Title: opencode*` | |

"Checked with" means: installed in a sandbox, a request run through petsitter landed in the tool's own channel, and the config came back byte-for-byte on shutdown. Not yet confirmed by a person on a real setup. These tools change their config formats; when one breaks, this says what it last worked with.

Not yet: **Goose**. It works pointed at petsitter by hand, but has no one-click adapter.

Not supported: **Freebuff**. Its normal mode runs on Freebuff's own models through its backend, with no base URL to change. A custom endpoint is only possible through its in-app "bring your own key" connections, which switch it off its free models, and it has no one-shot mode to test with.

Each agent saves your original config to `registry.json` in the config directory (`~/.config/petsitter` by default) and restores it on disconnect or shutdown. The file is put back exactly as it was, comments included, so anything the tool itself changed in it while connected is undone too. The tools read their config when a session starts: connect or disconnect, then start a new session.

### Discovered programs

The Agents page also lists every program that has sent requests through petsitter, whether or not it was set up here. Each row shows:

- its `X-Title` header, or its `User-Agent` when it sends no X-Title
- the models it asked for
- which channel its requests went to, and how many
- a sample of the headers from its most recent request (click the row); credential values are masked

**New channel for this** opens a new channel pre-filled to match that program: its X-Title, else its User-Agent's product name with a wildcard (`goose*`), else its model. **Forget** removes the row.

The list is saved to `discovered.json` in the config directory, so it survives restarts.

### Adding agents

New agents live in `src/petsitter/agents/` and subclass `Agent` from `agents/__init__.py`. See [`petsitter-create-agent/SKILL.md`](../src/petsitter/.agents/skills/petsitter-create-agent/SKILL.md) for the template and conventions.

### CLI

```bash
pet agents list                  # agents and whether they're connected
pet agents register claude-code
pet agents unregister claude-code
pet agents restore               # put every config back, without a running proxy
```

### API

```bash
# List available agents with detect status
curl http://localhost:8080/api/agents

# Register an agent (creates trickset, patches config)
curl -X POST http://localhost:8080/api/agents/claude-code/register

# Unregister an agent (restores original config)
curl -X POST http://localhost:8080/api/agents/claude-code/unregister

# Create the agent's trickset if it doesn't have one
curl -X POST http://localhost:8080/api/agents/claude-code/trickset

# Get registry state
curl http://localhost:8080/api/agents/registered

# Programs seen, with the channels they went to
curl http://localhost:8080/api/traffic

# Shutdown and restore all configurations
curl -X POST http://localhost:8080/api/shutdown
```

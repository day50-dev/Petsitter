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

| Agent | Config mechanism | What gets patched | Channel filters |
|-------|-----------------|-------------------|-----------------|
| [OpenCode](https://opencode.ai) | `~/.config/opencode/opencode.json` | Provider `baseURL` | `X-Title: opencode*` |
| [Claude Code](https://code.claude.com) | `~/.claude/settings.json` | `ANTHROPIC_BASE_URL` in `env` block | `Model: claude*` |
| [Codex](https://developers.openai.com/codex) | `~/.codex/config.toml` (or `$CODEX_HOME/config.toml`) | `openai_base_url` | `Model: gpt*` |

Each agent saves your original config to `registry.json` in the config directory (`~/.config/petsitter` by default) and restores it on disconnect or shutdown.

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

# Connecting your coding tools

Pointing Claude Code, Codex and opencode at petsitter, and putting them back.

[← back to the README](../README.md)

---

## Agents

Petsitter has a one-click setup wizard for routing popular coding tools through the proxy. When you click **Set up** on an agent card in the Agents tab, petsitter:

1. Detects your credentials (API keys, config files)
2. Creates a trickset with the right tricks for that tool
3. Patches the tool's config file to point at `http://localhost:8080`
4. Saves the original config so it can be restored on shutdown

The **exit button** in the top-right restores every tool's original configuration and shuts petsitter down.

### Available agents

| Agent | Config mechanism | What gets patched |
|-------|-----------------|-------------------|
| [OpenCode](https://opencode.ai) | `~/.config/opencode/opencode.json` | Provider `baseURL` |
| [Claude Code](https://code.claude.com) | `~/.claude/settings.json` | `ANTHROPIC_BASE_URL` in `env` block |
| [Codex](https://developers.openai.com/codex) | `~/.codex/config.toml` | `openai_base_url` |

Each agent saves your original config to `~/.config/petsitter/registry.json` and restores it on unregister or shutdown.

### Adding agents

New agents live in `agents/` and subclass `Agent` from `agents/__init__`. See [`.agents/skills/petsitter-create-agent/SKILL.md`](.agents/skills/petsitter-create-agent/SKILL.md) for the template and conventions.

### API

```bash
# List available agents with detect status
curl http://localhost:8080/api/agents

# Register an agent (creates trickset, patches config)
curl -X POST http://localhost:8080/api/agents/claude-code/register

# Unregister an agent (restores original config)
curl -X POST http://localhost:8080/api/agents/claude-code/unregister

# Get registry state
curl http://localhost:8080/api/agents/registered

# Shutdown and restore all configurations
curl -X POST http://localhost:8080/api/shutdown
```


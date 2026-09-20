# Command line reference

petsitter's flags and the pet subcommands.

[← back to the README](../README.md)

---

## CLI Options

| Option | Short | Description |
|--------|-------|-------------|
| `--config` | `-c` | Path to a config file (e.g., `another_petsitter_config.conf.json`) or a config directory. Defaults to `$PET_CONFIG_DIR` if set, else `~/.config/petsitter`. Tricksets live in `<base>/tricksets`. |
| `--listen` | `-l` | Host:port to listen on (default: `localhost:8080`) |
| `--version` | `-v` | Show version and exit |

### `pet` subcommands

`pet` edits the same JSON files the dashboard writes, so the two always agree and neither needs the server running. `pet --help` lists everything; the ones worth knowing:

| Command | What it does |
|---------|--------------|
| `pet ts` | List tricksets; `pet ts <name>` for detail, `pet ts <name> <trick> <param> <value>` to set one |
| `pet tricks` | List available local trick modules |
| `pet add` / `pet rm` | Add or remove a trick from a trickset |
| `pet search [query]` | Search the [community index](community.md#community-tricks) |
| `pet cat <owner>/<name>` | Print a community trick's source without installing it |
| `pet install <owner>/<name>` | Install from the index (a bare name instead runs a local trick's `install()` hook) |
| `pet installed` | List tricks installed from the index |
| `pet publish <trick>` | Publish a trick to the index |
| `pet model` | Show or set model config; `pet model _default > f.json` / `cat f.json \| pet --import model` backs up and restores the whole modelset |
| `pet agents` | List, register, unregister harness agents |


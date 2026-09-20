"""pet — a command-line companion to the petsitter dashboard.

Everything petsitter persists lives in JSON files under ``~/.config/petsitter/``
(``config.json`` for global model settings, plus one ``tricksets/<name>.json``
per trickset).  ``pet`` reads and writes those same files directly, so the CLI
and the web dashboard always see the same state — no server needs to be running.
Point any command at a different config area with ``pet -c <path>`` (a config
directory or a config file); the flag must come before the subcommand.

The commands mirror the dashboard tabs:

    pet ts                           # list tricksets (look at tricksets)
    pet ts <trickset>                # full detail for one trickset
    pet ts <trickset> <trick>        # detail for one trick
    pet ts <trickset> <trick> <param> <value>  # set a trick param
    pet tricks                       # list available trick modules
    pet new <name>                   # create a trickset
    pet delete <name>                # delete a trickset
    pet rename <old> <new>           # rename a trickset
    pet add <trickset> <trick>       # add a trick to a trickset
    pet rm <trickset> <trick>        # remove a trick from a trickset
    pet ts <trickset> <trick> enable false   # deactivate a trick
    pet ts <trickset> <trick> keyword go     # set a prompt keyword
    pet ts <trickset> <trick> config '{"k": 1}'  # set trick config
    pet reorder <trickset> <trick> <index>
    pet ts <trickset> param key value  # trickset-level parameters
    pet filter <trickset> [--x-title G] [--model G]
    pet search [query]              # search the community trick index
    pet cat <owner>/<name>          # read a trick's source before installing
    pet install <owner>/<name>      # install a trick from the index
    pet installed                   # list tricks installed from the index
    pet publish <trick>             # publish a trick to the index
    pet install <trick>             # run a local trick's install() hook
    pet uninstall <trick>           # run a local trick's uninstall() hook
    pet lifecycle <trick> <hook>
    pet model [name] [param] [value]  # show or set a model entry
    pet model _default > models.json          # back up the whole modelset
    cat models.json | pet --import model      # restore it (swap modelsets)
    pet logfile <trickset> [path]
    pet loglevel <trickset> [level]
    pet examples [--force]          # install the example tricksets
    pet agents [list|register|unregister]
"""

import json
import os
import sys
from pathlib import Path
from typing import Any

import click

from petsitter import registry
from petsitter.loader import load_trick_from_path
from petsitter.trick import Trick
from petsitter.trickset import SCHEMA, Trickset

PKG_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PKG_ROOT.parents[1]  # only meaningful in a source checkout
BUILTIN_TRICKS_DIR = PKG_ROOT / "tricks"

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


# --------------------------------------------------------------------------
# file access helpers — everything below works directly on the JSON files
# --------------------------------------------------------------------------


# Set by the -c/--config option before a subcommand runs.
_override_config_dir: Path | None = None
_override_config_path: Path | None = None


def config_dir() -> Path:
    if _override_config_dir is not None:
        return _override_config_dir
    return Path(os.environ.get("PET_CONFIG_DIR", str(Path.home() / ".config" / "petsitter")))


def config_path() -> Path:
    if _override_config_path is not None:
        return _override_config_path
    return config_dir() / "config.json"


def tricksets_dir() -> Path:
    return config_dir() / "tricksets"


def load_config() -> dict:
    p = config_path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_config(cfg: dict) -> None:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=2) + "\n")


def _ts_path(name: str) -> Path:
    return tricksets_dir() / f"{name}.json"


def _save_ts(ts: Trickset) -> None:
    if not ts.file_path:
        ts.file_path = str(_ts_path(ts.name))
    ts.save()


def _load_ts(name: str) -> Trickset:
    """Load a trickset from disk; create ``_default`` on first use."""
    p = _ts_path(name)
    if p.exists():
        return Trickset.load_from_file(str(p))
    if name == "_default":
        ts = Trickset("_default", SCHEMA, {"X-Title": "*", "Model": "*"}, [], file_path=str(p))
        ts.save()
        return ts
    raise click.ClickException(
        f"trickset '{name}' not found in {tricksets_dir()} (create it with 'pet new {name}')"
    )


def _list_ts_files() -> list[Path]:
    d = tricksets_dir()
    if not d.exists():
        return []
    return sorted(d.glob("*.json"))


def _tricks_dir() -> Path | None:
    cwd = Path("tricks")
    if cwd.is_dir():
        return cwd
    if BUILTIN_TRICKS_DIR.is_dir():
        return BUILTIN_TRICKS_DIR
    return None


def _introspect(path: Path) -> dict:
    """Extract display_name, brief, keywords, prompt_keyword from a trick module."""
    info = {
        "path": str(path),
        "display_name": None,
        "brief": None,
        "keywords": [],
        "prompt_keyword": "",
        "required_models": ["default"],
        "config_fields": [],
        "mtime": path.stat().st_mtime_ns,
    }
    try:
        import importlib.util as _util
        spec = _util.spec_from_file_location(path.stem, str(path))
        if spec and spec.loader:
            mod = _util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            for name in dir(mod):
                obj = getattr(mod, name)
                if isinstance(obj, type) and issubclass(obj, Trick) and obj is not Trick:
                    info["display_name"] = getattr(obj, "__display_name__", None) or name
                    info["brief"] = getattr(obj, "__brief__", "")
                    info["keywords"] = list(getattr(obj, "keywords", []) or [])
                    info["prompt_keyword"] = getattr(obj, "prompt_keyword", "") or ""
                    info["required_models"] = list(getattr(obj, "required_models", ["default"]) or ["default"])
                    info["config_fields"] = list(getattr(obj, "config_fields", []) or [])
                    break
    except Exception:
        pass
    return info


def _available_tricks() -> list[dict]:
    d = _tricks_dir()
    if not d:
        return []
    result = []
    for f in sorted(d.glob("*.py")):
        if f.name == "__init__.py":
            continue
        result.append(_introspect(f))
    return result


def _resolve_trick_spec(spec: str) -> str:
    """Resolve a trick name/path to a loadable ``.py`` path.

    ``json_mode`` -> ``tricks/json_mode.py``; paths and ``pkg:`` specs pass
    through unchanged.
    """
    s = spec.strip()
    if not s:
        raise click.UsageError("trick path is empty")
    if registry.parse_pkg_spec(s) is not None:
        return s
    if s.endswith(".py"):
        return s
    # Built-ins are always recorded as "tricks/<name>.py", never as the
    # absolute path they happen to live at. loader.resolve_path() resolves
    # that form package-relative, so a trickset stays portable between
    # machines and survives the venv moving.
    if Path(f"tricks/{s}.py").exists():
        return f"tricks/{s}.py"
    if (BUILTIN_TRICKS_DIR / f"{s}.py").exists():
        return f"tricks/{s}.py"
    return s


def _trick_exists(path: str) -> bool:
    # Delegate to the loader so the CLI and the runtime agree on what
    # resolves: cwd, repo root, inside the package, or a pkg: spec.
    from petsitter.loader import resolve_path
    try:
        return resolve_path(path).exists()
    except FileNotFoundError:
        return False


def _find_trick_index(ts: Trickset, spec: str) -> int | None:
    """Locate a trick by file path, class name, filename, or display name."""
    spec_l = spec.lower()
    resolved = _resolve_trick_spec(spec).lower()
    for i, path in enumerate(ts.trick_paths):
        names = {path.lower(), Path(path).stem.lower()}
        if i < len(ts.tricks) and ts.tricks[i] is not None:
            t = ts.tricks[i]
            names.add(type(t).__name__.lower())
            dn = getattr(t, "__display_name__", "") or ""
            if dn:
                names.add(dn.lower())
        if spec_l in names or resolved in names:
            return i
    return None


def _trick_label(ts: Trickset, i: int) -> str:
    if i >= len(ts.tricks) or ts.tricks[i] is None:
        return Path(ts.trick_paths[i]).stem
    t = ts.tricks[i]
    return getattr(t, "__display_name__", "") or type(t).__name__


def _parse_value(val: str) -> Any:
    """Parse a CLI value: JSON literals (3, true, null) else a string."""
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return val


def _version() -> str:
    """Version of the running petsitter.

    Installed metadata first: an installed wheel has no pyproject.toml, and
    a checkout of a *different* repo in the cwd must not be able to answer
    this question.  The pyproject read is the fallback for working from a
    source tree that was never pip-installed.
    """
    try:
        from importlib.metadata import version as _pkg_version
        return _pkg_version("petsitter")
    except Exception:
        pass
    try:
        try:
            import tomllib
        except ModuleNotFoundError:      # Python 3.10
            import tomli as tomllib
        with open(REPO_ROOT / "pyproject.toml", "rb") as f:
            return tomllib.load(f).get("project", {}).get("version", "0.0.0")
    except Exception:
        return "0.0.0"


# --------------------------------------------------------------------------
# output helpers
# --------------------------------------------------------------------------


def _print_available_tricks(tricks: list[dict], json_out: bool = False) -> None:
    if json_out:
        click.echo(json.dumps(tricks, indent=2))
        return
    if not tricks:
        click.echo("No tricks found.")
        return
    rows = []
    for t in tricks:
        name = t["display_name"] or Path(t["path"]).stem
        kw = ",".join(t["keywords"])
        if t["prompt_keyword"]:
            kw = (kw + "," if kw else "") + f"({t['prompt_keyword']})"
        rows.append((name, t["path"], t["brief"] or "", kw))
    name_w = max(len(r[0]) for r in rows) + 2
    path_w = max(len(r[1]) for r in rows) + 2
    for name, path, brief, kw in rows:
        line = f"{name:<{name_w}}{path:<{path_w}}{brief}"
        if kw:
            line += f"  [{kw}]"
        click.echo(line)


MODEL_PARAMS = ("url", "model", "key")
TRICK_PARAMS = ("enable", "keyword", "config")


def _global_models() -> dict:
    """Return the global modelset, synthesizing ``default`` from top-level fields."""
    cfg = load_config()
    models = dict(cfg.get("modelset", {}))
    if "default" not in models:
        models["default"] = {
            "url": cfg.get("model_url", ""),
            "model": cfg.get("model_name", ""),
            "key": cfg.get("api_key", ""),
        }
    return models


def _val_str(val: Any) -> str:
    return val if isinstance(val, str) else json.dumps(val)


def _model_set(name: str, param: str, val: Any, ts_name: str) -> None:
    if ts_name == "_default":
        cfg = load_config()
        modelset = dict(cfg.get("modelset", {}))
        entry = dict(modelset.get(name, {})) if isinstance(modelset.get(name), dict) else {}
        entry[param] = val
        modelset[name] = entry
        cfg["modelset"] = modelset
        if name == "default":
            cfg["model_url"] = entry.get("url", "")
            cfg["model_name"] = "" if entry.get("model") is False else (entry.get("model") or "")
            cfg["api_key"] = "" if entry.get("key") is False else (entry.get("key") or "")
        save_config(cfg)
        click.echo(f"model '{name}' {param}={_val_str(val)} saved")
        return
    ts = _load_ts(ts_name)
    entry = dict(ts.models.get(name, {})) if isinstance(ts.models.get(name), dict) else {}
    entry[param] = val
    ts.models[name] = entry
    _save_ts(ts)
    click.echo(f"model '{name}' {param}={_val_str(val)} saved on trickset '{ts_name}'")


def _model_remove(name: str, ts_name: str) -> None:
    if ts_name == "_default":
        cfg = load_config()
        modelset = dict(cfg.get("modelset", {}))
        if name not in modelset and name != "default":
            raise click.ClickException(f"model '{name}' not found")
        modelset.pop(name, None)
        if name == "default":
            cfg["model_url"] = ""
            cfg["model_name"] = ""
            cfg["api_key"] = ""
        cfg["modelset"] = modelset
        save_config(cfg)
        click.echo(f"Removed model '{name}' from global config")
        return
    ts = _load_ts(ts_name)
    if name not in ts.models:
        raise click.ClickException(f"model '{name}' not found on trickset '{ts_name}'")
    ts.models.pop(name, None)
    _save_ts(ts)
    click.echo(f"Removed model '{name}' from trickset '{ts_name}'")


def _parse_modelset(data: Any) -> dict:
    """Validate a raw stdin modelset into ``{name: {url, model, key}}``."""
    if not isinstance(data, dict):
        raise click.ClickException(
            "import expects a JSON object mapping model names to entries, "
            "e.g. {\"default\": {\"url\": \"http://localhost:11434\"}}"
        )
    modelset: dict = {}
    for key, entry in data.items():
        if not isinstance(entry, dict):
            raise click.ClickException(
                f"model '{key}' must be a JSON object, e.g. {{\"url\": \"http://...\"}}"
            )
        modelset[str(key)] = dict(entry)
    return modelset


def _import_modelset(modelset: dict, scope: str) -> None:
    """Replace the whole modelset for a scope with the imported one."""
    if scope == "_default":
        cfg = load_config()
        cfg["modelset"] = modelset
        entry = modelset.get("default")
        if isinstance(entry, dict):
            cfg["model_url"] = entry.get("url", "")
            cfg["model_name"] = "" if entry.get("model") is False else (entry.get("model") or "")
            cfg["api_key"] = "" if entry.get("key") is False else (entry.get("key") or "")
        else:
            cfg["model_url"] = ""
            cfg["model_name"] = ""
            cfg["api_key"] = ""
        save_config(cfg)
        click.echo(f"imported modelset ({len(modelset)} model(s)) to _default")
        return
    ts = _load_ts(scope)
    ts.models = dict(modelset)
    _save_ts(ts)
    click.echo(f"imported modelset ({len(modelset)} model(s)) to '{scope}'")


# --------------------------------------------------------------------------
# lifecycle helpers
# --------------------------------------------------------------------------


def _run_lifecycle(path: str, func: str) -> None:
    if not _trick_exists(path):
        raise click.ClickException(f"Trick file not found: {path}")
    try:
        cls = load_trick_from_path(path)
    except Exception as e:
        raise click.ClickException(f"Could not load trick {path}: {e}")
    trick = cls()
    method = getattr(trick, func, None)
    if method is None:
        raise click.ClickException(f"Trick {path} has no hook '{func}'")
    method()
    click.echo(f"Ran {func}() on {path}")


def _install_agent_manager():
    agents_dir = PKG_ROOT / "agents"
    from petsitter.agent_manager import AgentManager
    return AgentManager(config_dir=str(config_dir()), agents_dir=str(agents_dir))


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------


def _rewrite_import_args(argv: list[str]) -> list[str]:
    """Translate ``pet --import <obj> [scope]`` into ``pet _import <obj> [scope]``.

    ``--import`` is not a real click option (a subcommand can't follow one), so
    the tokens are rewritten into the hidden ``_import`` command before click
    parses anything.  Both ``--import model`` and ``--import=model`` forms work;
    other tokens pass through untouched so ``-c <path>`` still applies.
    """
    out: list[str] = []
    i = 0
    n = len(argv)
    while i < n:
        a = argv[i]
        if a == "--import" or a.startswith("--import="):
            obj = a.split("=", 1)[1] if "=" in a else (
                argv[i + 1] if i + 1 < n else ""
            )
            i += 1 if "=" in a else 2
            if i < n and not argv[i].startswith("-"):
                scope = argv[i]
                i += 1
            else:
                scope = "_default"
            out += ["_import", obj, scope]
        else:
            out.append(a)
            i += 1
    return out


class _PetGroup(click.Group):
    """Group that lists commands in logical sections."""

    COMMAND_SECTIONS: list[tuple[str, list[str]]] = [
        ("Tricksets", ["ts", "new", "examples", "rename", "delete"]),
        ("Tricks", ["tricks", "add", "rm", "reorder", "filter"]),
        ("Models", ["model"]),
        ("Lifecycle", ["install", "uninstall", "lifecycle"]),
        ("Logging", ["logfile", "loglevel"]),
        ("System", ["status", "agents"]),
    ]

    EXAMPLES = (
        "pet ts                            # list all tricksets",
        "pet ts opencode                   # full detail for one trickset",
        "pet new mykit -t json_mode -t kennel",
        "pet add mykit kennel              # runs the trick's install hook",
        "pet ts mykit kennel enable false  # activate/deactivate a trick",
        "pet ts mykit kennel keyword go    # set a prompt keyword override",
        "pet ts mykit kennel config '{\"k\": 1}'   # set per-trick config",
        "pet model default url http://localhost:11434",
        "pet model _default > models.json      # back up the whole modelset",
        "cat models.json | pet --import model  # restore it",
        "pet -c another_petsitter_config.conf.json ts",
    )

    def main(self, args=None, **kwargs):
        if args is None:
            args = sys.argv[1:]
        args = _rewrite_import_args(args)
        return super().main(args=args, **kwargs)

    def format_commands(self, ctx, formatter) -> None:
        rows: dict[str, list[tuple[str, str]]] = {title: [] for title, _ in self.COMMAND_SECTIONS}
        for name in self.list_commands(ctx):
            cmd = self.get_command(ctx, name)
            if cmd is None:
                continue
            section = next((t for t, members in self.COMMAND_SECTIONS if name in members), None)
            if section is None:
                continue
            rows[section].append((name, cmd.get_short_help_str(formatter.width - 6)))
        for title, section_rows in rows.items():
            if not section_rows:
                continue
            with formatter.section(title):
                formatter.write_dl(section_rows)

    def format_help_text(self, ctx, formatter) -> None:
        formatter.write_paragraph()
        formatter.write_text(self.help)
        formatter.write_paragraph()
        formatter.write_heading("Examples")
        with formatter.indentation():
            pad = " " * formatter.current_indent
            for line in self.EXAMPLES:
                formatter.write(pad + line + "\n")


@click.group(cls=_PetGroup)
@click.version_option(_version(), prog_name="pet")
@click.option(
    "-c", "--config",
    "config_arg",
    default=None,
    help="Path to a config file (e.g., another_petsitter_config.conf.json) or "
         "config directory (default: $PET_CONFIG_DIR or ~/.config/petsitter). "
         "Must come before the subcommand.",
)
def cli(config_arg: str | None) -> None:
    """Manage petsitter tricksets, tricks, and models from the command line.
    """
    global _override_config_dir, _override_config_path
    if config_arg:
        p = Path(config_arg).expanduser().resolve()
        if p.suffix:
            _override_config_dir = p.parent
            _override_config_path = p
        else:
            _override_config_dir = p
            _override_config_path = p / "config.json"


@cli.command("ts")
@click.argument("name", required=False)
@click.argument("trick", required=False)
@click.argument("param", required=False)
@click.argument("value", required=False)
@click.option("--clear", is_flag=True, help="Clear trickset parameters (with 'ts <name> param')")
def ts_cmd(name: str | None, trick: str | None, param: str | None, value: str | None,
           clear: bool) -> None:
    """View or set tricksets and their tricks.

    \b
      pet ts                            # list tricksets (JSON)
      pet ts <name>                     # show a trickset (JSON)
      pet ts <name> <trick>             # show a trick (JSON)
      pet ts <name> <trick> <param>     # show a param value
      pet ts <name> <trick> <param> <value>  # set a param value

    \b
    Trick params: enable (true/false), keyword, config (JSON object).
    Trickset parameters use the reserved trick name 'param':
      pet ts <name> param [<key> [<value>]]   # show/set/clear parameters
    """
    if name is None:
        data = {}
        for f in _list_ts_files():
            t = Trickset.load_from_file(str(f))
            data[t.name] = t.to_dict()
        click.echo(json.dumps(data, indent=2))
        return

    ts = _load_ts(name)

    if trick is None:
        click.echo(json.dumps(ts.to_dict(), indent=2))
        return

    if trick == "param":
        if clear:
            ts.parameters = {}
            _save_ts(ts)
            click.echo(f"parameters for '{name}': {{}}")
            return
        if param is None:
            click.echo(json.dumps(ts.parameters, indent=2) if ts.parameters else "{ }")
            return
        if value is None:
            if param not in ts.parameters:
                click.echo("(unset)")
                return
            click.echo(_val_str(ts.parameters[param]))
            return
        ts.parameters[param] = _parse_value(value)
        _save_ts(ts)
        click.echo(f"parameters for '{name}': {json.dumps(ts.parameters)}")
        return

    idx = _find_trick_index(ts, trick)
    if idx is None:
        raise click.ClickException(f"trick '{trick}' not found in '{name}'")
    tid = ts.trick_ids[idx] if idx < len(ts.trick_ids) else ""
    entries = ts._trick_entries()
    entry = entries[idx] if idx < len(entries) else {}

    if param is None:
        click.echo(json.dumps(entry, indent=2))
        return

    if param not in TRICK_PARAMS:
        raise click.UsageError(f"unknown param '{param}' (expected one of {', '.join(TRICK_PARAMS)})")

    if value is None:
        if param == "enable":
            val = ts.trick_enabled[idx] if idx < len(ts.trick_enabled) else True
            click.echo("true" if val else "false")
        elif param == "keyword":
            kw = ts.trick_keywords[idx] if idx < len(ts.trick_keywords) else None
            click.echo(kw if kw else "(unset)")
        else:
            cfg = ts.trick_configs.get(tid, {})
            click.echo(json.dumps(cfg, indent=2) if cfg else "(unset)")
        return

    if param == "enable":
        parsed = _parse_value(value)
        if not isinstance(parsed, bool):
            raise click.UsageError("enable expects true or false")
        while len(ts.trick_enabled) <= idx:
            ts.trick_enabled.append(True)
        ts.trick_enabled[idx] = parsed
        _save_ts(ts)
        state = "enabled" if parsed else "disabled"
        click.echo(f"{_trick_label(ts, idx)}: {state} in '{name}'")
        return

    if param == "keyword":
        kw = value.strip() if value and value.strip() else None
        while len(ts.trick_keywords) <= idx:
            ts.trick_keywords.append(None)
        ts.trick_keywords[idx] = kw
        _save_ts(ts)
        if kw:
            click.echo(f"keyword for {_trick_label(ts, idx)} set to '{kw}'")
        else:
            click.echo(f"keyword override for {_trick_label(ts, idx)} cleared")
        return

    parsed = _parse_value(value)
    if not isinstance(parsed, dict):
        raise click.UsageError("config expects a JSON object, e.g. '{\"mcp_path\": \"/tmp/x\"}'")
    ts.trick_configs[tid] = parsed
    t = ts.tricks[idx] if idx < len(ts.tricks) else None
    if t is not None:
        try:
            t.configure(parsed)
        except Exception as e:
            click.echo(f"Warning: configure() failed: {e}", err=True)
    _save_ts(ts)
    click.echo(f"config for {_trick_label(ts, idx)} saved: {json.dumps(parsed)}")


@cli.command("tricks")
@click.option("--json", "json_out", is_flag=True, help="Emit raw JSON")
def tricks_cmd(json_out: bool) -> None:
    """List available trick modules."""
    _print_available_tricks(_available_tricks(), json_out=json_out)


@cli.command("new")
@click.argument("name")
@click.option("--x-title", default="*", show_default=True, help="X-Title filter glob")
@click.option("--model", "model_filter", default="*", show_default=True, help="Model filter glob")
@click.option("-t", "--trick", "tricks", multiple=True, help="Trick path/name to seed (repeatable)")
def new_cmd(name: str, x_title: str, model_filter: str, tricks: tuple[str, ...]) -> None:
    """Create a new trickset."""
    if _ts_path(name).exists():
        raise click.ClickException(f"Trickset '{name}' already exists")
    ts = Trickset(name, SCHEMA, {"X-Title": x_title, "Model": model_filter}, [], file_path=str(_ts_path(name)))
    for t in tricks:
        resolved = _resolve_trick_spec(t)
        if not _trick_exists(resolved):
            raise click.ClickException(f"Trick file not found: {t}")
        ts.add_trick(resolved)
    _save_ts(ts)
    click.echo(f"Created trickset '{name}' -> {ts.file_path}")


@cli.command("delete")
@click.argument("name")
def delete_cmd(name: str) -> None:
    """Delete a trickset file."""
    if name == "_default":
        raise click.ClickException("Cannot delete the _default trickset")
    p = _ts_path(name)
    if not p.exists():
        raise click.ClickException(f"Trickset '{name}' not found")
    p.unlink()
    click.echo(f"Deleted trickset '{name}'")


@cli.command("rename")
@click.argument("old")
@click.argument("new")
def rename_cmd(old: str, new: str) -> None:
    """Rename a trickset (moves its JSON file)."""
    ts = _load_ts(old)
    if _ts_path(new).exists():
        raise click.ClickException(f"Trickset '{new}' already exists")
    old_path = Path(ts.file_path) if ts.file_path else _ts_path(old)
    new_path = _ts_path(new)
    ts.name = new
    old_path.rename(new_path)
    ts.file_path = str(new_path)
    _save_ts(ts)
    click.echo(f"Renamed '{old}' -> '{new}'")


@cli.command("add")
@click.argument("name")
@click.argument("trick")
@click.option("--disable", is_flag=True, help="Add the trick disabled")
@click.option("--keyword", "keyword", default=None, help="Override prompt keyword")
@click.option("--no-install", is_flag=True, help="Skip the trick's install() hook")
def add_cmd(name: str, trick: str, disable: bool, keyword: str | None, no_install: bool) -> None:
    """Add a trick to a trickset. Runs its install() hook like the dashboard."""
    ts = _load_ts(name)
    resolved = _resolve_trick_spec(trick)
    if not _trick_exists(resolved):
        raise click.ClickException(f"Trick file not found: {trick}")
    t = ts.add_trick(resolved, enabled=not disable, keyword=keyword)
    if not no_install:
        try:
            t.install()
        except Exception as e:
            click.echo(f"Warning: install() failed for {resolved}: {e}", err=True)
    _save_ts(ts)
    click.echo(f"Added {type(t).__name__} ({resolved}) to '{name}'")


@cli.command("rm")
@click.argument("name")
@click.argument("trick")
def rm_cmd(name: str, trick: str) -> None:
    """Remove a trick from a trickset. Runs its uninstall() hook."""
    ts = _load_ts(name)
    idx = _find_trick_index(ts, trick)
    if idx is None:
        raise click.ClickException(f"Trick '{trick}' not found in '{name}'")
    tid = ts.trick_ids[idx]
    if idx < len(ts.tricks) and ts.tricks[idx] is not None:
        try:
            ts.tricks[idx].uninstall()
        except Exception as e:
            click.echo(f"Warning: uninstall() failed for {trick}: {e}", err=True)
    ts.remove_trick(tid)
    _save_ts(ts)
    click.echo(f"Removed {trick} from '{name}'")


@cli.command("reorder")
@click.argument("name")
@click.argument("trick")
@click.argument("index", type=int)
def reorder_cmd(name: str, trick: str, index: int) -> None:
    """Move a trick to a new position (0-based) in the execution order."""
    ts = _load_ts(name)
    idx = _find_trick_index(ts, trick)
    if idx is None:
        raise click.ClickException(f"Trick '{trick}' not found in '{name}'")
    tid = ts.trick_ids[idx]
    if not ts.reorder_trick(tid, index):
        raise click.ClickException(f"Could not reorder '{trick}'")
    _save_ts(ts)
    click.echo(f"Moved {trick} to position {index} in '{name}'")


@cli.command("filter")
@click.argument("name")
@click.option("--x-title", default=None, help="X-Title filter glob")
@click.option("--model", "model_filter", default=None, help="Model filter glob")
def filter_cmd(name: str, x_title: str | None, model_filter: str | None) -> None:
    """Set a trickset's routing filters (globs, '*' matches everything)."""
    ts = _load_ts(name)
    if x_title is not None:
        ts.filters["X-Title"] = x_title
    if model_filter is not None:
        ts.filters["Model"] = model_filter
    if x_title is None and model_filter is None:
        raise click.UsageError("Pass --x-title and/or --model")
    _save_ts(ts)
    click.echo(f"filters for '{name}': {'  '.join(f'{k}={v}' for k, v in ts.filters.items())}")


@cli.command("install")
@click.argument("trick")
@click.option("--trickset", "ts_name", default=None,
              help="Also add the installed trick to this trickset")
@click.option("--force", is_flag=True, help="Re-download even if already installed")
def install_cmd(trick: str, ts_name: str | None, force: bool) -> None:
    """Install a trick from the community index, or run a local trick's install() hook.

    \b
      pet install dana/ollama-ctx        # fetch from the index
      pet install dana/ollama-ctx --trickset opencode
      pet install swapharness            # run the local trick's install() hook

    A name containing a slash is a package; a bare name is a local trick.
    """
    if "/" not in trick or trick.endswith(".py"):
        _run_lifecycle(_resolve_trick_spec(trick), "install")
        return

    name, _, want_version = trick.partition("@")
    entry = _resolve_entry(name, want_version or None)
    try:
        path, fresh = registry.install(entry, config_dir(), force=force)
    except registry.RegistryError as e:
        raise click.ClickException(str(e))

    spec = registry.pkg_spec(entry["name"], entry["version"])
    verb = "installed" if fresh else "already installed"
    click.echo(f"{verb} {entry['name']}@{entry['version']}")
    click.echo(f"  {path}")
    if entry.get("brief"):
        click.echo(f"  {entry['brief']}")

    if ts_name:
        ts = _load_ts(ts_name)
        if spec in ts.trick_paths:
            click.echo(f"  already in trickset '{ts_name}'")
        else:
            ts.add_trick(spec)
            _save_ts(ts)
            click.echo(f"  added to trickset '{ts_name}'")
        if fresh:
            _run_lifecycle(spec, "install")
    else:
        click.echo(f"\nAdd it to a trickset with:\n  pet add <trickset> {spec}")


@cli.command("uninstall")
@click.argument("trick")
@click.option("--version", default=None, help="Remove only this version")
def uninstall_cmd(trick: str, version: str | None) -> None:
    """Remove an installed package, or run a local trick's uninstall() hook."""
    if "/" not in trick or trick.endswith(".py"):
        _run_lifecycle(_resolve_trick_spec(trick), "uninstall")
        return

    name, _, at_version = trick.partition("@")
    version = version or at_version or None
    try:
        removed = registry.uninstall(config_dir(), name, version)
    except registry.RegistryError as e:
        raise click.ClickException(str(e))
    if not removed:
        raise click.ClickException(f"{name} is not installed")
    for p in removed:
        click.echo(f"removed {p}")

    still = [f.name for f in _list_ts_files()
             if any(registry.parse_pkg_spec(t) and
                    registry.parse_pkg_spec(t)[0] == name
                    for t in Trickset.load_from_file(str(f)).trick_paths)]
    if still:
        click.echo(f"\nStill referenced by: {', '.join(still)}")


@cli.command("search")
@click.argument("query", required=False, default="")
@click.option("--all", "show_all", is_flag=True, help="Include unfeatured tricks")
@click.option("--refresh", is_flag=True, help="Bypass the cached index")
@click.option("--json", "json_out", is_flag=True, help="Emit raw JSON")
def search_cmd(query: str, show_all: bool, refresh: bool, json_out: bool) -> None:
    """Search the community trick index."""
    index = _fetch_index(refresh)
    results = registry.search(index, query, featured_only=not show_all and not query)

    if json_out:
        click.echo(json.dumps(results, indent=2))
        return
    if not results:
        click.echo(f"nothing matching '{query}'" if query else "the index is empty")
        return

    installed = {i["name"]: i["version"] for i in registry.list_installed(config_dir())}
    for e in results:
        marks = []
        if e.get("featured"):
            marks.append("★")
        if e["name"] in installed:
            marks.append("installed" if installed[e["name"]] == e["version"] else "update")
        tag = ("  [" + " ".join(marks) + "]") if marks else ""
        click.echo(f"{e['name']}@{e['version']}{tag}")
        if e.get("brief"):
            click.echo(f"    {e['brief']}")
        click.echo(f"    ⭐ {e.get('stars', 0)}  {e.get('repo', '')}")

    if not show_all and not query:
        click.echo(f"\n{len(results)} featured · pet search --all for everything")


@cli.command("cat")
@click.argument("name")
@click.option("--refresh", is_flag=True, help="Bypass the cached index")
def cat_cmd(name: str, refresh: bool) -> None:
    """Print a trick's source without installing it.

    Tricks are one file and usually short. Reading one before you run it is
    cheap in a way that reading a dependency tree is not.
    """
    pkg, _, want_version = name.partition("@")
    installed = registry.installed_path(config_dir(), pkg, want_version) if want_version else None
    if installed and installed.exists():
        click.echo(installed.read_text())
        return
    entry = _resolve_entry(pkg, want_version or None, refresh=refresh)
    try:
        click.echo(registry._fetch(entry["url"], timeout=30).decode("utf-8", "replace"))
    except registry.RegistryError as e:
        raise click.ClickException(str(e))


@cli.command("installed")
@click.option("--json", "json_out", is_flag=True, help="Emit raw JSON")
def installed_cmd(json_out: bool) -> None:
    """List tricks installed from the index."""
    items = registry.list_installed(config_dir())
    if json_out:
        click.echo(json.dumps(items, indent=2))
        return
    if not items:
        click.echo("nothing installed from the index yet. Try 'pet search'.")
        return
    for i in items:
        click.echo(f"{i['spec']}")
        if i.get("brief"):
            click.echo(f"    {i['brief']}")
        click.echo(f"    {i['path']}")


@cli.command("publish")
@click.argument("trick")
@click.option("--repo", default=None,
              help="Existing GitHub repo to push to (default: create one)")
@click.option("--dry-run", is_flag=True, help="Show what would happen and stop")
def publish_cmd(trick: str, repo: str | None, dry_run: bool) -> None:
    """Publish a trick to the community index.

    \b
    Publishing is: push a public repo, add the 'petsitter-trick' topic. The
    hourly crawler does the rest. There is no server, no account, and nothing
    to approve. This command just runs the git and gh steps for you.
    """
    import shutil
    import subprocess

    path = Path(_resolve_trick_spec(trick))
    if not path.exists():
        raise click.ClickException(f"no such file: {path}")

    meta = _introspect(path)
    version = _trick_attr(path, "__version__")
    problems = []
    if not version:
        problems.append(
            "no __version__ on the Trick subclass. Add e.g. __version__ = \"0.1.0\""
        )
    if not meta.get("brief"):
        problems.append("no __brief__. The index shows it as the one-line description.")
    if problems:
        for p in problems:
            click.echo(f"  ✗ {p}")
        if not meta.get("brief") and version:
            click.confirm("Publish without a brief?", abort=True)
        else:
            raise click.ClickException("cannot publish yet")

    owner_slug = registry.slugify_stem(path.stem)
    click.echo(f"  file     {path}")
    click.echo(f"  version  {version}")
    click.echo(f"  brief    {meta.get('brief') or '(none)'}")
    click.echo(f"  name     <your-github-login>/{owner_slug}")

    if not shutil.which("gh"):
        click.echo(
            "\ngh is not installed, so do it by hand:\n"
            "  1. push this file to a public GitHub repo\n"
            "  2. gh repo edit --add-topic petsitter-trick\n"
            "It'll be in the index within the hour."
        )
        return

    if dry_run:
        click.echo("\n(dry run, stopping here)")
        return

    target = repo or click.prompt("GitHub repo (owner/name)",
                                  default=f"petsitter-{owner_slug}")

    def run(*args: str) -> subprocess.CompletedProcess:
        click.echo(f"  $ {' '.join(args)}")
        return subprocess.run(args, capture_output=True, text=True)

    exists = run("gh", "repo", "view", target).returncode == 0
    if not exists:
        r = run("gh", "repo", "create", target, "--public",
                "--description", meta.get("brief") or "A petsitter trick")
        if r.returncode != 0:
            raise click.ClickException(r.stderr.strip() or "gh repo create failed")

    r = run("gh", "repo", "edit", target, "--add-topic", "petsitter-trick")
    if r.returncode != 0:
        raise click.ClickException(r.stderr.strip() or "could not add the topic")

    click.echo(
        f"\n✓ {target} is tagged petsitter-trick\n"
        f"  Push {path.name} to it if you haven't, then it lands in the index\n"
        f"  within the hour as <your-login>/{owner_slug}."
    )


def _fetch_index(refresh: bool = False) -> dict:
    try:
        return registry.fetch_index(config_dir(), load_config(), force=refresh)
    except registry.RegistryError as e:
        raise click.ClickException(str(e))


def _resolve_entry(name: str, version: str | None, refresh: bool = False) -> dict:
    index = _fetch_index(refresh)
    try:
        return registry.resolve(index, name, version)
    except registry.RegistryError as e:
        raise click.ClickException(str(e))


def _trick_attr(path: Path, attr: str) -> str | None:
    """Read one class attribute out of a trick file without importing it."""
    import ast
    try:
        tree = ast.parse(path.read_text())
    except (SyntaxError, OSError):
        return None
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.Assign):
                for t in stmt.targets:
                    if isinstance(t, ast.Name) and t.id == attr:
                        try:
                            v = ast.literal_eval(stmt.value)
                        except (ValueError, SyntaxError):
                            return None
                        return v if isinstance(v, str) else None
    return None


@cli.command("lifecycle")
@click.argument("trick")
@click.argument("hook", type=click.Choice(["install", "startup", "shutdown", "uninstall"]))
def lifecycle_cmd(trick: str, hook: str) -> None:
    """Run an arbitrary lifecycle hook on a trick."""
    _run_lifecycle(_resolve_trick_spec(trick), hook)


@cli.command("model")
@click.argument("name", required=False)
@click.argument("param", required=False)
@click.argument("value", required=False)
@click.option("--trickset", "ts_name", default="_default", show_default=True,
              help="Scope: a trickset name, or _default for the global config.json modelset")
@click.option("--remove", is_flag=True, help="Remove this model entry")
def model_cmd(name: str | None, param: str | None, value: str | None,
              ts_name: str, remove: bool) -> None:
    """View or set model config.

    \b
      pet model                            # show all models (JSON)
      pet model _default                   # same, as a dump for backup/restore
      pet model default                    # show the 'default' entry (JSON)
      pet model default url                # show just the url value
      pet model default url http://...     # set the url value

    \b
    Params: url, model, key. Pass the value 'false' for passthrough
    (e.g. 'pet model default model false'). Scope to a trickset with
    --trickset; the 'default' entry also drives the top-level
    model_url/model_name/api_key in config.json.
    """
    if remove:
        if not name:
            raise click.UsageError("--remove requires a model name")
        _model_remove(name, ts_name)
        return

    if value is not None:
        if not name or not param:
            raise click.UsageError("set requires: pet model <name> <param> <value>")
        if param not in MODEL_PARAMS:
            raise click.UsageError(f"unknown param '{param}' (expected one of {', '.join(MODEL_PARAMS)})")
        _model_set(name, param, _parse_value(value), ts_name)
        return

    if name == "_default" and param is None:
        click.echo(json.dumps(_global_models(), indent=2))
        return

    global_models = _global_models()
    scoped: dict = {}
    if ts_name != "_default":
        ts = _load_ts(ts_name)
        scoped = {k: v for k, v in ts.models.items() if isinstance(v, dict)}

    effective = dict(global_models)
    effective.update(scoped)

    if name is None:
        click.echo(json.dumps(effective, indent=2))
        return

    entry = effective.get(name)
    if entry is None:
        raise click.ClickException(f"model '{name}' not found (use 'pet model' to list)")
    if param is None:
        click.echo(json.dumps(entry, indent=2))
        return
    if param not in MODEL_PARAMS:
        raise click.UsageError(f"unknown param '{param}' (expected one of {', '.join(MODEL_PARAMS)})")
    if param not in entry:
        click.echo("(unset)")
        return
    click.echo(_val_str(entry[param]))


@cli.command("_import", hidden=True)
@click.argument("obj")
@click.argument("scope", required=False, default="_default")
def import_cmd(obj: str, scope: str) -> None:
    """Import an object from stdin JSON.

    Backing command for ``pet --import`` (the group rewrites it here):
    ``cat models.json | pet --import model [scope]`` replaces that scope's
    whole modelset — the inverse of ``pet model _default``.
    """
    if not obj:
        raise click.UsageError("--import needs an object name, e.g. 'pet --import model'")
    if obj != "model":
        raise click.ClickException(f"unknown --import object '{obj}' (supported: model)")
    raw = click.get_text_stream("stdin").read()
    if not raw.strip():
        raise click.ClickException(
            "nothing on stdin — pipe model JSON into 'pet --import model', "
            "e.g. 'pet model _default > x.json' then 'cat x.json | pet --import model'"
        )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise click.ClickException(f"stdin is not valid JSON: {e}")
    _import_modelset(_parse_modelset(data), scope)


@cli.command("logfile")
@click.argument("name")
@click.argument("path", required=False)
def logfile_cmd(name: str, path: str | None) -> None:
    """Show or set a trickset's log file path."""
    ts = _load_ts(name)
    if path is None:
        click.echo(ts.logfile)
        return
    ts.logfile = str(Path(path).expanduser())
    _save_ts(ts)
    click.echo(f"logfile for '{name}': {ts.logfile}")


@cli.command("loglevel")
@click.argument("name")
@click.argument("level", required=False, type=click.Choice(LOG_LEVELS))
def loglevel_cmd(name: str, level: str | None) -> None:
    """Show or set a trickset's log level (DEBUG/INFO/WARNING/ERROR)."""
    ts = _load_ts(name)
    if level is None:
        click.echo(ts.loglevel)
        return
    ts.loglevel = level.upper()
    _save_ts(ts)
    click.echo(f"loglevel for '{name}': {ts.loglevel}")


@cli.command("examples")
@click.option("--force", is_flag=True, help="Overwrite existing examples (backs up first)")
def examples_cmd(force: bool) -> None:
    """Install the example tricksets into the config dir."""
    from petsitter import server
    # server resolves its config dir in cli(), which never runs when pet
    # imports it directly. Without this, examples land in the default
    # ~/.config/petsitter no matter what -c or $PET_CONFIG_DIR say.
    server.CONFIG_DIR = config_dir()
    server.CONFIG_PATH = config_path()
    server.TRICKSETS_DIR = tricksets_dir()
    server.BACKUPS_DIR = config_dir() / "backups"
    results = server.install_examples(force=force)
    if not results:
        click.echo("No example tricksets found.")
        return
    for r in results:
        if r["result"]:
            click.echo(f"  installed {r['name']}")
        else:
            click.echo(f"  skipped {r['name']} ({r.get('errmsg', '')})")


@cli.group()
def agents() -> None:
    """Manage harness agents (route tools through petsitter)."""


@agents.command("list")
def agents_list() -> None:
    """List harness agents and their registration state."""
    mgr = _install_agent_manager()
    registered = mgr.get_registered().get("agents", {})
    agents_data = mgr.get_agents()
    if not agents_data:
        click.echo("No agent harnesses found.")
        return
    for agent_id, a in sorted(agents_data.items()):
        d = a.get("detect", {})
        status = d.get("status", "?")
        reg = "registered" if registered.get(agent_id, {}).get("status") == "registered" else "not registered"
        click.echo(f"{agent_id}  [{status} / {reg}]  {a.get('display_name', '')}")
        click.echo(f"    {a.get('description', '')}")
        for p in a.get("config_paths", []):
            click.echo(f"    config: {p}")


@agents.command("register")
@click.argument("agent_id")
def agents_register(agent_id: str) -> None:
    """Register an agent: create its trickset and patch its config."""
    mgr = _install_agent_manager()
    try:
        success, log = mgr.register(agent_id)
    except KeyError as e:
        raise click.ClickException(str(e))
    for entry in log:
        lvl = entry.get("level", "INFO")
        click.echo(f"[{lvl}] {entry.get('message', '')}")
    if not success:
        raise click.ClickException(f"Registration of '{agent_id}' failed")
    click.echo(f"Registered agent '{agent_id}'")


@agents.command("unregister")
@click.argument("agent_id")
def agents_unregister(agent_id: str) -> None:
    """Unregister an agent and restore its original config."""
    mgr = _install_agent_manager()
    try:
        success, log = mgr.unregister(agent_id)
    except KeyError as e:
        raise click.ClickException(str(e))
    for entry in log:
        lvl = entry.get("level", "INFO")
        click.echo(f"[{lvl}] {entry.get('message', '')}")
    click.echo(f"Unregistered agent '{agent_id}'")


@agents.command("restore")
def agents_restore() -> None:
    """Put every registered agent's config back, without a running proxy.

    The server restores agents on its way out (SIGINT, SIGTERM, SIGHUP, or a
    clean exit), but a SIGKILL, an OOM kill, or a crash skips all of that and
    leaves a tool like Claude Code pointed at a proxy that is no longer
    listening. This reads the same registry.json the server would and undoes
    every "registered" entry by hand -- safe to run any time, whether or not
    petsitter is running, and a no-op if everything is already restored.
    """
    mgr = _install_agent_manager()
    log = mgr.unregister_all()
    if not log:
        click.echo("Nothing to restore.")
        return
    for entry in log:
        lvl = entry.get("level", "INFO")
        click.echo(f"[{lvl}] {entry.get('message', '')}")


@cli.command("status")
def status_cmd() -> None:
    """Show a summary of the current configuration."""
    cfg = load_config()
    url = cfg.get("model_url", "")
    model = cfg.get("model_name", "")
    click.echo("petsitter status")
    click.echo(f"  config dir: {config_dir()}")
    click.echo(f"  model:      {url or '(not set)'}" + (f"  ({model})" if model else ""))
    files = _list_ts_files()
    click.echo(f"  tricksets:  {len(files)}")
    for f in files:
        ts = Trickset.load_from_file(str(f))
        enabled = sum(1 for i in range(len(ts.trick_paths))
                      if i < len(ts.trick_enabled) and ts.trick_enabled[i])
        click.echo(f"    {ts.name}  ({enabled}/{len(ts.trick_paths)} tricks enabled)")


if __name__ == "__main__":
    cli()

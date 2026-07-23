"""HTTP server and CLI for petsitter."""

import asyncio
import atexit
import json
import logging
import os
import shutil
import subprocess
from collections import deque
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path
from typing import Any

import click
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from src.agent_manager import AgentManager
from src.gui_routes import register_gui_routes
from src.loader import load_trick_from_path, load_tricks
from src.proxy import ProxyHandler
from src.trick import (
    configure_modelset,
)
from src.trickset import Trickset, _new_id


class LogCaptureHandler(logging.Handler):
    def __init__(self, maxlen: int = 500):
        super().__init__()
        self.logs = deque(maxlen=maxlen)
        self._sse_clients: list[asyncio.Queue] = []

    def emit(self, record: logging.LogRecord) -> None:
        entry = {
            "time": datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
            "level": record.levelname,
            "message": self.format(record),
            "name": record.name,
        }
        self.logs.append(entry)
        for q in self._sse_clients:
            q.put_nowait(entry)

    def get_logs(self, level: str | None = None, limit: int = 100) -> list[dict]:
        logs = list(self.logs)
        if level:
            logs = [l for l in logs if l["level"] == level.upper()]
        return logs[-limit:]

    def add_sse_client(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._sse_clients.append(q)
        return q

    def remove_sse_client(self, q: asyncio.Queue) -> None:
        if q in self._sse_clients:
            self._sse_clients.remove(q)


_log_capture: LogCaptureHandler | None = None
_agent_manager: AgentManager | None = None

CONFIG_DIR = Path.home() / ".config" / "petsitter"
CONFIG_PATH = CONFIG_DIR / "config.json"
TRICKSETS_DIR = CONFIG_DIR / "tricksets"
BACKUPS_DIR = CONFIG_DIR / "backups"
_SOURCE_TRICKSETS = Path(__file__).resolve().parent.parent / "tricksets"


def _resolve_trick_path(name: str) -> str:
    """Resolve a trick name to a .py file path.

    ``swapharness`` → ``tricks/swapharness.py``
    ``tricks/json_mode.py`` → stays as-is.
    """
    if name.endswith(".py"):
        return name
    if "/" not in name:
        candidate = f"tricks/{name}.py"
        if Path(candidate).exists():
            return candidate
    return name


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_config(config: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n")


def install_examples(force: bool = False) -> list[dict]:
    """Copy example tricksets from package source to CONFIG_DIR/tricksets/.

    When *force* is ``False``, existing files are left untouched and the
    result entry for that file contains ``"result": False`` with an
    ``"errmsg"``.  When *force* is ``True``, the current file is backed up
    to ``CONFIG_DIR/backups/{stem}-{timestamp}.json`` before overwriting.

    Returns a list of dicts, one per source file::

        {"name": "gemma4", "result": True}
        {"name": "opencode", "result": False, "errmsg": "opencode.json already exists"}
    """
    results: list[dict] = []
    if not _SOURCE_TRICKSETS.exists():
        return results
    TRICKSETS_DIR.mkdir(parents=True, exist_ok=True)
    for f in sorted(_SOURCE_TRICKSETS.glob("*.json")):
        dest = TRICKSETS_DIR / f.name
        if dest.exists():
            if not force:
                results.append({"name": f.stem, "result": False, "errmsg": f"{f.name} already exists"})
                continue
            BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = BACKUPS_DIR / f"{f.stem}-{stamp}.json"
            shutil.copy2(str(dest), str(backup))
        shutil.copy2(str(f), str(dest))
        results.append({"name": f.stem, "result": True})
    return results


def create_app(
    model_url: str,
    model_name: str | None,
    api_key: str,
    trick_paths: list[str],
    trickset_paths: list[str] | None = None,
    modelset_data: dict[str, str] | None = None,
    config_path: str | None = None,
) -> Starlette:
    tricksets: dict[str, Trickset] = {}

    if trickset_paths:
        for tp in trickset_paths:
            ts = Trickset.load_from_file(tp)
            tricksets[ts.name] = ts

    if trick_paths:
        if "_default" in tricksets:
            existing = tricksets["_default"]
            for tp in trick_paths:
                existing.add_trick(tp)
        else:
            default_ts = Trickset("_default", "0.3.0", {"X-Title": "*", "Model": "*"}, list(trick_paths))
            default_ts.load_tricks()
            tricksets["_default"] = default_ts

    handler = ProxyHandler(model_url, model_name, api_key, tricksets=tricksets)

    global _log_capture, _agent_manager
    _agent_manager = AgentManager(config_dir=str(CONFIG_DIR))
    log_level = getattr(logging, os.getenv("LOGLEVEL", "INFO").upper(), logging.INFO)
    logging.getLogger().setLevel(log_level)
    _log_capture = LogCaptureHandler()
    _log_capture.setLevel(log_level)
    _log_capture.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logging.getLogger().addHandler(_log_capture)

    # First-run: install example tricksets to user config dir
    cfg = load_config()
    if not cfg.get("first_run"):
        results = install_examples()
        for r in results:
            if r["result"]:
                logging.getLogger("petsitter").info("Installed example trickset: %s", r["name"])
            else:
                logging.getLogger("petsitter").info("Skipped existing trickset: %s (%s)", r["name"], r.get("errmsg", ""))
        cfg["first_run"] = True
        save_config(cfg)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app):
        yield
        handler.shutdown_all()
        if _agent_manager is not None:
            _agent_manager.unregister_all()

    app = Starlette(lifespan=lifespan)

    async def stream_chat_completions(handler: ProxyHandler, payload: dict, x_title: str):
        try:
            result = await handler.chat_completions(payload, x_title=x_title)
            message = result["choices"][0]["message"]
            stream_result = {
                "id": result.get("id", "chatcmpl-petsitter"),
                "object": "chat.completion.chunk",
                "created": result.get("created", __import__("time").time()),
                "model": result.get("model", "unknown"),
                "choices": [{
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "content": message.get("content"),
                    },
                    "finish_reason": result["choices"][0].get("finish_reason", "stop"),
                }],
            }
            if "tool_calls" in message:
                stream_result["choices"][0]["delta"]["tool_calls"] = message["tool_calls"]
            yield f"data: {json.dumps(stream_result)}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            import traceback
            logging.getLogger("petsitter").error(f"Error in stream_chat_completions: {e}\n{traceback.format_exc()}")
            yield f"data: {json.dumps({'error': {'message': str(e), 'type': 'proxy_error'}})}\n\n"

    async def chat_completions(request: Request) -> Response:
        try:
            payload = await request.json()
            stream = payload.get("stream", False)
            x_title = request.headers.get("X-Title", "")
            if stream:
                return StreamingResponse(
                    stream_chat_completions(handler, payload, x_title),
                    media_type="text/event-stream",
                )
            else:
                result = await handler.chat_completions(payload, x_title=x_title)
                return JSONResponse(result)
        except json.JSONDecodeError as e:
            return JSONResponse({"error": "Invalid JSON", "type": "invalid_request"}, status_code=400)
        except ValueError as e:
            return JSONResponse({"error": str(e), "type": "setup_required"}, status_code=503)
        except Exception as e:
            import traceback
            logging.getLogger("petsitter").error(f"Error in chat_completions: {e}\n{traceback.format_exc()}")
            return JSONResponse(
                {"error": str(e), "type": "proxy_error"},
                status_code=500,
            )
    app.add_route("/v1/chat/completions", chat_completions, methods=["POST"])

    async def models(request: Request) -> Response:
        try:
            result = await handler.models()
            return JSONResponse(result)
        except ValueError as e:
            return JSONResponse({"error": str(e), "type": "setup_required"}, status_code=503)
        except Exception as e:
            import traceback
            logging.getLogger("petsitter").error(f"Error in models: {e}\n{traceback.format_exc()}")
            return JSONResponse({"error": str(e), "type": "proxy_error"}, status_code=500)
    app.add_route("/v1/models", models, methods=["GET"])

    async def health(request: Request) -> Response:
        return JSONResponse({"status": "ok"})
    app.add_route("/health", health, methods=["GET"])

    register_gui_routes(app, handler, api_key, config_path=config_path)

    # ----- trickset API endpoints -----
    # Fixed-path routes must come before {name} param route

    async def list_tricksets(request: Request) -> Response:
        result = []
        for ts in handler.tricksets.values():
            info = ts.to_dict()
            info["trick_names"] = [type(t).__name__ for t in ts.tricks]
            result.append(info)
        return JSONResponse(result)
    app.add_route("/api/tricksets", list_tricksets, methods=["GET"])

    async def tricksets_available(request: Request) -> Response:
        files = []
        if TRICKSETS_DIR.exists():
            for f in sorted(TRICKSETS_DIR.glob("*.json")):
                files.append({"path": str(f), "name": f.stem})
        return JSONResponse(files)
    app.add_route("/api/tricksets/available", tricksets_available, methods=["GET"])

    async def tricksets_load(request: Request) -> Response:
        data = await request.json()
        path = data.get("path", "")
        try:
            ts = Trickset.load_from_file(path)
            handler.tricksets[ts.name] = ts
            return JSONResponse({"success": True, "name": ts.name, "trick_count": len(ts.tricks)})
        except Exception as e:
            return JSONResponse({"success": False, "error": str(e)}, status_code=400)
    app.add_route("/api/tricksets/load", tricksets_load, methods=["POST"])

    async def tricksets_unload(request: Request) -> Response:
        data = await request.json()
        name = data.get("name", "")
        if name in handler.tricksets:
            del handler.tricksets[name]
            return JSONResponse({"success": True})
        return JSONResponse({"success": False, "error": f"Trickset '{name}' not found"}, status_code=404)
    app.add_route("/api/tricksets/unload", tricksets_unload, methods=["POST"])

    async def get_trickset(request: Request) -> Response:
        name = request.path_params.get("name")
        ts = handler.tricksets.get(name)
        if not ts:
            return JSONResponse({"error": f"Trickset '{name}' not found"}, status_code=404)
        info = ts.to_dict()
        info["trick_names"] = [type(t).__name__ for t in ts.tricks]
        return JSONResponse(info)
    app.add_route("/api/tricksets/{name}", get_trickset, methods=["GET"])

    async def update_trickset(request: Request) -> Response:
        name = request.path_params.get("name")
        ts = handler.tricksets.get(name)
        if not ts:
            path = TRICKSETS_DIR / f"{name}.json"
            if path.exists():
                ts = Trickset.load_from_file(str(path))
                handler.tricksets[name] = ts
            else:
                return JSONResponse({"error": f"Trickset '{name}' not found"}, status_code=404)
        data = await request.json()
        if "filters" in data:
            ts.filters = data["filters"]
        if "tricks" in data:
            raw = data["tricks"]
            has_ids = all(isinstance(e, dict) and e.get("id") for e in raw)
            if has_ids:
                if ts.merge_tricks(raw):
                    if ts.file_path:
                        ts.save()
            else:
                new_paths: list[str] = []
                new_enabled: list[bool] = []
                new_ids: list[str] = []
                for entry in raw:
                    if isinstance(entry, str):
                        new_paths.append(entry)
                        new_enabled.append(True)
                        new_ids.append(_new_id())
                    elif isinstance(entry, dict):
                        new_paths.append(entry.get("file", ""))
                        new_enabled.append(entry.get("enabled", True))
                        new_ids.append(entry.get("id") or _new_id())
                ts.trick_paths = new_paths
                ts.trick_enabled = new_enabled
                ts.trick_ids = new_ids
                ts.load_tricks()
                if ts.file_path:
                    ts.save()
        if "parameters" in data:
            ts.parameters = dict(data["parameters"])
        if "models" in data:
            ts.models = dict(data["models"])
        if "name" in data and data["name"] != ts.name:
            new_name = data["name"]
            if new_name in handler.tricksets:
                return JSONResponse({"error": f"Trickset '{new_name}' already exists"}, status_code=409)
            old_name = ts.name
            ts.name = new_name
            if ts.file_path:
                old_path = Path(ts.file_path)
                new_path = old_path.parent / f"{new_name}.json"
                old_path.rename(new_path)
                ts.file_path = str(new_path)
            handler.tricksets[new_name] = handler.tricksets.pop(old_name)
        if ts.file_path:
            ts.save()
        return JSONResponse({"success": True})
    app.add_route("/api/tricksets/{name}", update_trickset, methods=["PUT"])

    async def delete_trickset(request: Request) -> Response:
        name = request.path_params.get("name")
        if name == "_default":
            return JSONResponse({"error": "Cannot delete default trickset"}, status_code=400)
        ts = handler.tricksets.get(name)
        if ts and ts.file_path:
            Path(ts.file_path).unlink(missing_ok=True)
        handler.tricksets.pop(name, None)
        return JSONResponse({"success": True})
    app.add_route("/api/tricksets/{name}", delete_trickset, methods=["DELETE"])

    async def install_examples_endpoint(request: Request) -> Response:
        data = await request.json()
        force = data.get("force", False)
        results = install_examples(force=force)
        return JSONResponse({"results": results})
    app.add_route("/api/tricksets/install-examples", install_examples_endpoint, methods=["POST"])

    # ----- agent API endpoints -----

    async def list_agents(request: Request) -> Response:
        if _agent_manager is None:
            return JSONResponse({"error": "Agent manager not initialized"}, status_code=500)
        return JSONResponse(_agent_manager.get_agents())
    app.add_route("/api/agents", list_agents, methods=["GET"])

    async def get_agent_registered(request: Request) -> Response:
        if _agent_manager is None:
            return JSONResponse({"error": "Agent manager not initialized"}, status_code=500)
        return JSONResponse(_agent_manager.get_registered())
    app.add_route("/api/agents/registered", get_agent_registered, methods=["GET"])

    async def register_agent(request: Request) -> Response:
        if _agent_manager is None:
            return JSONResponse({"error": "Agent manager not initialized"}, status_code=500)
        agent_id = request.path_params.get("id")
        try:
            success, log = _agent_manager.register(agent_id)
            status = 200 if success else 400
            return JSONResponse({"success": success, "agent_id": agent_id, "log": log}, status_code=status)
        except KeyError as e:
            return JSONResponse({"success": False, "error": str(e), "log": []}, status_code=404)
        except Exception as e:
            return JSONResponse({"success": False, "error": str(e), "log": []}, status_code=500)
    app.add_route("/api/agents/{id}/register", register_agent, methods=["POST"])

    async def unregister_agent(request: Request) -> Response:
        if _agent_manager is None:
            return JSONResponse({"error": "Agent manager not initialized"}, status_code=500)
        agent_id = request.path_params.get("id")
        try:
            success, log = _agent_manager.unregister(agent_id)
            return JSONResponse({"success": success, "agent_id": agent_id, "log": log})
        except KeyError as e:
            return JSONResponse({"success": False, "error": str(e), "log": []}, status_code=404)
        except Exception as e:
            return JSONResponse({"success": False, "error": str(e), "log": []}, status_code=500)
    app.add_route("/api/agents/{id}/unregister", unregister_agent, methods=["POST"])

    async def shutdown_server(request: Request) -> Response:
        if _agent_manager is not None:
            _agent_manager.unregister_all()
        handler.shutdown_all()
        asyncio.create_task(_delayed_exit(0.5))
        return JSONResponse({"success": True, "message": "Shutting down"})
    app.add_route("/api/shutdown", shutdown_server, methods=["POST"])

    atexit.register(lambda: handler.shutdown_all())

    return app


async def _delayed_exit(delay: float = 0.5) -> None:
    """Exit the process after a short delay so the HTTP response can be sent."""
    await asyncio.sleep(delay)
    import os
    os._exit(0)


def _get_version() -> str:
    try:
        import tomllib
        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        return tomllib.loads(pyproject.read_text()).get("project", {}).get("version", "0.0.0")
    except Exception:
        pass
    try:
        r = subprocess.run(
            ["git", "describe", "--tags", "--dirty", "--always"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    try:
        return _pkg_version("petsitter")
    except PackageNotFoundError:
        return "0.0.0"


class _PetSitterCLI(click.Command):
    """Custom CLI that intercepts --trick/-t with no value to list tricks."""

    def parse_args(self, ctx, args):
        for i, arg in enumerate(args):
            if arg in ("-t", "--trick"):
                if i + 1 >= len(args) or args[i + 1].startswith("-"):
                    _print_trick_table()
                    ctx.exit(0)
        return super().parse_args(ctx, args)


def _print_trick_table():
    """Print a table of available tricks:  Name | File | Description."""
    from src.gui_routes import _introspect_trick_file

    tricks_dir = Path("tricks")
    if not tricks_dir.exists():
        click.echo("No tricks directory found.")
        return

    rows = []
    for f in sorted(tricks_dir.glob("*.py")):
        if f.name == "__init__.py":
            continue
        info = _introspect_trick_file(f)
        rows.append((info["display_name"] or f.stem, str(f), info["brief"] or ""))

    if rows:
        name_w = max(len(r[0]) for r in rows) + 2
        path_w = max(len(r[1]) for r in rows) + 2
        for name, path, brief in rows:
            click.echo(f"{name:<{name_w}}| {path:<{path_w}}| {brief}")


@click.command(cls=_PetSitterCLI)
@click.version_option(
    _get_version(),
    "-v", "--version",
    prog_name="petsitter",
)
@click.option(
    "-u", "--url",
    "model_url",
    default=None,
    help="Base URL of the upstream model (e.g., http://localhost:11434)",
)
@click.option(
    "-m", "--model",
    "model_name",
    default=None,
    help="Model name to use (optional for some backends like vllm, sglang)",
)
@click.option(
    "-k", "--key",
    "api_key",
    default="",
    help="API key for upstream (if required)",
)
@click.option(
    "-t", "--trick",
    "tricks",
    multiple=True,
    help="Path to a trick module, or --trick alone to list available tricks",
)
@click.option(
    "-tc", "--trick-config",
    "tricksets",
    multiple=True,
    help="Path to a trickset JSON file (can be specified multiple times)",
)
@click.option(
    "-mc", "--model-config",
    "model_config",
    default=None,
    help="Path to a model config JSON file (MAS URIs for multi-model tricks)",
)
@click.option(
    "-l", "--listen",
    "listen_on",
    default="localhost:8080",
    help="Host:port to listen on (default: localhost:8080)",
)
def cli(
    model_url: str | None,
    model_name: str | None,
    api_key: str,
    tricks: tuple[str, ...],
    tricksets: tuple[str, ...],
    model_config: str | None,
    listen_on: str,
) -> None:
    """Petsitter - OpenAI-compatible proxy with tricks.

    https://github.com/day50-dev/Petsitter

    Example:

    \b
        petsitter -u http://localhost:11434 \\
                  -m llama3:8b \\
                  -t tricks/tool_call.py \\
                  -tc tricksets/gemma4.json \\
                  -l localhost:8080

    \b
        petsitter -mc modelset-example.json \\
                  -t tricks/kennel.py \\
                  -l localhost:8080
    """
    # Load persistent config
    cfg = load_config()
    cfg_tricks = list(cfg.get("tricks", []))
    cfg_tricksets = list(cfg.get("tricksets", []))
    cfg_modelset = cfg.get("modelset")

    # CLI args override persistent config
    if model_url is None:
        model_url = cfg.get("model_url", "")
    if model_name is None:
        model_name = cfg.get("model_name", "")
    if not api_key:
        api_key = cfg.get("api_key", "")

    if model_config:
        mc_path = Path(model_config).resolve()
        if not mc_path.exists():
            click.echo(f"Error: model config file not found: {model_config}", err=True)
            raise SystemExit(1)
        try:
            modelset_data = json.loads(mc_path.read_text())
        except json.JSONDecodeError as e:
            click.echo(f"Error: invalid JSON in model config file: {e}", err=True)
            raise SystemExit(1)
    else:
        modelset_data = cfg_modelset

    if modelset_data:
        configure_modelset(modelset_data)
        if "default" in modelset_data:
            dflt = modelset_data["default"]
            if isinstance(dflt, dict):
                inferred_url = dflt.get("url", "")
                if inferred_url:
                    model_url = inferred_url
                inferred_model = dflt.get("model")
                if isinstance(inferred_model, str) and inferred_model:
                    model_name = inferred_model

    # Process -t args: trickname:function runs a lifecycle hook,
    # trickname or path loads the trick into the default trickset.
    lifecycle_tricks: list[str] = []
    load_tricks_list: list[str] = []
    for arg in (list(tricks) if tricks else []):
        if ":" in arg and not arg.endswith(".py"):
            name, _, func = arg.partition(":")
            path = _resolve_trick_path(name)
            lifecycle_tricks.append(f"{path}:{func}")
        else:
            load_tricks_list.append(_resolve_trick_path(arg) if "/" not in arg else arg)
    trick_list = load_tricks_list if load_tricks_list else cfg_tricks
    trickset_list = list(tricksets) if tricksets else cfg_tricksets

    # Run lifecycle hooks (install, uninstall, startup, shutdown) on demand
    for entry in lifecycle_tricks:
        path, _, func = entry.partition(":")
        try:
            cls = load_trick_from_path(path)
            trick = cls()
            method = getattr(trick, func, None)
            if method is None:
                click.echo(f"Error: trick {path} has no method '{func}'", err=True)
                continue
            if asyncio.iscoroutinefunction(method):
                asyncio.run(method())
            else:
                method()
            click.echo(f"Ran {func}() on {path}")
        except Exception as e:
            click.echo(f"Error running {func}() on {path}: {e}", err=True)

    if ":" in listen_on:
        host, port_str = listen_on.rsplit(":", 1)
        if not host:
            host = "127.0.0.1"
        port = int(port_str)
    else:
        host = listen_on
        port = 8080

    app = create_app(
        model_url, model_name, api_key,
        trick_paths=trick_list,
        trickset_paths=trickset_list,
        modelset_data=modelset_data,
        config_path=str(CONFIG_PATH),
    )

    # Save config for next run (merging CLI state into persistent config)
    save_config({
        "model_url": model_url,
        "model_name": model_name or "",
        "api_key": api_key,
        "modelset": modelset_data or {},
        "tricks": trick_list,
        "tricksets": trickset_list,
    })

    if not model_url:
        click.echo("Starting petsitter dashboard with no upstream model configured.")
        click.echo("Configure a model via the dashboard at http://" + listen_on)
    else:
        click.echo(f"Starting petsitter on {host}:{port}")
        click.echo(f"Upstream: {model_url}")
    if model_name:
        click.echo(f"Model: {model_name}")
    if trick_list:
        click.echo(f"Tricks: {', '.join(trick_list)}")
    if trickset_list:
        click.echo(f"Trick configs: {', '.join(trickset_list)}")
    if model_config:
        click.echo(f"Model config: {model_config}")

    log_level = os.getenv("LOGLEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(levelname)s: %(message)s"
    )

    uvicorn.run(app, host=host, port=port, timeout_graceful_shutdown=3)


if __name__ == "__main__":
    cli()

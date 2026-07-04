"""HTTP server and CLI for petsitter."""

import json
import logging
import os
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
from starlette.staticfiles import StaticFiles

from src.loader import load_tricks
from src.proxy import ProxyHandler
from src.trick import Trick, build_modelset_example, configure_modelset, parse_mas_uri
from src.trickset import Trickset


class LogCaptureHandler(logging.Handler):
    def __init__(self, maxlen: int = 500):
        super().__init__()
        self.logs = deque(maxlen=maxlen)

    def emit(self, record: logging.LogRecord) -> None:
        self.logs.append({
            "time": datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
            "level": record.levelname,
            "message": self.format(record),
            "name": record.name,
        })

    def get_logs(self, level: str | None = None, limit: int = 100) -> list[dict]:
        logs = list(self.logs)
        if level:
            logs = [l for l in logs if l["level"] == level.upper()]
        return logs[-limit:]


_log_capture: LogCaptureHandler | None = None


def create_app(
    model_url: str,
    model_name: str | None,
    api_key: str,
    trick_paths: list[str],
    trickset_paths: list[str] | None = None,
    modelset_data: dict[str, str] | None = None,
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

    # Validate modelset against all loaded tricks' requirements
    all_tricks: list[Trick] = []
    for ts in tricksets.values():
        all_tricks.extend(ts.tricks)
    _validate_modelset(all_tricks, modelset_data)

    global _log_capture
    _log_capture = LogCaptureHandler()
    _log_capture.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logging.getLogger().addHandler(_log_capture)

    app = Starlette()

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
            tb = traceback.format_exc()
            click.echo(f"ERROR in stream_chat_completions: {e}")
            click.echo(tb)
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
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            click.echo(f"ERROR in chat_completions: {e}")
            click.echo(tb)
            return JSONResponse(
                {"error": {"message": str(e), "type": "proxy_error", "traceback": tb}},
                status_code=500,
            )
    app.add_route("/v1/chat/completions", chat_completions, methods=["POST"])

    async def models(request: Request) -> Response:
        try:
            result = await handler.models()
            return JSONResponse(result)
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            click.echo(f"ERROR in models: {e}")
            click.echo(tb)
            return JSONResponse(
                {"error": {"message": str(e), "type": "proxy_error", "traceback": tb}},
                status_code=500,
            )
    app.add_route("/v1/models", models, methods=["GET"])

    async def health(request: Request) -> Response:
        return JSONResponse({"status": "ok"})
    app.add_route("/health", health, methods=["GET"])

    gui_dir = Path(__file__).parent / "gui"
    app.mount("/static", StaticFiles(directory=str(gui_dir)), name="static")

    async def gui_page(request: Request) -> Response:
        content = (gui_dir / "index.html").read_text()
        return Response(content=content, media_type="text/html")
    app.add_route("/gui", gui_page, methods=["GET"])
    app.add_route("/", gui_page, methods=["GET"])

    async def docs_page(request: Request) -> Response:
        content = (gui_dir / "swagger.html").read_text()
        return Response(content=content, media_type="text/html")
    app.add_route("/docs", docs_page, methods=["GET"])

    async def gui_info(request: Request) -> Response:
        return JSONResponse({
            "listen_on": f"{request.url.hostname}:{request.url.port}",
            "model_url": model_url,
            "model_name": model_name,
        })
    app.add_route("/api/info", gui_info, methods=["GET"])

    async def gui_tricks(request: Request) -> Response:
        return JSONResponse(handler.get_tricks_info())
    app.add_route("/api/tricks", gui_tricks, methods=["GET"])

    async def gui_tricks_available(request: Request) -> Response:
        tricks_dir = Path("tricks")
        files = []
        if tricks_dir.exists():
            for f in sorted(tricks_dir.glob("*.py")):
                if f.name != "__init__.py":
                    files.append(str(f))
        return JSONResponse(files)
    app.add_route("/api/tricks/available", gui_tricks_available, methods=["GET"])

    async def gui_tricks_load(request: Request) -> Response:
        data = await request.json()
        path = data.get("path", "")
        ts_name = data.get("trickset")
        try:
            trick = handler.add_trick(path, ts_name=ts_name)
            return JSONResponse({"success": True, "name": type(trick).__name__})
        except Exception as e:
            return JSONResponse({"success": False, "error": str(e)}, status_code=400)
    app.add_route("/api/tricks/load", gui_tricks_load, methods=["POST"])

    async def gui_tricks_unload(request: Request) -> Response:
        data = await request.json()
        name = data.get("name", "")
        ts_name = data.get("trickset")
        if handler.remove_trick(name, ts_name=ts_name):
            return JSONResponse({"success": True})
        return JSONResponse({"success": False, "error": f"Trick '{name}' not found"}, status_code=404)
    app.add_route("/api/tricks/unload", gui_tricks_unload, methods=["POST"])

    async def gui_tricks_reorder(request: Request) -> Response:
        data = await request.json()
        name = data.get("name", "")
        new_index = data.get("new_index", 0)
        ts_name = data.get("trickset")
        if handler.reorder_trick(name, new_index, ts_name=ts_name):
            return JSONResponse({"success": True})
        return JSONResponse({"success": False, "error": f"Trick '{name}' not found"}, status_code=404)
    app.add_route("/api/tricks/reorder", gui_tricks_reorder, methods=["POST"])

    async def gui_logs(request: Request) -> Response:
        level = request.query_params.get("level")
        limit_str = request.query_params.get("limit", "100")
        try:
            limit = max(1, min(500, int(limit_str)))
        except (ValueError, TypeError):
            limit = 100
        logs = _log_capture.get_logs(level=level, limit=limit) if _log_capture else []
        return JSONResponse(logs)
    app.add_route("/api/logs", gui_logs, methods=["GET"])

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
        tricksets_dir = Path("tricksets")
        files = []
        if tricksets_dir.exists():
            for f in sorted(tricksets_dir.glob("*.json")):
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
            return JSONResponse({"error": f"Trickset '{name}' not found"}, status_code=404)
        data = await request.json()
        if "filters" in data:
            ts.filters = data["filters"]
        if "tricks" in data:
            ts.trick_paths = list(data["tricks"])
            ts.load_tricks()
        if ts.file_path:
            ts.save()
        return JSONResponse({"success": True})
    app.add_route("/api/tricksets/{name}", update_trickset, methods=["PUT"])

    return app


def _get_version() -> str:
    try:
        return _pkg_version("petsitter")
    except PackageNotFoundError:
        pass
    try:
        return subprocess.run(
            ["git", "describe", "--tags", "--dirty", "--always"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception:
        return "0.0.0"


def _validate_modelset(tricks: list[Trick], modelset_data: dict[str, str] | None) -> None:
    """Check that all loaded tricks have their required models available."""
    modelset_keys = set(modelset_data.keys()) if modelset_data else set()

    for trick in tricks:
        required = set(trick.required_models)
        if modelset_data is not None:
            missing = required - modelset_keys
            if missing:
                keys_str = ", ".join(f"{k!r}" for k in sorted(required))
                missing_str = ", ".join(f"{k!r}" for k in sorted(missing))
                example = build_modelset_example(list(required))
                click.echo(
                    f"Error: Trick {type(trick).__name__} requires model keys "
                    f"[{keys_str}], but {missing_str} "
                    f"{'is' if len(missing) == 1 else 'are'} missing from the modelset.\n"
                    f"Expected a modelset like:\n{example}",
                    err=True,
                )
                raise SystemExit(1)
        else:
            extras = [k for k in trick.required_models if k != "default"]
            if extras:
                keys_str = ", ".join(f"{k!r}" for k in trick.required_models)
                example = build_modelset_example(trick.required_models)
                click.echo(
                    f"Error: Trick {type(trick).__name__} requires model keys "
                    f"[{keys_str}], but -mc/--model-config was not provided.\n"
                    f"Provide a model config JSON file like:\n{example}",
                    err=True,
                )
                raise SystemExit(1)


@click.command()
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
    help="Path to a trick module (can be specified multiple times)",
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
    modelset_data: dict[str, str] | None = None
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

        if model_url is None and "default" in modelset_data:
            model_url, inferred_name = parse_mas_uri(modelset_data["default"])
            if model_name is None:
                model_name = inferred_name

        configure_modelset(modelset_data)

    if not model_url:
        click.echo(
            "Error: -u/--url is required when -mc/--model-config is not provided "
            "(or the model config must have a 'default' key)",
            err=True,
        )
        raise SystemExit(1)

    if ":" in listen_on:
        host, port_str = listen_on.rsplit(":", 1)
        port = int(port_str)
    else:
        host = listen_on
        port = 8080

    app = create_app(
        model_url, model_name, api_key,
        trick_paths=list(tricks),
        trickset_paths=list(tricksets),
        modelset_data=modelset_data,
    )

    click.echo(f"Starting petsitter on {host}:{port}")
    click.echo(f"Upstream: {model_url}")
    if model_name:
        click.echo(f"Model: {model_name}")
    if tricks:
        click.echo(f"Tricks: {', '.join(tricks)}")
    if tricksets:
        click.echo(f"Trick configs: {', '.join(tricksets)}")
    if model_config:
        click.echo(f"Model config: {model_config}")

    log_level = os.getenv("LOGLEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(levelname)s: %(message)s"
    )

    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    cli()

"""HTTP server and CLI for petsitter."""

import json
import logging
import os
from collections import deque
from datetime import datetime
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
from src.trick import Trick


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
) -> Starlette:
    """Create the petsitter Starlette application.

    Args:
        model_url: Base URL of the upstream model.
        model_name: Optional model name override.
        api_key: API key for upstream.
        trick_paths: List of trick file paths.

    Returns:
        Configured Starlette app.
    """
    tricks = load_tricks(trick_paths) if trick_paths else []
    handler = ProxyHandler(model_url, model_name, api_key, tricks)

    global _log_capture
    _log_capture = LogCaptureHandler()
    _log_capture.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logging.getLogger().addHandler(_log_capture)

    app = Starlette()

    async def stream_chat_completions(handler: ProxyHandler, payload: dict):
        """Stream chat completions as SSE events in OpenAI format."""
        try:
            result = await handler.chat_completions(payload)
            
            # Convert to streaming format with delta instead of message
            message = result["choices"][0]["message"]
            
            # Build streaming response with delta
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
            
            # Add tool_calls to delta if present
            if "tool_calls" in message:
                stream_result["choices"][0]["delta"]["tool_calls"] = message["tool_calls"]
            
            yield f"data: {json.dumps(stream_result)}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            click.echo(f"ERROR in stream_chat_completions: {e}")
            click.echo(tb)
            error_data = {
                "error": {"message": str(e), "type": "proxy_error"}
            }
            yield f"data: {json.dumps(error_data)}\n\n"

    async def chat_completions(request: Request) -> Response:
        """Proxy chat completions to upstream model."""
        try:
            payload = await request.json()
            stream = payload.get("stream", False)
            
            if stream:
                return StreamingResponse(
                    stream_chat_completions(handler, payload),
                    media_type="text/event-stream",
                )
            else:
                result = await handler.chat_completions(payload)
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
        """Proxy models listing to upstream."""
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
    app.add_route("/gui/info", gui_info, methods=["GET"])

    async def gui_tricks(request: Request) -> Response:
        return JSONResponse(handler.get_tricks_info())
    app.add_route("/gui/tricks", gui_tricks, methods=["GET"])

    async def gui_tricks_available(request: Request) -> Response:
        tricks_dir = Path("tricks")
        files = []
        if tricks_dir.exists():
            for f in sorted(tricks_dir.glob("*.py")):
                if f.name != "__init__.py":
                    files.append(str(f))
        return JSONResponse(files)
    app.add_route("/gui/tricks/available", gui_tricks_available, methods=["GET"])

    async def gui_tricks_load(request: Request) -> Response:
        data = await request.json()
        path = data.get("path", "")
        try:
            trick = handler.add_trick(path)
            return JSONResponse({"success": True, "name": type(trick).__name__})
        except Exception as e:
            return JSONResponse({"success": False, "error": str(e)}, status_code=400)
    app.add_route("/gui/tricks/load", gui_tricks_load, methods=["POST"])

    async def gui_tricks_unload(request: Request) -> Response:
        data = await request.json()
        name = data.get("name", "")
        if handler.remove_trick(name):
            return JSONResponse({"success": True})
        return JSONResponse({"success": False, "error": f"Trick '{name}' not found"}, status_code=404)
    app.add_route("/gui/tricks/unload", gui_tricks_unload, methods=["POST"])

    async def gui_logs(request: Request) -> Response:
        level = request.query_params.get("level")
        limit_str = request.query_params.get("limit", "100")
        try:
            limit = max(1, min(500, int(limit_str)))
        except (ValueError, TypeError):
            limit = 100
        logs = _log_capture.get_logs(level=level, limit=limit) if _log_capture else []
        return JSONResponse(logs)
    app.add_route("/gui/logs", gui_logs, methods=["GET"])

    return app


@click.command()
@click.option(
    "--model_url",
    required=True,
    help="Base URL of the upstream model (e.g., http://localhost:11434)",
)
@click.option(
    "--model_name",
    default=None,
    help="Model name to use (optional for some backends like vllm, sglang)",
)
@click.option(
    "--api_key",
    default="",
    help="API key for upstream (if required)",
)
@click.option(
    "--trick",
    "tricks",
    multiple=True,
    help="Path to a trick module (can be specified multiple times)",
)
@click.option(
    "--listen_on",
    default="localhost:8080",
    help="Host:port to listen on (default: localhost:8080)",
)
def cli(
    model_url: str,
    model_name: str | None,
    api_key: str,
    tricks: tuple[str, ...],
    listen_on: str,
) -> None:
    """Petsitter - OpenAI-compatible proxy with tricks.

    Example:

    \b
        petsitter --model_url http://localhost:11434 \\
                  --model_name llama3:8b \\
                  --trick tricks/tool_call.py \\
                  --trick tricks/json_mode.py \\
                  --listen_on localhost:8080
    """
    # Parse listen_on
    if ":" in listen_on:
        host, port_str = listen_on.rsplit(":", 1)
        port = int(port_str)
    else:
        host = listen_on
        port = 8080

    app = create_app(model_url, model_name, api_key, list(tricks))

    click.echo(f"Starting petsitter on {host}:{port}")
    click.echo(f"Upstream: {model_url}")
    if model_name:
        click.echo(f"Model: {model_name}")
    if tricks:
        click.echo(f"Tricks: {', '.join(tricks)}")

    # Configure logging from environment
    log_level = os.getenv("LOGLEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(levelname)s: %(message)s"
    )

    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    cli()

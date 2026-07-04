"""GUI dashboard routes for petsitter."""

from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.staticfiles import StaticFiles

from src.trick import get_model_config, parse_mas_uri, remove_model_config, update_model_config
from src.trickset import Trickset

_log_capture = None


def register_gui_routes(app, handler, api_key):
    global _log_capture
    from src.server import _log_capture as server_log_capture
    _log_capture = server_log_capture

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
            "model_url": handler.model_url,
            "model_name": handler.model_name,
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

    async def gui_trick_detail(request: Request) -> Response:
        name = request.path_params.get("name")
        info = handler.get_tricks_info()
        for t in info:
            if t["name"] == name:
                return JSONResponse(t)
        return JSONResponse({"error": f"Trick '{name}' not found"}, status_code=404)
    app.add_route("/api/tricks/{name}", gui_trick_detail, methods=["GET"])

    async def gui_trick_toggle(request: Request) -> Response:
        name = request.path_params.get("name")
        data = await request.json()
        enabled = data.get("enabled")
        if handler.toggle_trick(name, enabled):
            return JSONResponse({"success": True, "enabled": handler._enabled.get(name, True)})
        return JSONResponse({"success": False, "error": f"Trick '{name}' not found"}, status_code=404)
    app.add_route("/api/tricks/{name}/toggle", gui_trick_toggle, methods=["POST"])

    # ----- model config API endpoints -----

    async def gui_models(request: Request) -> Response:
        model_url = handler.model_url
        model_name = handler.model_name or ""
        info = {
            "model_url": model_url,
            "model_name": model_name,
            "api_key": bool(api_key),
            "configured_models": {},
        }
        for key in ("default", "thinker", "toolcall", "think", "tool_caller"):
            try:
                info["configured_models"][key] = get_model_config(key)
            except KeyError:
                pass
        return JSONResponse(info)
    app.add_route("/api/models", gui_models, methods=["GET"])

    async def gui_models_update(request: Request) -> Response:
        data = await request.json()
        if "model_url" in data:
            handler.model_url = data["model_url"].rstrip("/")
        if "model_name" in data:
            handler.model_name = data["model_name"]
        if "api_key" in data:
            handler.api_key = data["api_key"]
        if "add_model" in data:
            key = data["add_model"].get("key")
            uri = data["add_model"].get("uri")
            if key and uri:
                url, name = parse_mas_uri(uri)
                update_model_config(key, url, name)
        if "update_model" in data:
            um = data["update_model"]
            key = um.get("key")
            field = um.get("field")
            value = um.get("value")
            if key and field and value:
                try:
                    existing = get_model_config(key)
                except KeyError:
                    existing = {"model_url": "", "model_name": ""}
                if field == "url":
                    existing["model_url"] = value
                elif field == "name":
                    existing["model_name"] = value
                update_model_config(key, existing["model_url"], existing["model_name"])
        if "remove_model" in data:
            remove_model_config(data["remove_model"])
        return JSONResponse({"success": True})
    app.add_route("/api/models", gui_models_update, methods=["POST"])

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

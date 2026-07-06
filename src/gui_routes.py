"""GUI dashboard routes for petsitter."""

import asyncio
import json
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.staticfiles import StaticFiles

from src.trick import Trick, get_model_config, remove_model_config, update_model_config
from src.trickset import Trickset

_log_capture = None
_config_path: str | None = None


def _collect_trick_paths(handler) -> list[str]:
    paths = set()
    for ts_name, ts in handler.tricksets.items():
        if ts_name == "_default":
            paths.update(ts.trick_paths)
    return sorted(paths, key=lambda p: (p.count("/"), p))


def _save_full_config(handler, api_key):
    """Persist current dashboard model + trick settings to config file."""
    if not _config_path:
        return
    modelset = {}
    from src.trick import _modelset
    for key in set(_modelset.keys()) | {"default"}:
        try:
            cfg = get_model_config(key)
            entry: dict[str, Any] = {"url": cfg.get("url", "")}
            model_val = cfg.get("model")
            if model_val is not None:
                entry["model"] = model_val
            key_val = cfg.get("key")
            if key_val is not None:
                entry["key"] = key_val
            modelset[key] = entry
        except KeyError:
            pass
    if "default" not in modelset:
        entry: dict[str, Any] = {"url": handler.model_url}
        if handler.model_name:
            entry["model"] = handler.model_name
        if api_key:
            entry["key"] = api_key
        modelset["default"] = entry
    config = {
        "model_url": handler.model_url,
        "model_name": handler.model_name or "",
        "api_key": api_key,
        "modelset": modelset,
        "tricks": _collect_trick_paths(handler),
    }
    Path(_config_path).write_text(json.dumps(config, indent=2) + "\n")


def _introspect_trick_file(path: Path) -> dict:
    """Extract display_name and brief from a trick module without instantiating."""
    import importlib.util

    info = {"path": str(path), "display_name": None, "brief": None, "mtime": path.stat().st_mtime_ns}
    try:
        spec = importlib.util.spec_from_file_location(path.stem, str(path))
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            for name in dir(mod):
                obj = getattr(mod, name)
                if isinstance(obj, type) and issubclass(obj, Trick) and obj is not Trick:
                    info["display_name"] = getattr(obj, "__display_name__", None) or name
                    info["brief"] = getattr(obj, "__brief__", "")
                    break
    except Exception:
        pass
    return info


def register_gui_routes(app, handler, api_key, config_path: str | None = None):
    global _log_capture, _config_path
    from src.server import _log_capture as server_log_capture
    _log_capture = server_log_capture
    _config_path = config_path

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
        from src.server import _get_version
        return JSONResponse({
            "listen_on": f"{request.url.hostname}:{request.url.port}",
            "model_url": handler.model_url,
            "model_name": handler.model_name,
            "version": _get_version(),
        })
    app.add_route("/api/info", gui_info, methods=["GET"])

    async def gui_tricks(request: Request) -> Response:
        return JSONResponse(handler.get_tricks_info())
    app.add_route("/api/tricks", gui_tricks, methods=["GET"])

    async def gui_tricks_available(request: Request) -> Response:
        tricks_dir = Path("tricks")
        if not tricks_dir.exists():
            tricks_dir = Path(__file__).parent.parent / "tricks"
        result = []
        if tricks_dir.exists():
            for f in sorted(tricks_dir.glob("*.py")):
                if f.name == "__init__.py":
                    continue
                result.append(_introspect_trick_file(f))
        return JSONResponse(result)
    app.add_route("/api/tricks/available", gui_tricks_available, methods=["GET"])

    async def gui_tricks_load(request: Request) -> Response:
        data = await request.json()
        path = data.get("path", "")
        ts_name = data.get("trickset")
        try:
            trick = handler.add_trick(path, ts_name=ts_name)
            _save_full_config(handler, api_key)
            return JSONResponse({"success": True, "name": type(trick).__name__})
        except Exception as e:
            return JSONResponse({"success": False, "error": str(e)}, status_code=400)
    app.add_route("/api/tricks/load", gui_tricks_load, methods=["POST"])

    async def gui_tricks_unload(request: Request) -> Response:
        data = await request.json()
        name = data.get("name", "")
        ts_name = data.get("trickset")
        if handler.remove_trick(name, ts_name=ts_name):
            _save_full_config(handler, api_key)
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
        from src.trick import _modelset
        all_keys = sorted(set(_modelset.keys()) | {"default"})
        configured: dict[str, dict[str, Any]] = {}
        for k in all_keys:
            try:
                configured[k] = get_model_config(k)
            except KeyError:
                configured[k] = {"url": "", "model": "", "key": ""}
        return JSONResponse({
            "model_url": handler.model_url,
            "model_name": handler.model_name or "",
            "api_key": bool(api_key),
            "configured_models": configured,
        })
    app.add_route("/api/models", gui_models, methods=["GET"])

    async def gui_models_update(request: Request) -> Response:
        data = await request.json()
        if "model_url" in data:
            handler.model_url = data["model_url"].rstrip("/")
        if "model_name" in data:
            handler.model_name = data["model_name"]
        if "api_key" in data:
            handler.api_key = data["api_key"]
        if "set_model" in data:
            sm = data["set_model"]
            key = sm.get("key", "")
            url = sm.get("model_url", sm.get("url", "")).rstrip("/")
            model_val = sm.get("model_name", sm.get("model", ""))
            key_val = sm.get("api_key", sm.get("key", ""))
            if key == "default":
                handler.model_url = url
                handler.model_name = model_val
            update_model_config(key, url, model_val, key_val)
        if "remove_model" in data:
            remove_model_config(data["remove_model"])
        _save_full_config(handler, api_key)
        return JSONResponse({"success": True})
    app.add_route("/api/models", gui_models_update, methods=["POST"])

    async def gui_trickset_create(request: Request) -> Response:
        data = await request.json()
        name = data.get("name", "")
        filters = data.get("filters", {"X-Title": "*", "Model": "*"})
        if not name:
            return JSONResponse({"success": False, "error": "name required"}, status_code=400)
        parameters = data.get("parameters", {})
        models = data.get("models", {})
        ts = Trickset(name, "0.7.0", filters, [], parameters=parameters, models=models)
        ts.file_path = str(Path("tricksets") / f"{name}.json")
        ts.save()
        handler.tricksets[name] = ts
        return JSONResponse({"success": True, "name": name})
    app.add_route("/api/tricksets/create", gui_trickset_create, methods=["POST"])

    async def gui_logs_sse(request: Request) -> StreamingResponse:
        level = request.query_params.get("level", "")

        async def event_generator():
            if not _log_capture:
                return
            q = _log_capture.add_sse_client()
            try:
                for entry in _log_capture.get_logs(level=level, limit=200):
                    if level and entry["level"] != level.upper():
                        continue
                    yield f"data: {json.dumps(entry)}\n\n"
                while True:
                    try:
                        entry = await asyncio.wait_for(q.get(), timeout=30)
                        if level and entry["level"] != level.upper():
                            continue
                        yield f"data: {json.dumps(entry)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            except asyncio.CancelledError:
                pass
            finally:
                _log_capture.remove_sse_client(q)

        return StreamingResponse(event_generator(), media_type="text/event-stream")
    app.add_route("/api/logs", gui_logs_sse, methods=["GET"])

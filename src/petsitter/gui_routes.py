"""GUI dashboard routes for petsitter."""

import asyncio
import json
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.staticfiles import StaticFiles

from petsitter.trick import Trick, get_model_config, remove_model_config, update_model_config
from petsitter.trickset import Trickset, SCHEMA

_log_capture = None
_config_path: str | None = None


def _save_full_config(handler, api_key):
    """Persist current dashboard model settings to the config file."""
    if not _config_path:
        return
    modelset = {}
    from petsitter.trick import _modelset
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
    }
    Path(_config_path).write_text(json.dumps(config, indent=2) + "\n")


def _introspect_trick_file(path: Path) -> dict:
    """Extract display_name, brief, keywords, and prompt_keyword from a trick module without instantiating."""
    import importlib.util

    info = {"path": str(path), "display_name": None, "brief": None, "keywords": [], "prompt_keyword": "", "config_fields": [], "mtime": path.stat().st_mtime_ns}
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
                    info["keywords"] = list(getattr(obj, "keywords", []) or [])
                    info["prompt_keyword"] = getattr(obj, "prompt_keyword", "") or ""
                    info["config_fields"] = list(getattr(obj, "config_fields", []) or [])
                    break
    except Exception:
        pass
    return info


def register_gui_routes(app, handler, api_key, config_path: str | None = None):
    global _log_capture, _config_path
    from petsitter.server import _log_capture as server_log_capture
    _log_capture = server_log_capture
    _config_path = config_path

    gui_dir = Path(__file__).parent / "gui"
    app.mount("/static", StaticFiles(directory=str(gui_dir)), name="static")

    # The dashboard is read from disk on every request so edits show up on a
    # reload. Without this header a browser caches it heuristically and serves
    # stale markup and stale JS, which looks exactly like a broken page.
    NO_STORE = {"Cache-Control": "no-store, must-revalidate"}

    async def gui_page(request: Request) -> Response:
        content = (gui_dir / "index.html").read_text()
        return Response(content=content, media_type="text/html", headers=NO_STORE)
    app.add_route("/gui", gui_page, methods=["GET"])
    app.add_route("/", gui_page, methods=["GET"])

    async def docs_page(request: Request) -> Response:
        content = (gui_dir / "swagger.html").read_text()
        return Response(content=content, media_type="text/html")
    app.add_route("/docs", docs_page, methods=["GET"])

    async def help_page(request: Request) -> Response:
        readme = Path(__file__).resolve().parent / "README.md"
        text = readme.read_text(encoding="utf-8")
        return Response(content=text, media_type="text/plain")
    app.add_route("/api/help", help_page, methods=["GET"])

    async def gui_info(request: Request) -> Response:
        from petsitter.server import _get_version
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
            tricks_dir = Path(__file__).parent / "tricks"
        result = []
        if tricks_dir.exists():
            for f in sorted(tricks_dir.glob("*.py")):
                if f.name == "__init__.py":
                    continue
                info = _introspect_trick_file(f)
                # Report built-ins by their portable "tricks/<name>.py" form;
                # the Load button posts this straight into a trickset, and an
                # absolute site-packages path would not survive a move.
                info["path"] = f"tricks/{f.name}"
                result.append(info)
        return JSONResponse(result)
    app.add_route("/api/tricks/available", gui_tricks_available, methods=["GET"])

    # ---- community index -------------------------------------------------
    # There is no registry server; these routes read the static index.json
    # through src.registry so the browser doesn't have to deal with CORS or
    # duplicate the cache.

    def _registry_config_dir() -> Path:
        if _config_path:
            return Path(_config_path).parent
        return Path.home() / ".config" / "petsitter"

    async def gui_registry(request: Request) -> Response:
        from petsitter import registry
        q = request.query_params.get("q", "")
        show_all = request.query_params.get("all") in ("1", "true")
        refresh = request.query_params.get("refresh") in ("1", "true")
        cfg_dir = _registry_config_dir()
        try:
            index = await asyncio.to_thread(
                registry.fetch_index, cfg_dir, None, refresh
            )
        except registry.RegistryError as e:
            # Not a server error. The index is a static file someone else
            # hosts, and it may simply not be published yet, or the machine
            # may be offline. The dashboard still has local tricks to show,
            # so answer 200 with an empty community half.
            return JSONResponse({
                "tricks": [], "total": 0, "generated": "",
                "index_url": registry.index_url(None),
                "unavailable": str(e),
            })
        results = registry.search(index, q, featured_only=not show_all and not q)
        installed = {i["name"]: i["version"]
                     for i in registry.list_installed(cfg_dir)}
        for e in results:
            e = e  # entries are plain dicts from the cached index
            e["installed"] = installed.get(e["name"])
        return JSONResponse({
            "generated": index.get("generated", ""),
            "total": index.get("count", 0),
            "index_url": registry.index_url(None),
            "tricks": results,
        })
    app.add_route("/api/registry", gui_registry, methods=["GET"])

    async def gui_registry_install(request: Request) -> Response:
        from petsitter import registry
        data = await request.json()
        name = (data.get("name") or "").strip()
        version = data.get("version") or None
        ts_name = data.get("trickset")
        cfg_dir = _registry_config_dir()
        try:
            index = await asyncio.to_thread(registry.fetch_index, cfg_dir, None, False)
            entry = registry.resolve(index, name, version)
            path, fresh = await asyncio.to_thread(registry.install, entry, cfg_dir, False)
        except registry.RegistryError as e:
            return JSONResponse({"success": False, "error": str(e)}, status_code=400)

        spec = registry.pkg_spec(entry["name"], entry["version"])
        result = {"success": True, "spec": spec, "path": str(path), "fresh": fresh}
        if ts_name:
            try:
                trick = handler.add_trick(spec, ts_name=ts_name)
                result["loaded"] = type(trick).__name__
            except Exception as e:
                # It's on disk; only the wiring failed, and saying which is
                # the difference between "retry" and "report a bug".
                result["success"] = False
                result["error"] = f"installed to {path}, but adding to '{ts_name}' failed: {e}"
                return JSONResponse(result, status_code=400)
        return JSONResponse(result)
    app.add_route("/api/registry/install", gui_registry_install, methods=["POST"])

    async def gui_registry_source(request: Request) -> Response:
        """Source of a trick, for the Read button. Installed copy if we have it."""
        from petsitter import registry
        name = request.query_params.get("name", "")
        version = request.query_params.get("version", "")
        cfg_dir = _registry_config_dir()
        try:
            if version:
                local = registry.installed_path(cfg_dir, name, version)
                if local.exists():
                    return Response(content=local.read_text(), media_type="text/plain")
            index = await asyncio.to_thread(registry.fetch_index, cfg_dir, None, False)
            entry = registry.resolve(index, name, version or None)
            blob = await asyncio.to_thread(registry._fetch, entry["url"], 30)
            return Response(content=blob.decode("utf-8", "replace"), media_type="text/plain")
        except registry.RegistryError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
    app.add_route("/api/registry/source", gui_registry_source, methods=["GET"])

    # ---- playground ------------------------------------------------------
    # A real trip through the pipeline, not a simulation: the same
    # chat_completions() a client would hit, with tracing switched on so the
    # dashboard can show which tricks actually did something.

    async def gui_playground(request: Request) -> Response:
        import time as _time
        from petsitter.observability import start_trace, reset_trace, get_trace

        data = await request.json()
        messages = data.get("messages") or []
        if not messages:
            return JSONResponse({"error": "no messages"}, status_code=400)
        ts_name = data.get("trickset") or ""

        payload = {"messages": messages, "stream": False}
        # Naming a loaded trickset pins the request to it; otherwise the
        # request goes through normal filter matching, which is the case
        # you want when you're testing the filters themselves.
        if ts_name and ts_name in handler.tricksets:
            payload["model"] = f"trickset/{ts_name}"
        elif handler.model_name:
            payload["model"] = handler.model_name
        if data.get("temperature") is not None:
            payload["temperature"] = data["temperature"]

        token = start_trace()
        started = _time.monotonic()
        try:
            result = await handler.chat_completions(
                payload, x_title=data.get("x_title", "petsitter-playground"),
            )
            trace = list(get_trace() or [])
        except Exception as e:
            trace = list(get_trace() or [])
            return JSONResponse({
                "error": f"{type(e).__name__}: {e}",
                "trace": trace,
                "elapsed_ms": int((_time.monotonic() - started) * 1000),
            }, status_code=502)
        finally:
            reset_trace(token)

        reply = ""
        tool_calls = None
        try:
            msg = (result.get("choices") or [{}])[0].get("message") or {}
            reply = msg.get("content") or ""
            tool_calls = msg.get("tool_calls")
        except (AttributeError, IndexError, TypeError):
            reply = ""

        return JSONResponse({
            "reply": reply,
            "tool_calls": tool_calls,
            "trace": trace,
            "elapsed_ms": int((_time.monotonic() - started) * 1000),
            "usage": result.get("usage") if isinstance(result, dict) else None,
        })
    app.add_route("/api/playground", gui_playground, methods=["POST"])

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
        tid = data.get("id", "")
        name = data.get("name", "")
        ts_name = data.get("trickset")
        if handler.remove_trick(tid, ts_name=ts_name):
            return JSONResponse({"success": True})
        return JSONResponse({"success": False, "error": f"Trick '{name or tid}' not found"}, status_code=404)
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
        ts_name = data.get("trickset", "_default")
        if handler.toggle_trick(name, enabled, ts_name=ts_name):
            ts = handler.tricksets.get(ts_name)
            if ts:
                for i, t in enumerate(ts.tricks):
                    if type(t).__name__ == name:
                        return JSONResponse({"success": True, "enabled": ts.trick_enabled[i] if i < len(ts.trick_enabled) else True})
            return JSONResponse({"success": True, "enabled": True})
        return JSONResponse({"success": False, "error": f"Trick '{name}' not found"}, status_code=404)
    app.add_route("/api/tricks/{name}/toggle", gui_trick_toggle, methods=["POST"])

    # ----- model config API endpoints -----

    async def gui_models(request: Request) -> Response:
        from petsitter.trick import _modelset
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
        ts = Trickset(name, SCHEMA, filters, [], parameters=parameters, models=models)
        ts.file_path = str(Path.home() / ".config" / "petsitter" / "tricksets" / f"{name}.json")
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

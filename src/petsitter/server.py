"""HTTP server and CLI for petsitter."""

import asyncio
import atexit
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
from collections import deque
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path
from typing import Any

import click
import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from petsitter.agent_manager import AgentManager
from petsitter.gui_routes import register_gui_routes
from petsitter.proxy import ProxyHandler
from petsitter.trick import (
    configure,
    configure_modelset,
)
from petsitter.trickset import SCHEMA, Trickset, _default_logfile, _new_id


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
_SOURCE_TRICKSETS = Path(__file__).resolve().parent / "tricksets"

# Tricks seeded into a brand-new "_default" trickset when no tricks are
# configured anywhere (no -t flags, no saved trickset file).
DEFAULT_TRICKS = ["tricks/conversational_tool.py", "tricks/secrets_protector.py"]

_PROXY_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?::\d+)?$")


BANNER = """██████  ███████ ████████ ███████ ██ ████████ ████████ ███████ ██████
██   ██ ██         ██    ██      ██    ██       ██    ██      ██   ██
██████  █████      ██    ███████ ██    ██       ██    █████   ██████
██      ██         ██         ██ ██    ██       ██    ██      ██   ██
██      ███████    ██    ███████ ██    ██       ██    ███████ ██   ██"""

# start.txt is the reference for this layout: the banner, then one obvious
# place to go, then the details, then a rule with the logs below it.
BANNER_WIDTH = 70
BANNER_INDENT = " " * 18


def _hyperlink(url: str, label: str = "") -> str:
    """Wrap a URL in an OSC 8 escape so the terminal makes it clickable.

    Emitted only for an interactive terminal: piping the output (into a log,
    a pager, or a file) should yield the plain URL, and a dumb terminal would
    print the escape as garbage rather than swallow it.
    """
    label = label or url
    try:
        if not sys.stdout.isatty():
            return label
    except (AttributeError, ValueError):
        return label
    if os.environ.get("TERM", "dumb") in ("", "dumb"):
        return label
    return f"\033]8;;{url}\033\\{label}\033]8;;\033\\"



def _print_startup(listen_on: str, model_url: str, model_name: str,
                   config_path: str, trickset_labels: list[str]) -> None:
    """Announce the one thing a person needs -- the web interface -- first.

    The details underneath matter when something is wrong; the link matters
    every time, so it gets the space and nothing competes with it.
    """
    # 0.0.0.0 is not something anyone can click; offer a URL that works.
    display_host = listen_on
    for wildcard in ("0.0.0.0", "[::]"):
        if listen_on.startswith(wildcard + ":"):
            display_host = "localhost:" + listen_on.rsplit(":", 1)[1]
            break
    url = "http://" + display_host

    click.echo("")
    click.echo(click.style(BANNER, fg="cyan"))
    click.echo("")
    click.echo(BANNER_INDENT + click.style("WEB INTERFACE ( START HERE )", bold=True))
    click.echo("")
    click.echo(BANNER_INDENT + _hyperlink(
        url, click.style(url, fg="bright_cyan", bold=True, underline=True)))
    click.echo("")
    click.echo("")

    rows = []
    if model_url:
        rows.append(("upstream", model_url + ("  (" + model_name + ")" if model_name else "")))
    else:
        rows.append(("upstream", click.style("not set yet \u2014 set one in the web interface", fg="yellow")))
    if trickset_labels:
        rows.append(("tricksets", ", ".join(trickset_labels)))
    rows.append(("config", config_path))
    for key, value in rows:
        if key == "config":
            value = _hyperlink("file://" + config_path, value)
        click.echo("  " + click.style(key.ljust(10), fg="bright_black") + value)

    click.echo("")
    click.echo(click.style("-" * BANNER_WIDTH, fg="bright_black"))


_uvicorn_server = None
_restored_agents: list[str] = []


def is_shutting_down() -> bool:
    """True once the server has begun stopping.

    Long-running responses poll this so they can end on their own terms during
    the graceful window, instead of being cancelled after it.
    """
    server = _uvicorn_server
    return bool(server is not None and server.should_exit)


def _is_petsitter(host: str, port: int) -> bool:
    """Whether the thing on that port is another petsitter.

    Checks more than one endpoint on purpose: an older or unhealthy petsitter
    may fail one of them, and it is still more useful to say "that's petsitter"
    than "that's something else".
    """
    for path, marker in (("/health", None), ("/api/info", "version")):
        try:
            r = httpx.get(f"http://{host}:{port}{path}", timeout=1.0)
        except Exception:
            continue
        if r.status_code != 200:
            continue
        if marker is None:
            return True
        try:
            if marker in r.json():
                return True
        except Exception:
            continue
    return False


def _print_port_busy(host: str, port: int) -> None:
    """Explain the clash and what to do, rather than a raw bind traceback."""
    display = "localhost" if host in ("127.0.0.1", "0.0.0.0", "::1") else host
    mine = _is_petsitter(host, port)

    click.echo("")
    if mine:
        click.echo(click.style(f"  petsitter is already running on {display}:{port}.", bold=True))
        click.echo("")
        click.echo("  You can just open it:")
        click.echo("  " + _hyperlink(f"http://{display}:{port}",
                                     click.style(f"http://{display}:{port}",
                                                 fg="bright_cyan", bold=True, underline=True)))
        click.echo("")
        click.echo("  Or run a second one alongside it on another port:")
    else:
        click.echo(click.style(
            f"  Port {port} is already being used by something else.", bold=True))
        click.echo("")
        click.echo("  petsitter can run on a different port instead:")
    click.echo("")
    click.echo(click.style(f"    petsitter -l {display}:{port + 1}", fg="bright_white"))
    click.echo("")
    click.echo(click.style(f"  ({display}:{port + 1} is just a suggestion \u2014 any free port works.)",
                           fg="bright_black"))
    click.echo("")


ISSUES_URL = "https://github.com/day50-dev/Petsitter/issues"


def _restore_agents() -> list[str]:
    """Put every connected tool's config back before petsitter goes away.

    A tool pointed at a proxy that is no longer listening is simply broken --
    Claude Code with ANTHROPIC_BASE_URL set to a dead port cannot reach
    Anthropic at all. Leaving that behind on exit would be the rudest thing
    petsitter could do, so connections last exactly as long as the process.

    Returns the display names of whatever was disconnected. The names are
    remembered because this runs from two places -- the CLI on its way out, and
    an atexit backstop for the exits that skip it -- and whichever goes second
    finds nothing left to restore but still has to be able to say what happened.
    """
    global _restored_agents
    if _agent_manager is None:
        return _restored_agents
    try:
        # get_registered() checks each tool's config file live, not just the
        # registry's memory of what it did -- so this still finds (and puts
        # back) an agent whose registry.json write silently failed to land
        # while its config was in fact left pointed at petsitter.
        registered = [aid for aid, e in (_agent_manager.get_registered().get("agents") or {}).items()
                      if e.get("status") == "registered"]
    except Exception:
        return _restored_agents
    if not registered:
        return _restored_agents
    names = list(_restored_agents)
    for agent_id in registered:
        try:
            _agent_manager.unregister(agent_id)
        except Exception:
            logging.getLogger("petsitter").exception("could not restore %s", agent_id)
            continue
        try:
            names.append(_agent_manager._get(agent_id).display_name)
        except Exception:
            names.append(agent_id)
    _restored_agents = names
    return names


def _print_goodbye(restored: list[str] | None = None) -> None:
    """A last word on the way out, with somewhere to send the complaints.

    Printed after the server has actually stopped, so it is the final thing on
    screen rather than something scrolled away by shutdown logging.
    """
    click.echo("")
    if restored:
        tools = ", ".join(restored)
        one = len(restored) == 1
        click.echo(click.style(
            f"  Put {tools} back the way {'it was' if one else 'they were'}.", bold=True))
        # The env var is read once at startup, so a session that was already
        # running is still pointed at a port nothing is listening on.
        click.echo(f"  Restart {'it' if one else 'them'} if {'it was' if one else 'they were'}"
                   f" already open \u2014 {'it is' if one else 'they are'} still pointed here.")
        click.echo("")
    click.echo(click.style("  Thanks for using petsitter.", bold=True))
    click.echo("")
    click.echo("  Anything broken, confusing, or missing? That's worth an issue \u2014")
    click.echo("  the confusing ones especially. Bug reports and ideas both welcome:")
    click.echo("")
    click.echo("  " + _hyperlink(ISSUES_URL, click.style(ISSUES_URL, fg="bright_cyan", underline=True)))
    click.echo("")


def _parse_p_path(path: str) -> tuple[str, str] | None:
    """Parse a ``/p/`` proxy path into ``(host, subpath)``.

    ``/p/build.nvidia.com/v1/chat/completions`` -> ``("build.nvidia.com", "/v1/chat/completions")``.
    Returns ``None`` if the path isn't a ``/p/`` route or the host is invalid.
    """
    if not path.startswith("/p/"):
        return None
    rest = path[len("/p/"):]
    host, sep, sub = rest.partition("/")
    if not host or not _PROXY_HOST_RE.match(host):
        return None
    return host, ("/" + sub.lstrip("/") if sep else "")


def _chunk_text(text: str, size: int = 64) -> list[str]:
    """Split text into fixed-size pieces, preserving whitespace/content exactly."""
    if not text:
        return []
    return [text[i:i + size] for i in range(0, len(text), size)]


async def _generic_proxy(target: str, request: Request) -> Response:
    """Transparently forward a request to *target* and stream back the response."""
    body = await request.body()
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length", "connection", "transfer-encoding")
    }
    try:
        async with httpx.AsyncClient() as client:
            upstream_resp = await client.request(
                request.method, target,
                content=body or None,
                headers=headers,
                timeout=120.0,
            )
    except httpx.TransportError as e:
        return JSONResponse({"error": str(e), "type": "proxy_error"}, status_code=502)
    if upstream_resp.headers.get("content-type", "").startswith("text/event-stream"):
        return StreamingResponse(
            upstream_resp.aiter_bytes(),
            media_type="text/event-stream",
            status_code=upstream_resp.status_code,
        )
    return Response(
        content=upstream_resp.content,
        status_code=upstream_resp.status_code,
        headers={"content-type": upstream_resp.headers.get("content-type", "application/json")},
    )


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


def reload_config(handler: "ProxyHandler") -> dict:
    """Re-read the config files on disk and apply them top-down.

    Re-applies the global model routing from ``config.json`` and reconciles
    each loaded trickset against its JSON file via ``Trickset.reread_config``.
    Tricks already loaded keep their runtime state (no behavior hot-reload);
    only configuration changes take effect. Tricksets on disk that are not
    loaded are loaded fresh; loaded tricksets whose files are gone are
    unloaded. Returns a summary of what changed.
    """
    from petsitter.trick import _modelset

    cfg = load_config()
    modelset_data = cfg.get("modelset") or {}
    if isinstance(modelset_data, dict):
        configure_modelset(modelset_data)

    model_url = cfg.get("model_url", "")
    model_name = cfg.get("model_name", "")
    api_key = cfg.get("api_key", "")
    if isinstance(modelset_data, dict) and isinstance(modelset_data.get("default"), dict):
        dflt = modelset_data["default"]
        if dflt.get("url"):
            model_url = dflt["url"]
        if isinstance(dflt.get("model"), str) and dflt["model"]:
            model_name = dflt["model"]

    model_url = model_url.rstrip("/")
    handler.model_url = model_url
    handler.model_name = model_name or ""
    handler.api_key = api_key
    configure(model_url, model_name or "", api_key)

    summary: dict[str, list[str]] = {"reloaded": [], "added": [], "removed": [], "kept": []}
    for name, ts in list(handler.tricksets.items()):
        res = ts.reread_config()
        action = res["action"]
        summary.setdefault(action, []).append(name)
        if action == "removed":
            del handler.tricksets[name]

    TRICKSETS_DIR.mkdir(parents=True, exist_ok=True)
    for f in sorted(TRICKSETS_DIR.glob("*.json")):
        if f.stem in handler.tricksets:
            continue
        try:
            ts = Trickset.load_from_file(str(f))
        except Exception:
            logging.getLogger("petsitter").exception("Failed to load trickset %s on reread", f)
            continue
        handler.tricksets[ts.name] = ts
        summary["added"].append(ts.name)

    # Reconcile the config file's "tricksets" list: entries are either file
    # paths or inline definitions. Inline tricksets are re-applied in place.
    cfg_entries = cfg.get("tricksets", [])
    if isinstance(cfg_entries, list):
        for entry in cfg_entries:
            if isinstance(entry, dict):
                name = entry.get("name")
                if not name:
                    continue
                if name in handler.tricksets:
                    handler.tricksets[name].reread_config(data=entry)
                else:
                    try:
                        handler.tricksets[name] = Trickset.from_dict(entry)
                    except Exception:
                        logging.getLogger("petsitter").exception("Failed to load inline trickset %r on reread", entry)
                        continue
                    summary["added"].append(name)
            elif isinstance(entry, str):
                try:
                    ts = Trickset.load_from_file(entry)
                except Exception:
                    logging.getLogger("petsitter").exception("Failed to load trickset %s on reread", entry)
                    continue
                if ts.name not in handler.tricksets:
                    handler.tricksets[ts.name] = ts
                    summary["added"].append(ts.name)

    return {
        "models": {
            "model_url": handler.model_url,
            "model_name": handler.model_name,
            "api_key_set": bool(handler.api_key),
            "modelset_keys": sorted(_modelset.keys()),
        },
        "tricksets": summary,
    }


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
    restore_saved: bool = False,
) -> Starlette:
    tricksets: dict[str, Trickset] = {}

    if trickset_paths:
        for tp in trickset_paths:
            if isinstance(tp, dict):
                ts = Trickset.from_dict(tp)
            else:
                ts = Trickset.load_from_file(tp)
            tricksets[ts.name] = ts

    # Restore the saved "_default" trickset so dashboard edits survive restarts.
    if restore_saved and "_default" not in tricksets:
        saved_default = TRICKSETS_DIR / "_default.json"
        if saved_default.exists():
            try:
                tricksets["_default"] = Trickset.load_from_file(str(saved_default))
            except Exception:
                logging.getLogger("petsitter").exception("Failed to restore saved _default trickset; falling back to defaults")

    if trick_paths:
        if "_default" in tricksets:
            existing = tricksets["_default"]
            for tp in trick_paths:
                if tp not in existing.trick_paths:
                    existing.add_trick(tp)
        else:
            default_ts = Trickset("_default", SCHEMA, {"X-Title": "*", "Model": "*"}, list(trick_paths), file_path=str(TRICKSETS_DIR / "_default.json"))
            default_ts.load_tricks()
            tricksets["_default"] = default_ts
    elif "_default" not in tricksets:
        default_ts = Trickset("_default", SCHEMA, {"X-Title": "*", "Model": "*"}, list(DEFAULT_TRICKS), file_path=str(TRICKSETS_DIR / "_default.json"))
        default_ts.load_tricks()
        tricksets["_default"] = default_ts

    handler = ProxyHandler(model_url, model_name, api_key, tricksets=tricksets)

    global _log_capture, _agent_manager
    _agent_manager = AgentManager(config_dir=str(CONFIG_DIR), handler=handler)
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
        # Tools are disconnected here, while the app is still shutting down
        # cleanly. _restore_agents records what it undid so the CLI can tell
        # the user on its way out.
        _restore_agents()

    app = Starlette(lifespan=lifespan)

    async def stream_chat_completions(handler: ProxyHandler, payload: dict, x_title: str, upstream_request_url: str = "", forward_headers: dict | None = None):
        try:
            result = await handler.chat_completions(payload, x_title=x_title, upstream_request_url=upstream_request_url, forward_headers=forward_headers)
            message = result["choices"][0]["message"]
            base = {
                "id": result.get("id", "chatcmpl-petsitter"),
                "object": "chat.completion.chunk",
                "created": result.get("created", __import__("time").time()),
                "model": result.get("model", "unknown"),
            }

            def emit(delta: dict, finish_reason: str | None = None) -> str:
                chunk = dict(base)
                chunk["choices"] = [{
                    "index": 0,
                    "delta": delta,
                    "finish_reason": finish_reason,
                }]
                return f"data: {json.dumps(chunk)}\n\n"

            yield emit({"role": "assistant", "content": ""})
            reasoning = message.get("reasoning_content")
            if reasoning:
                for part in _chunk_text(reasoning):
                    yield emit({"reasoning_content": part})
            content = message.get("content")
            if content:
                for part in _chunk_text(content):
                    yield emit({"content": part})
            if "tool_calls" in message and message["tool_calls"]:
                yield emit({"tool_calls": message["tool_calls"]})
            yield emit({}, finish_reason=result["choices"][0].get("finish_reason", "stop"))
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

    async def anthropic_messages(request: Request) -> Response:
        """Anthropic's Messages API, which is what ANTHROPIC_BASE_URL points at."""
        from petsitter import anthropic_compat as ac
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse(
                {"type": "error", "error": {"type": "invalid_request_error",
                                            "message": "body is not valid JSON"}},
                status_code=400)

        x_title = request.headers.get("X-Title", "")
        forward = dict(request.headers)
        streaming = bool(payload.get("stream", False))

        if streaming:
            async def event_stream():
                try:
                    result = await handler.messages(payload, x_title=x_title,
                                                    forward_headers=forward)
                except Exception as e:
                    logging.getLogger("petsitter").exception("/v1/messages failed")
                    yield ac.error_event(str(e))
                    return
                for chunk in ac.stream_events(result):
                    yield chunk
            return StreamingResponse(event_stream(), media_type="text/event-stream")

        try:
            result = await handler.messages(payload, x_title=x_title, forward_headers=forward)
        except Exception as e:
            logging.getLogger("petsitter").exception("/v1/messages failed")
            return JSONResponse(
                {"type": "error", "error": {"type": "api_error", "message": str(e)}},
                status_code=502)
        return JSONResponse(result)

    app.add_route("/v1/messages", anthropic_messages, methods=["POST"])

    async def health(request: Request) -> Response:
        return JSONResponse({"status": "ok"})
    app.add_route("/health", health, methods=["GET"])

    # ----- /p/ path-prefix transparent proxy -----
    # http://localhost:8080/p/<host>/<rest> proxies to https://<host>/<rest>
    # through the normal trick pipeline. Trickset selection relies on the
    # existing X-Title/Model filters; the client's Authorization header and
    # model field pass through to the upstream.

    async def proxy_p(request: Request) -> Response:
        parsed = _parse_p_path(request.url.path)
        if parsed is None:
            return JSONResponse({"error": "Invalid /p/ proxy target", "type": "invalid_request"}, status_code=400)
        host, rest = parsed

        if 'localhost' in host or '127.0.0.1' in host:
          upstream = f"http://{host}{rest}"
        else:
          upstream = f"https://{host}{rest}"

        forward_headers = {}
        if request.headers.get("authorization"):
            forward_headers["Authorization"] = request.headers["authorization"]
        x_title = request.headers.get("X-Title", "")
        logging.getLogger("petsitter").info("/p/ proxy -> %s (x_title=%r)", upstream, x_title)

        if request.method == "POST" and rest.endswith("/chat/completions"):
            try:
                payload = await request.json()
            except json.JSONDecodeError:
                return JSONResponse({"error": "Invalid JSON", "type": "invalid_request"}, status_code=400)
            if payload.get("stream", False):
                return StreamingResponse(
                    stream_chat_completions(handler, payload, x_title, upstream_request_url=upstream, forward_headers=forward_headers),
                    media_type="text/event-stream",
                )
            try:
                result = await handler.chat_completions(payload, x_title=x_title, upstream_request_url=upstream, forward_headers=forward_headers)
                return JSONResponse(result)
            except ValueError as e:
                return JSONResponse({"error": str(e), "type": "setup_required"}, status_code=503)
            except Exception as e:
                import traceback
                logging.getLogger("petsitter").error(f"Error in /p/ chat_completions: {e}\n{traceback.format_exc()}")
                return JSONResponse({"error": str(e), "type": "proxy_error"}, status_code=500)

        if request.method == "GET" and rest.endswith("/models"):
            try:
                result = await handler.models(upstream_url=upstream, forward_headers=forward_headers)
                return JSONResponse(result)
            except ValueError as e:
                return JSONResponse({"error": str(e), "type": "setup_required"}, status_code=503)
            except Exception as e:
                import traceback
                logging.getLogger("petsitter").error(f"Error in /p/ models: {e}\n{traceback.format_exc()}")
                return JSONResponse({"error": str(e), "type": "proxy_error"}, status_code=500)

        return await _generic_proxy(upstream, request)
    app.add_route("/p/{path:path}", proxy_p, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])

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

    async def get_trickset_log(request: Request) -> Response:
        name = request.path_params.get("name")
        ts = handler.tricksets.get(name)
        if not ts:
            return JSONResponse({"error": f"Trickset '{name}' not found"}, status_code=404)
        logfile = ts.logfile or _default_logfile(ts.name)
        lines: list[str] = []
        missing = not Path(logfile).exists()
        if not missing:
            limit = int(request.query_params.get("lines", "200"))
            try:
                with open(logfile, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.read().splitlines()[-limit:]
            except OSError:
                missing = True
        return JSONResponse({"name": name, "logfile": logfile, "missing": missing, "lines": lines})
    app.add_route("/api/tricksets/{name}/log", get_trickset_log, methods=["GET"])

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
        if not ts.file_path:
            ts.file_path = str(TRICKSETS_DIR / f"{ts.name}.json")
        if "filters" in data:
            ts.filters = data["filters"]
        if "tricks" in data:
            raw = data["tricks"]
            has_ids = all(isinstance(e, dict) and e.get("id") for e in raw)
            if has_ids:
                if ts.merge_tricks(raw):
                    ts.save()
            else:
                new_paths: list[str] = []
                new_enabled: list[bool] = []
                new_ids: list[str] = []
                new_keywords: list[str | None] = []
                new_configs: dict[str, dict] = {}
                for entry in raw:
                    if isinstance(entry, str):
                        new_paths.append(entry)
                        new_enabled.append(True)
                        new_ids.append(_new_id())
                        new_keywords.append(None)
                    elif isinstance(entry, dict):
                        eid = entry.get("id") or _new_id()
                        new_paths.append(entry.get("file", ""))
                        new_enabled.append(entry.get("enabled", True))
                        new_ids.append(eid)
                        new_keywords.append(entry.get("keyword"))
                        if isinstance(entry.get("config"), dict):
                            new_configs[eid] = dict(entry["config"])
                ts.trick_paths = new_paths
                ts.trick_enabled = new_enabled
                ts.trick_ids = new_ids
                ts.trick_keywords = new_keywords
                ts.trick_configs = new_configs
                ts.load_tricks()
                ts.save()
        if "parameters" in data:
            ts.parameters = dict(data["parameters"])
        if "models" in data:
            ts.models = dict(data["models"])
        if "logfile" in data:
            ts.logfile = data["logfile"] or _default_logfile(ts.name)
        if "loglevel" in data:
            ts.loglevel = str(data["loglevel"]).upper()
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

    async def readconfig(request: Request) -> Response:
        """Re-read config files and apply them to the running instance."""
        try:
            return JSONResponse(reload_config(handler))
        except Exception as e:
            import traceback
            logging.getLogger("petsitter").error(f"Error in /readconfig: {e}\n{traceback.format_exc()}")
            return JSONResponse({"error": str(e), "type": "proxy_error"}, status_code=500)
    app.add_route("/readconfig", readconfig, methods=["POST"])

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

    async def agent_trickset(request: Request) -> Response:
        """Create this agent's trickset if it doesn't have one yet."""
        if _agent_manager is None:
            return JSONResponse({"error": "Agent manager not initialized"}, status_code=500)
        agent_id = request.path_params.get("id")
        try:
            name, log = _agent_manager.ensure_trickset(agent_id)
            return JSONResponse({"success": True, "name": name, "log": log})
        except KeyError as e:
            return JSONResponse({"success": False, "error": str(e), "log": []}, status_code=404)
        except Exception as e:
            return JSONResponse({"success": False, "error": str(e), "log": []}, status_code=500)
    app.add_route("/api/agents/{id}/trickset", agent_trickset, methods=["POST"])

    async def shutdown_server(request: Request) -> Response:
        # Same should_exit path as Ctrl-C/SIGTERM/SIGHUP, not a hard os._exit --
        # this used to kill the process 0.5s after the request regardless of
        # what was in flight, which severed any live client mid-stream (a
        # connected Claude Code session reading a response through this proxy
        # would see the connection die mid-thinking-block and persist that as
        # a corrupt empty block in its own transcript). Setting should_exit
        # instead lets uvicorn's existing graceful-shutdown window drain
        # in-flight requests, and _restore_agents() runs once, naturally,
        # from the same post-run() cleanup path every other exit uses.
        handler.shutdown_all()
        if _uvicorn_server is not None:
            _uvicorn_server.should_exit = True
        return JSONResponse({"success": True, "message": "Shutting down"})
    app.add_route("/api/shutdown", shutdown_server, methods=["POST"])

    def _on_exit():
        handler.shutdown_all()
        # A backstop for exits that never reach the CLI's own cleanup: kill -TERM,
        # an unhandled error, the dashboard's shutdown button. unregister is
        # idempotent, so doing it twice is harmless.
        _restore_agents()

    atexit.register(_on_exit)

    return app


def _get_version() -> str:
    """Version of the running petsitter.

    Order matters.  Installed metadata is authoritative, because an installed
    wheel ships no pyproject.toml and ``git describe`` would otherwise run in
    whatever directory the user happens to be standing in and report some
    unrelated repo's tag.  The pyproject read covers a source checkout that
    was never pip-installed; git is the last resort.
    """
    try:
        return _pkg_version("petsitter")
    except PackageNotFoundError:
        pass

    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if pyproject.is_file():
        try:
            try:
                import tomllib
            except ModuleNotFoundError:      # Python 3.10
                import tomli as tomllib
            return tomllib.loads(pyproject.read_text()).get("project", {}).get("version", "0.0.0")
        except Exception:
            pass

    try:
        r = subprocess.run(
            ["git", "describe", "--tags", "--dirty", "--always"],
            cwd=str(Path(__file__).resolve().parents[2]),
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return "0.0.0"


@click.command()
@click.version_option(
    _get_version(),
    "-v", "--version",
    prog_name="petsitter",
)
@click.option(
    "-c", "--config",
    "config_arg",
    default=None,
    help="Path to a config file (e.g., another_petsitter_config.conf.json) or config "
         "directory (default: $PET_CONFIG_DIR or ~/.config/petsitter)",
)
@click.option(
    "-l", "--listen",
    "listen_on",
    default="localhost:8080",
    help="Host:port to listen on (default: localhost:8080)",
)
@click.option(
    "--no-browser",
    is_flag=True,
    help="Don't open the dashboard in a browser on startup.",
)
def cli(config_arg: str | None, listen_on: str, no_browser: bool) -> None:
    """Petsitter - OpenAI-compatible proxy with tricks.

    https://github.com/day50-dev/Petsitter

    Reads all settings from the config file at CONFIG_PATH (default
    ~/.config/petsitter/config.json, or the value of $PET_CONFIG_DIR).
    Point -c at a config file or directory to run from a different base.

    \b
        petsitter -l localhost:8080

    \b
        petsitter -c another_petsitter_config.conf.json -l localhost:8080
    """
    global CONFIG_DIR, CONFIG_PATH, TRICKSETS_DIR, BACKUPS_DIR

    if config_arg:
        p = Path(config_arg).expanduser().resolve()
        if p.suffix:
            CONFIG_DIR = p.parent
            CONFIG_PATH = p
        else:
            CONFIG_DIR = p
            CONFIG_PATH = p / "config.json"
    else:
        base = Path(os.environ.get("PET_CONFIG_DIR", str(Path.home() / ".config" / "petsitter"))).expanduser()
        CONFIG_DIR = base
        CONFIG_PATH = base / "config.json"
    TRICKSETS_DIR = CONFIG_DIR / "tricksets"
    BACKUPS_DIR = CONFIG_DIR / "backups"

    cfg = load_config()
    cfg_tricksets = list(cfg.get("tricksets", []))

    modelset_data = cfg.get("modelset") or {}
    if not isinstance(modelset_data, dict):
        modelset_data = {}

    configure_modelset(modelset_data)

    model_url = cfg.get("model_url", "")
    model_name = cfg.get("model_name", "")
    api_key = cfg.get("api_key", "")
    dflt = modelset_data.get("default")
    if isinstance(dflt, dict):
        if dflt.get("url"):
            model_url = dflt["url"]
        if isinstance(dflt.get("model"), str) and dflt["model"]:
            model_name = dflt["model"]
    model_url = model_url.rstrip("/")

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
        trick_paths=[],
        trickset_paths=cfg_tricksets,
        modelset_data=modelset_data,
        config_path=str(CONFIG_PATH),
        restore_saved=True,
    )

    # Save config for next run, preserving any keys we don't manage.
    cfg.update({
        "model_url": model_url,
        "model_name": model_name or "",
        "api_key": api_key,
        "modelset": modelset_data,
        "tricksets": cfg_tricksets,
    })
    save_config(cfg)

    from petsitter.agents import set_petsitter_url
    set_petsitter_url(f"http://{listen_on}")

    labels = [e if isinstance(e, str) else e.get("name", "<inline>") for e in cfg_tricksets]
    _print_startup(listen_on, model_url, model_name or "", str(CONFIG_PATH), labels)

    log_level = os.getenv("LOGLEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(levelname)s: %(message)s"
    )

    # Run the server object rather than uvicorn.run() so long-lived responses
    # can see should_exit and finish themselves. Without that, an open SSE
    # stream keeps uvicorn waiting until the graceful timeout expires, and
    # every force-cancelled response prints a full traceback on Ctrl-C.
    global _uvicorn_server
    config = uvicorn.Config(app, host=host, port=port, timeout_graceful_shutdown=5)
    _uvicorn_server = uvicorn.Server(config)

    # uvicorn only installs its own handler for SIGINT/SIGTERM (see its
    # HANDLED_SIGNALS). A closed terminal or an exiting login shell sends
    # SIGHUP instead, whose default disposition is an immediate kill -- no
    # unwind, no atexit, nothing restored. Route it through the same
    # should_exit flag so a dropped terminal shuts down exactly as cleanly
    # as Ctrl-C. SIGKILL can never be caught this way; that's what the
    # standalone `pet agents restore` command is for.
    if hasattr(signal, "SIGHUP"):
        def _handle_sighup(signum, frame):
            _uvicorn_server.should_exit = True
        signal.signal(signal.SIGHUP, _handle_sighup)

    if not no_browser:
        dashboard_url = f"http://{host if host != '0.0.0.0' else 'localhost'}:{port}/"

        def _open_when_ready():
            import time
            import webbrowser
            # uvicorn.Server sets .started only once it's actually accepting
            # connections; opening before that races the browser against the
            # bind and shows a connection-refused page instead of the dashboard.
            for _ in range(100):  # ~10s
                if getattr(_uvicorn_server, "started", False) or _uvicorn_server.should_exit:
                    break
                time.sleep(0.1)
            if _uvicorn_server.started and not _uvicorn_server.should_exit:
                webbrowser.open(dashboard_url)

        threading.Thread(target=_open_when_ready, daemon=True).start()

    try:
        _uvicorn_server.run()
    except KeyboardInterrupt:
        pass
    except SystemExit:
        # uvicorn calls sys.exit(1) from inside the loop when it cannot bind;
        # swallow it so the reason can be explained in words below.
        pass

    # uvicorn logs a bind failure and returns without raising, so "did it ever
    # start" is the only honest signal that the port was taken.
    if getattr(_uvicorn_server, "started", True) is False:
        _print_port_busy(host, port)
        raise SystemExit(1)

    _print_goodbye(_restore_agents())


if __name__ == "__main__":
    cli()

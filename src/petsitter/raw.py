"""The raw HTTP of the current request, as petsitter saw it: get_raw().

Two edges, recorded once each, so nothing in between has to remember to:

- the client's request: the request line as it arrived (before petsitter
  rewrites the path), its headers, and its body, copied as the server reads it,
  and petsitter's response to it: status, headers, body, copied as it's sent
  (server.py's middleware calls begin(), respond(), finish());
- every call petsitter makes to a provider while handling it: method, URL,
  request headers and body, status, response headers, timing, and the
  response body, streams included, copied as it passes through. Upstream
  clients come from async_client() / sync_client(), whose transport records.

Bodies are kept as bytes, decompressed if the provider sent them gzipped or
deflated. The record lives as long as the request; a trick that wants any of
it later keeps its own copy. When an upstream call finishes, a "raw_upstream"
pipeline event says so, which reaches subscribers even when the call failed
and no post_hook will run.
"""

import contextvars
import time
import zlib

import httpx

from petsitter.observability import trace_event

_raw: contextvars.ContextVar = contextvars.ContextVar("petsitter_raw", default=None)


def begin(line: str, headers: list, client: str, http_version: str = "1.1") -> tuple:
    """Start recording a request; returns (token, record). The caller copies
    the body in with add_body() as it's received, and petsitter's response
    with respond() / add_response_body() / finish()."""
    record = {"request": {"line": line, "http_version": http_version, "headers": [list(h) for h in headers],
                         "from": client, "at": time.time(), "body": bytearray()},
              "response": {"status": None, "headers": [], "body": bytearray(), "done": False,
                           # how petsitter's response ended (see finish()), and when
                           "ended": None, "error": None, "first_byte_ms": None, "last_byte_ms": None,
                           "ms": None, "client_gone_ms": None},
              "upstream": [], "_t0": time.monotonic(), "_on_finish": []}
    return _raw.set(record), record


def _ms(record: dict) -> int:
    return round((time.monotonic() - record["_t0"]) * 1000)


def add_body(record: dict, chunk: bytes) -> None:
    record["request"]["body"].extend(chunk or b"")


def respond(record: dict, status: int, headers: list) -> None:
    record["response"]["status"] = status
    record["response"]["headers"] = [list(h) for h in headers]


def add_response_body(record: dict, chunk: bytes, more_body: bool = True) -> None:
    resp = record["response"]
    resp["body"].extend(chunk or b"")
    now = _ms(record)
    if chunk:
        if resp["first_byte_ms"] is None:
            resp["first_byte_ms"] = now
        resp["last_byte_ms"] = now
    if not more_body:
        resp["ended"] = "complete"


def client_gone(record: dict) -> None:
    """The client disconnected (or its socket failed under a send)."""
    resp = record["response"]
    if resp["client_gone_ms"] is None and resp["ended"] != "complete":
        resp["client_gone_ms"] = _ms(record)


def failed(record: dict, error: BaseException) -> None:
    """petsitter raised while answering: the exception and its traceback."""
    import traceback
    resp = record["response"]
    if resp["error"] is None:
        resp["error"] = "".join(traceback.format_exception(type(error), error, error.__traceback__)).strip()


def on_finish(record: dict, fn) -> None:
    """Call fn(record) once the response is over, however it ended."""
    if record["response"]["done"]:
        fn(record)
    else:
        record["_on_finish"].append(fn)


def finish(record: dict) -> None:
    """The response is over. How it ended, in record["response"]["ended"]:
    "complete" (the last byte went out), "client disconnected" (before
    that), "error" (petsitter raised; the traceback is in "error"), or
    "incomplete" (none of those, yet it stopped short)."""
    resp = record["response"]
    resp["done"] = True
    resp["ms"] = _ms(record)
    if resp["error"]:
        resp["ended"] = "error"   # even if the stream closed cleanly: it carried an error event
    elif resp["ended"] != "complete":
        resp["ended"] = "client disconnected" if resp["client_gone_ms"] is not None else "incomplete"
    for fn in record.pop("_on_finish", []):
        try:
            fn(record)
        except Exception:
            import logging
            logging.getLogger("petsitter").exception("a raw-record subscriber failed")


def current() -> dict | None:
    """The live record itself (not a copy), which keeps filling in until the
    request is answered. For a trick that holds on to it past its hooks."""
    return _raw.get()

def end(token) -> None:
    _raw.reset(token)


def _decoded(body: bytes, headers: list) -> bytes:
    encoding = next((v for k, v in headers if k.lower() == "content-encoding"), "").lower()
    try:
        if encoding == "gzip":
            return zlib.decompress(body, 31)
        if encoding == "deflate":
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -15)
    except zlib.error:
        pass
    return body


def get_raw() -> dict | None:
    """The current request's raw HTTP, or None outside a request:

    {"request": {"line", "http_version", "headers", "from", "at", "body"},
     "response": {"status", "headers", "body", "done", "ended", "error",   (petsitter's, to the client)
                  "first_byte_ms", "last_byte_ms", "ms", "client_gone_ms"},
     "upstream": [{"method", "url", "request_headers", "request_body", "at",
                   "status", "headers", "first_byte_ms", "last_byte_ms", "ms", "done",
                   "ended", "error", "body"}, ...]}

    "ended" says how a response stopped (see finish() and _finish()); times
    are milliseconds from the start of the request or the call.

    Headers are [name, value] pairs, repeats kept. Bodies are bytes.
    """
    record = _raw.get()
    if record is None:
        return None
    request = dict(record["request"], body=bytes(record["request"]["body"]))
    response = dict(record["response"], body=bytes(record["response"]["body"]))
    upstream = []
    for ex in record["upstream"]:
        item = {k: v for k, v in ex.items() if not k.startswith("_") and k != "body"}
        item["body"] = _decoded(bytes(ex["body"]), ex.get("headers") or [])
        upstream.append(item)
    return {"request": request, "response": response, "upstream": upstream}


# -- reading an event stream ------------------------------------------------------

def stream_report(text: str, keep_last: int = 3) -> dict:
    """What an SSE body says, for diagnosing a reply that went wrong: how many
    events and heartbeat comments, whether it ended with [DONE] (or Anthropic's
    message_stop), the finish or stop reason, usage, how much text and
    reasoning, each tool call (rebuilt the way petsitter does: a name starts a
    call, pieces without one extend it) with whether its arguments parse as
    JSON, any error events, and the last few events verbatim."""
    import json
    out: dict = {"events": 0, "comments": 0, "done": False, "finish_reason": None, "usage": None,
                 "content_chars": 0, "reasoning_chars": 0, "tool_calls": [], "errors": [], "last_events": []}
    calls: list[dict] = []
    last: list[str] = []
    for line in text.splitlines():
        if line.startswith(":"):
            out["comments"] += 1
            continue
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        last.append(data if len(data) <= 600 else data[:600] + f"... ({len(data):,} chars)")
        del last[:-keep_last]
        if data == "[DONE]":
            out["done"] = True
            continue
        out["events"] += 1
        try:
            ev = json.loads(data)
        except ValueError:
            out["errors"].append(f"not JSON: {data[:200]}")
            continue
        if not isinstance(ev, dict):
            continue
        if "error" in ev or ev.get("type") == "error":
            out["errors"].append(ev.get("error", ev))
        if ev.get("usage"):
            out["usage"] = {**(out["usage"] or {}), **ev["usage"]}
        for c in ev.get("choices") or []:                       # OpenAI chunks
            if not isinstance(c, dict):
                continue
            if c.get("finish_reason"):
                out["finish_reason"] = c["finish_reason"]
            d = c.get("delta") or c.get("message") or {}
            if isinstance(d.get("content"), str):
                out["content_chars"] += len(d["content"])
            for k in ("reasoning_content", "reasoning", "thinking"):
                if isinstance(d.get(k), str):
                    out["reasoning_chars"] += len(d[k])
            for piece in d.get("tool_calls") or []:
                fn = (piece or {}).get("function") or {}
                if fn.get("name"):
                    calls.append({"name": fn["name"], "id": piece.get("id"), "arguments": fn.get("arguments") or ""})
                elif calls and isinstance(fn.get("arguments"), str):
                    calls[-1]["arguments"] += fn["arguments"]
                if calls and piece.get("id") and not calls[-1].get("id"):
                    calls[-1]["id"] = piece["id"]
        t = ev.get("type")                                       # Anthropic events
        if t == "message_stop":
            out["done"] = True
        elif t == "message_delta":
            out["finish_reason"] = (ev.get("delta") or {}).get("stop_reason") or out["finish_reason"]
        elif t == "content_block_start":
            block = ev.get("content_block") or {}
            if block.get("type") == "tool_use":
                calls.append({"name": block.get("name"), "id": block.get("id"), "arguments": ""})
        elif t == "content_block_delta":
            delta = ev.get("delta") or {}
            if delta.get("type") == "text_delta":
                out["content_chars"] += len(delta.get("text") or "")
            elif delta.get("type") == "thinking_delta":
                out["reasoning_chars"] += len(delta.get("thinking") or "")
            elif delta.get("type") == "input_json_delta" and calls:
                calls[-1]["arguments"] += delta.get("partial_json") or ""
    for c in calls:
        args = c.pop("arguments")
        try:
            json.loads(args or "{}")
            ok = True
        except ValueError:
            ok = False
        out["tool_calls"].append({**c, "arguments_chars": len(args), "arguments_json": ok})
    out["last_events"] = last
    return out


# -- recording transports -------------------------------------------------------

def _start(request: httpx.Request) -> dict:
    try:
        body = request.content
    except httpx.RequestNotRead:
        body = b""
    ex = {"method": request.method, "url": str(request.url),
          "request_headers": [[k, v] for k, v in request.headers.multi_items()],
          "request_body": body, "at": time.time(), "_t0": time.monotonic(),
          "status": None, "headers": [], "body": bytearray(), "done": False}
    record = _raw.get()
    if record is not None:
        record["upstream"].append(ex)
    return ex


def _respond(ex: dict, response: httpx.Response) -> None:
    ex["status"] = response.status_code
    version = (response.extensions or {}).get("http_version", b"HTTP/1.1")
    ex["http_version"] = version.decode() if isinstance(version, bytes) else str(version)
    ex["headers"] = [[k, v] for k, v in response.headers.multi_items()]
    ex["first_byte_ms"] = round((time.monotonic() - ex["_t0"]) * 1000)


def _finish(ex: dict, error: str | None = None) -> None:
    """The call is over. ex["ended"]: "complete" (the provider's body ran to
    its end), "closed early" (petsitter stopped reading before the end), or
    "error" (the connection or the read failed; see ex["error"])."""
    if ex["done"]:
        return
    ex["done"] = True
    ex["ms"] = round((time.monotonic() - ex["_t0"]) * 1000)
    if error:
        ex["error"] = error
    ex["ended"] = "error" if error else "complete" if ex.get("_eof") else "closed early"
    trace_event("raw_upstream", url=ex["url"], status=ex["status"], error=ex.get("error"))


def _got(ex: dict, chunk: bytes) -> None:
    ex["body"].extend(chunk)
    ex["last_byte_ms"] = round((time.monotonic() - ex["_t0"]) * 1000)


class _AsyncTee(httpx.AsyncByteStream):
    def __init__(self, inner, ex):
        self._inner, self._ex = inner, ex

    async def __aiter__(self):
        try:
            async for chunk in self._inner:
                _got(self._ex, chunk)
                yield chunk
            self._ex["_eof"] = True
        except Exception as e:
            _finish(self._ex, f"{type(e).__name__}: {e}")
            raise

    async def aclose(self):
        try:
            await self._inner.aclose()
        finally:
            _finish(self._ex)


class _SyncTee(httpx.SyncByteStream):
    def __init__(self, inner, ex):
        self._inner, self._ex = inner, ex

    def __iter__(self):
        try:
            for chunk in self._inner:
                _got(self._ex, chunk)
                yield chunk
            self._ex["_eof"] = True
        except Exception as e:
            _finish(self._ex, f"{type(e).__name__}: {e}")
            raise

    def close(self):
        try:
            self._inner.close()
        finally:
            _finish(self._ex)


class _AsyncTap(httpx.AsyncBaseTransport):
    def __init__(self, inner=None):
        self._inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request):
        ex = _start(request)
        try:
            response = await self._inner.handle_async_request(request)
        except Exception as e:
            _finish(ex, f"{type(e).__name__}: {e}")
            raise
        _respond(ex, response)
        return httpx.Response(response.status_code, headers=response.headers,
                              stream=_AsyncTee(response.stream, ex),
                              extensions=response.extensions, request=request)

    async def aclose(self):
        await self._inner.aclose()


class _SyncTap(httpx.BaseTransport):
    def __init__(self, inner=None):
        self._inner = inner or httpx.HTTPTransport()

    def handle_request(self, request):
        ex = _start(request)
        try:
            response = self._inner.handle_request(request)
        except Exception as e:
            _finish(ex, f"{type(e).__name__}: {e}")
            raise
        _respond(ex, response)
        return httpx.Response(response.status_code, headers=response.headers,
                              stream=_SyncTee(response.stream, ex),
                              extensions=response.extensions, request=request)

    def close(self):
        self._inner.close()


# How long petsitter waits on a provider, to connect or for an answer. How
# slow a model is isn't petsitter's call: a model can take minutes to answer,
# or to accept a connection while it finishes another request, and the tool on
# the other end has its own timeouts. This is only a backstop, so a connection
# that never answers is eventually let go.
DEFAULT_TIMEOUT_MINUTES = 30
_timeout = httpx.Timeout(DEFAULT_TIMEOUT_MINUTES * 60.0)


def upstream_timeout() -> httpx.Timeout:
    """How long to wait on a provider (petsitter's Settings page)."""
    return _timeout


def set_upstream_timeout(minutes: float) -> None:
    global _timeout
    if not minutes or minutes <= 0:
        raise ValueError("The timeout is a number of minutes above 0")
    _timeout = httpx.Timeout(float(minutes) * 60.0)


def async_client(**kw) -> httpx.AsyncClient:
    """An httpx.AsyncClient for calls to a provider, recorded for get_raw().
    A ``transport`` given is wrapped, so it's recorded too."""
    kw.setdefault("timeout", upstream_timeout())
    kw["transport"] = _AsyncTap(kw.get("transport"))
    return httpx.AsyncClient(**kw)


def sync_client(**kw) -> httpx.Client:
    """An httpx.Client for calls to a provider, recorded for get_raw()."""
    kw.setdefault("timeout", upstream_timeout())
    kw["transport"] = _SyncTap(kw.get("transport"))
    return httpx.Client(**kw)

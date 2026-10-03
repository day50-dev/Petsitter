"""The raw HTTP of the current request, as petsitter saw it: get_raw().

Two edges, recorded once each, so nothing in between has to remember to:

- the client's request: the request line as it arrived (before petsitter
  rewrites the path), its headers, and its body, copied as the server reads it
  (server.py's middleware calls begin());
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


def begin(line: str, headers: list, client: str) -> tuple:
    """Start recording a request; returns (token, record). The caller copies
    the body in with add_body() as it's received."""
    record = {"request": {"line": line, "headers": [list(h) for h in headers], "from": client,
                          "at": time.time(), "body": bytearray()},
              "upstream": []}
    return _raw.set(record), record


def add_body(record: dict, chunk: bytes) -> None:
    record["request"]["body"].extend(chunk or b"")


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

    {"request": {"line", "headers", "from", "at", "body"},
     "upstream": [{"method", "url", "request_headers", "request_body", "at",
                   "status", "headers", "first_byte_ms", "ms", "done",
                   "error", "body"}, ...]}

    Headers are [name, value] pairs, repeats kept. Bodies are bytes.
    """
    record = _raw.get()
    if record is None:
        return None
    request = dict(record["request"], body=bytes(record["request"]["body"]))
    upstream = []
    for ex in record["upstream"]:
        item = {k: v for k, v in ex.items() if k not in ("_t0", "body")}
        item["body"] = _decoded(bytes(ex["body"]), ex.get("headers") or [])
        upstream.append(item)
    return {"request": request, "upstream": upstream}


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
    ex["headers"] = [[k, v] for k, v in response.headers.multi_items()]
    ex["first_byte_ms"] = round((time.monotonic() - ex["_t0"]) * 1000)


def _finish(ex: dict, error: str | None = None) -> None:
    if ex["done"]:
        return
    ex["done"] = True
    ex["ms"] = round((time.monotonic() - ex["_t0"]) * 1000)
    if error:
        ex["error"] = error
    trace_event("raw_upstream", url=ex["url"], status=ex["status"], error=ex.get("error"))


class _AsyncTee(httpx.AsyncByteStream):
    def __init__(self, inner, ex):
        self._inner, self._ex = inner, ex

    async def __aiter__(self):
        try:
            async for chunk in self._inner:
                self._ex["body"].extend(chunk)
                yield chunk
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
                self._ex["body"].extend(chunk)
                yield chunk
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


def async_client(**kw) -> httpx.AsyncClient:
    """An httpx.AsyncClient for calls to a provider, recorded for get_raw().
    A ``transport`` given is wrapped, so it's recorded too."""
    kw["transport"] = _AsyncTap(kw.get("transport"))
    return httpx.AsyncClient(**kw)


def sync_client(**kw) -> httpx.Client:
    """An httpx.Client for calls to a provider, recorded for get_raw()."""
    kw["transport"] = _SyncTap(kw.get("transport"))
    return httpx.Client(**kw)

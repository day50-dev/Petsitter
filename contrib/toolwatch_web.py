#!/usr/bin/env python3
"""toolwatch_web - a browser view of what tools petsitter hands the model.

Same trick, same transport, different front end. It binds the identical unix
datagram socket contrib/toolwatch.py listens on and decodes the identical
JSON events the Tool Monitor trick publishes -- it just draws them as a page
instead of curses, because a sortable list, a detail pane, and a search box
are all easier to get right in HTML than in a terminal. Shares no code with
petsitter and imports nothing from it -- only from toolwatch.py itself, for
the socket listener and the demo generator, which are already standalone. The
page itself is read from src/petsitter/tricks/tool_monitor.html, the file the
dashboard shows in Tool Monitor's Live tab, so there is one copy of it.

    ./contrib/toolwatch_web.py                  # listen, serve on :8090
    ./contrib/toolwatch_web.py --demo           # synthesize a session and watch it
    ./contrib/toolwatch_web.py --port 9000 --socket /tmp/tm.sock

All the state assembly (Store.apply, sorting, search, the unused-run
collapsing) is reimplemented in the page's own JavaScript rather than shared
with toolwatch.py's Python -- each browser tab is its own subscriber to the
same event feed, exactly the way the curses UI is, just running in a
different place.
"""

import argparse
import json
import queue
import sys
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from toolwatch import DEFAULT_SOCKET_PATH, demo_sender, listener  # noqa: E402

MAX_BACKLOG = 2000


class Hub:
    """Fans decoded events out to every connected browser tab.

    toolwatch.py's curses UI has exactly one reader for the socket's events,
    so a single queue.Queue is enough. The web version can have any number of
    tabs open at once, so each subscriber gets its own queue, and a bounded
    backlog is replayed to a tab that connects mid-session -- otherwise it
    would sit on a blank page until the next event happened to arrive.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue] = []
        self.backlog: deque = deque(maxlen=MAX_BACKLOG)

    def publish(self, event: dict) -> None:
        self.backlog.append(event)
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            q.put(event)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subscribers.append(q)
        for event in self.backlog:
            q.put(event)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)


def pump(sink: queue.Queue, hub: Hub, stop: threading.Event) -> None:
    """Move events off the socket-listener's queue and onto the hub's fan-out."""
    while not stop.is_set():
        try:
            event = sink.get(timeout=0.25)
        except queue.Empty:
            continue
        hub.publish(event)


# The page is shared with petsitter's Tool Monitor, which shows it in the
# dashboard's Live tab. Read, not imported: this script still depends on
# nothing from petsitter.
PAGE_PATH = Path(__file__).resolve().parent.parent / "src" / "petsitter" / "tricks" / "tool_monitor.html"
PAGE = PAGE_PATH.read_text(encoding="utf-8")



class Handler(BaseHTTPRequestHandler):
    hub: Hub | None = None  # set in main() before the server starts
    tag = "toolwatch"

    def log_message(self, format, *args):  # noqa: A002 - matches base signature
        pass  # a local dev tool doesn't need an access log on stderr

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._serve_page()
        elif self.path == "/events":
            self._serve_sse()
        else:
            self.send_error(404)

    def _serve_page(self):
        body = (PAGE.replace("__TAG__", self.tag).replace("__DEMO_BTN__", "")
                .replace("__EMPTY_HINT__", "").encode("utf-8"))
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = self.hub.subscribe()
        try:
            while True:
                try:
                    event = q.get(timeout=15)
                    self.wfile.write(f"data: {json.dumps(event)}\n\n".encode("utf-8"))
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.hub.unsubscribe(q)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Browser view of the tools petsitter offers, withholds, and fires.")
    parser.add_argument("--socket", default=str(DEFAULT_SOCKET_PATH),
                        help="unix datagram socket to bind (default: %(default)s)")
    parser.add_argument("--demo", action="store_true",
                        help="generate synthetic agentic traffic through the real socket")
    parser.add_argument("--host", default="127.0.0.1", help="address to serve on")
    parser.add_argument("--port", type=int, default=8090, help="port to serve on")
    parser.add_argument("--no-browser", action="store_true",
                        help="don't open a browser tab on startup")
    args = parser.parse_args(argv)

    sink: queue.Queue = queue.Queue()
    stop = threading.Event()
    hub = Hub()

    threads = [
        threading.Thread(target=listener, args=(args.socket, sink, stop), daemon=True),
        threading.Thread(target=pump, args=(sink, hub, stop), daemon=True),
    ]
    if args.demo:
        threads.append(threading.Thread(target=demo_sender, args=(args.socket, stop), daemon=True))
    for thread in threads:
        thread.start()

    Handler.hub = hub
    Handler.tag = "DEMO" if args.demo else args.socket
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"toolwatch web listening on {url}  (ctrl-c to stop)")

    if not args.no_browser:
        import threading as _threading
        import webbrowser

        def _open():
            import time
            time.sleep(0.3)
            webbrowser.open(url)
        _threading.Thread(target=_open, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.shutdown()
        for thread in threads:
            thread.join(timeout=1.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())

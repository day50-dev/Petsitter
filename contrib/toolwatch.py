#!/usr/bin/env python3
"""toolwatch - a live view of what tools petsitter hands the model.

A standalone viewer for the Tool Monitor trick.  It binds a unix datagram
socket, decodes the JSON events the trick publishes, and draws them.  It shares
no code with petsitter and imports nothing from it: the trick's job is to say
what happened, this program's job is to draw it, and either can be replaced
without touching the other.

    ./contrib/toolwatch.py                 # listen on the default socket
    ./contrib/toolwatch.py --demo          # synthesize a session and watch it
    ./contrib/toolwatch.py --socket /tmp/tm.sock
    ./contrib/toolwatch.py --raw           # dump JSON instead of drawing

The panel worth looking at is "gate".  Each withheld tool names the trick that
withheld it, and any reason that trick chose to report appears above the list.  Agentic flows often withhold tools per
step so the model is walked through a flow chart instead of being handed every
capability at once; the gate panel shows, for one request, which tools were
offered, which were withheld by another trick, and which the model then fired.

Sorting cycles with s: first seen, most fired, most withheld, most offered,
fire rate, name. The count being sorted on replaces the states strip.

Keys:  j/k or arrows  select tool        pgup/pgdn  page (ctrl-b/ctrl-f too)
       g / G          first / last      /          search, then n and N
       \\              filter to matches esc        clear the search
       s or enter     cycle sort        f          follow
       r              raw event log     c          clear      q  quit
"""

import argparse
import curses
import itertools
import json
import os
import queue
import random
import socket
import sys
import threading
import time
from collections import OrderedDict, deque
from datetime import datetime
from pathlib import Path

DEFAULT_SOCKET_PATH = Path.home() / ".cache" / "petsitter" / "toolmon.sock"
MAX_REQUESTS = 400
MAX_HISTORY = 200

# The cycle behind the sort key. "seen" first so a press always returns to the
# order that does not move.
SORT_ORDERS = [
    ("seen", "first seen"),
    ("fired", "most fired"),
    ("withheld", "most withheld"),
    ("offered", "most offered"),
    ("rate", "fire rate"),
    ("name", "name"),
]
RECV_SIZE = 65536

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# state -> (glyph, color key, label)
STATES = {
    "fired":    ("▸", "fired",    "fired"),
    "unused":   ("·", "unused",   "offered, unused"),
    "withheld": ("⊘", "withheld", "withheld"),
    "added":    ("+",      "added",    "added by a trick"),
}


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

class Store:
    """Everything the UI draws, assembled from the event stream.

    The tool is the unit of interest, not the request: the question a gate
    raises is "when is write_file ever available, and who keeps taking it
    away", which is a question about one tool across many requests.  So each
    tool carries its own history, and requests are what that history is
    indexed by.
    """

    def __init__(self):
        self.tools = OrderedDict()      # name -> tool record, in first-seen order
        self.requests = OrderedDict()   # id -> summary, for context
        self.events = 0
        self.dropped = 0
        self.fired_total = 0
        self.withheld_total = 0
        self.last_event_at = 0.0

    def _tool(self, name: str) -> dict:
        tool = self.tools.get(name)
        if tool is None:
            tool = {
                "name": name,
                "description": "",
                "offered": 0,
                "withheld": 0,
                "fired": 0,
                "unused": 0,
                "history": deque(maxlen=MAX_HISTORY),
            }
            self.tools[name] = tool
        return tool

    def apply(self, event: dict) -> None:
        self.events += 1
        self.last_event_at = time.time()
        if event.get("truncated"):
            self.dropped += 1

        rid = event.get("request_id") or "?"
        request = self.requests.get(rid)
        if request is None:
            request = {"id": rid, "started": time.time(), "model": "", "x_title": "",
                       "conversation": "", "offered": [], "fired": 0, "withheld": 0,
                       "done": False}
            self.requests[rid] = request
            while len(self.requests) > MAX_REQUESTS:
                self.requests.popitem(last=False)

        if event.get("event") == "request":
            request["model"] = event.get("model", "")
            request["x_title"] = event.get("x_title", "")
            request["conversation"] = event.get("conversation", "")
            names = []
            for entry in event.get("offered") or []:
                if isinstance(entry, dict):
                    name = entry.get("name", "?")
                    if entry.get("description"):
                        self._tool(name)["description"] = entry["description"]
                else:
                    name = str(entry)
                self._tool(name)
                names.append(name)
            request["offered"] = names

        elif event.get("event") == "response":
            request["done"] = True
            offered = list(event.get("offered") or request["offered"])
            withheld = set(event.get("withheld") or [])
            added = list(event.get("added") or [])

            fired_args = {}
            for call in event.get("fired") or []:
                if isinstance(call, dict) and call.get("name"):
                    fired_args.setdefault(call["name"], call.get("arguments", ""))

            blamed, reasons = {}, {}
            for entry in event.get("by_trick") or []:
                for name in entry.get("withheld") or []:
                    blamed.setdefault(name, entry.get("trick", "?"))
            for note in event.get("notes") or []:
                if note.get("reason") and note.get("trick"):
                    reasons[note["trick"]] = note["reason"]

            request["fired"] = len(fired_args)
            request["withheld"] = len(withheld)

            for name in offered + [n for n in added if n not in offered]:
                tool = self._tool(name)
                if name in fired_args:
                    state = "fired"
                elif name in withheld:
                    state = "withheld"
                else:
                    state = "unused"
                tool[state] += 1
                tool["offered"] += 1
                by = blamed.get(name, "")
                tool["history"].appendleft({
                    "request_id": rid,
                    "at": time.time(),
                    "state": state,
                    "by": by,
                    "reason": reasons.get(by, ""),
                    "args": fired_args.get(name, ""),
                    "x_title": request["x_title"],
                })
                if state == "fired":
                    self.fired_total += 1
                elif state == "withheld":
                    self.withheld_total += 1

    def ordered_tools(self, order: str = "seen") -> list:
        """Tools in the requested order.

        "seen" is first-seen order and is the default because it is the only
        one that holds still while events arrive; every other order can move a
        row out from under the cursor, so they are a deliberate keypress.
        """
        tools = list(self.tools.values())
        if order == "fired":
            tools.sort(key=lambda t: (-t["fired"], -t["offered"], t["name"]))
        elif order == "withheld":
            tools.sort(key=lambda t: (-t["withheld"], -t["offered"], t["name"]))
        elif order == "offered":
            tools.sort(key=lambda t: (-t["offered"], -t["fired"], t["name"]))
        elif order == "rate":
            # most-used-when-available first; a tool never offered has no rate
            tools.sort(key=lambda t: (-(t["fired"] / t["offered"]) if t["offered"] else 1,
                                      -t["fired"], t["name"]))
        elif order == "name":
            tools.sort(key=lambda t: t["name"])
        return tools

    def most_active_index(self, tools: list) -> int:
        """Index of the tool that most recently fired, else most recently seen.

        Every tool in a request gets a history entry at the same instant, so
        "most recent" alone lands on an arbitrary one. Preferring a tool that
        actually fired is what makes follow mode track the action.
        """
        best_fired = best_any = 0
        newest_fired = newest_any = 0.0
        for index, tool in enumerate(tools):
            if not tool["history"]:
                continue
            entry = tool["history"][0]
            if entry["at"] > newest_any:
                newest_any, best_any = entry["at"], index
            if entry["state"] == "fired" and entry["at"] > newest_fired:
                newest_fired, best_fired = entry["at"], index
        return best_fired if newest_fired else best_any

    def recent_requests(self) -> list:
        return list(reversed(self.requests.values()))

    def strip(self, tool: dict, width: int) -> list:
        """Recent states oldest-to-newest, as (glyph, state) for colouring."""
        recent = list(tool["history"])[:width]
        return [(STATES[h["state"]][0], h["state"]) for h in reversed(recent)]



# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------

def listener(path: str, sink: queue.Queue, stop: threading.Event) -> None:
    """Bind the datagram socket and push decoded events onto the queue."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        try:
            target.unlink()
        except OSError:
            pass

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.bind(str(target))
        sock.settimeout(0.25)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
        except OSError:
            pass
        while not stop.is_set():
            try:
                blob, _ = sock.recvfrom(RECV_SIZE)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                sink.put(json.loads(blob.decode("utf-8", "replace")))
            except (ValueError, UnicodeDecodeError):
                continue
    finally:
        sock.close()
        try:
            target.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------
# demo
# --------------------------------------------------------------------------

DEMO_TOOLS = [
    ("read_file",    "Read a file from the workspace"),
    ("list_dir",     "List the entries of a directory"),
    ("grep",         "Search the workspace for a pattern"),
    ("write_file",   "Create or overwrite a file"),
    ("apply_patch",  "Apply a unified diff to a file"),
    ("run_tests",    "Run the project's test suite"),
    ("git_commit",   "Commit the staged changes"),
    ("git_push",     "Push commits to the remote"),
    ("web_search",   "Search the web"),
    ("send_email",   "Send an email on the user's behalf"),
]

# the "adventure game": each phase unlocks a different slice of the tool belt
DEMO_PHASES = [
    ("explore", ["read_file", "list_dir", "grep", "web_search"]),
    ("plan",    ["read_file", "grep"]),
    ("edit",    ["read_file", "write_file", "apply_patch"]),
    ("verify",  ["run_tests", "read_file"]),
    ("ship",    ["git_commit", "git_push"]),
]


def demo_sender(path: str, stop: threading.Event) -> None:
    """Drive synthetic traffic through the real socket, as the trick would."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.setblocking(False)
    everything = [
        {"type": "function", "function": {"name": n, "description": d}}
        for n, d in DEMO_TOOLS
    ]
    all_names = [n for n, _ in DEMO_TOOLS]

    def send(record):
        try:
            sock.sendto(json.dumps(record).encode(), path)
        except OSError:
            pass

    time.sleep(0.4)
    conversation = "%08x" % random.getrandbits(32)
    phase_cycle = itertools.cycle(DEMO_PHASES)
    counter = 0

    while not stop.is_set():
        phase, allowed = next(phase_cycle)
        counter += 1
        rid = "%08x" % random.getrandbits(32)
        if counter % len(DEMO_PHASES) == 1 and counter > 1:
            conversation = "%08x" % random.getrandbits(32)

        send({
            "v": 1, "ts": datetime.now().isoformat(), "event": "request",
            "request_id": rid, "conversation": conversation,
            "x_title": f"agent/{phase}", "model": "qwen3-coder-30b",
            "stream": True, "messages": 4 + counter * 2,
            "offered": [
                {"name": t["function"]["name"], "description": t["function"]["description"]}
                for t in everything
            ],
            "results": [],
        })

        # the model "thinks" for a beat, which is the whole point of two events
        for _ in range(random.randint(4, 11)):
            if stop.is_set():
                return
            time.sleep(0.1)

        fired = random.sample(allowed, k=min(len(allowed), random.randint(1, 2)))
        send({
            "v": 1, "ts": datetime.now().isoformat(), "event": "response",
            "request_id": rid, "conversation": conversation,
            "model": "qwen3-coder-30b",
            "offered": all_names,
            "final": allowed,
            "withheld": [n for n in all_names if n not in allowed],
            "added": [],
            "fired": [
                {"name": n, "id": "call_%04x" % random.getrandbits(16),
                 "arguments": json.dumps({"path": random.choice(
                     ["src/petsitter/proxy.py", "README.md", "tests/"])})}
                for n in fired
            ],
            "by_trick": [{
                "trick": "PhaseGateTrick",
                "withheld": [n for n in all_names if n not in allowed],
                "added": [],
            }],
            "notes": [{
                "stage": "gate", "trick": "PhaseGateTrick",
                "reason": f"phase={phase}",
            }],
            "finish": "tool_calls",
        })
        for _ in range(random.randint(3, 8)):
            if stop.is_set():
                return
            time.sleep(0.1)


# --------------------------------------------------------------------------
# drawing
# --------------------------------------------------------------------------

class Palette:
    def __init__(self):
        self.on = False
        self.pairs = {}

    def setup(self):
        if not curses.has_colors():
            return
        curses.start_color()
        try:
            curses.use_default_colors()
            background = -1
        except curses.error:
            background = curses.COLOR_BLACK
        spec = [
            ("frame",    curses.COLOR_CYAN),
            ("fired",    curses.COLOR_GREEN),
            ("withheld", curses.COLOR_RED),
            ("added",    curses.COLOR_YELLOW),
            ("unused",   curses.COLOR_WHITE),
            ("accent",   curses.COLOR_MAGENTA),
            ("title",    curses.COLOR_BLUE),
        ]
        for index, (name, color) in enumerate(spec, start=1):
            try:
                curses.init_pair(index, color, background)
                self.pairs[name] = curses.color_pair(index)
            except curses.error:
                self.pairs[name] = 0
        self.on = True

    def __call__(self, name, bold=False, dim=False):
        attr = self.pairs.get(name, 0)
        if bold:
            attr |= curses.A_BOLD
        if dim:
            attr |= curses.A_DIM
        return attr


def put(win, y, x, text, attr=0):
    """addstr that clips instead of raising at the screen edge."""
    height, width = win.getmaxyx()
    if y < 0 or y >= height or x >= width:
        return
    room = width - x
    if room <= 0:
        return
    try:
        win.addstr(y, x, text[:room], attr)
    except curses.error:
        pass


def box(win, y, x, height, width, title, pal, active=False):
    """Draw a titled rounded box."""
    if height < 2 or width < 2:
        return
    edge = pal("accent", bold=True) if active else pal("frame")
    top = "╭" + "─" * (width - 2) + "╮"
    bottom = "╰" + "─" * (width - 2) + "╯"
    put(win, y, x, top, edge)
    put(win, y + height - 1, x, bottom, edge)
    for row in range(1, height - 1):
        put(win, y + row, x, "│", edge)
        put(win, y + row, x + width - 1, "│", edge)
    if title:
        label = f" {title} "
        put(win, y, x + 2, label, pal("title", bold=True) if not active else pal("accent", bold=True))


def _collapse_unused(history):
    """Runs of consecutive "unused" entries become one summary row.

    ``history`` is newest-first (a deque with ``appendleft``). Yields dicts of
    either ``{"collapsed": False, "entry": ...}`` for a row worth its own line,
    or ``{"collapsed": True, "count": n}`` for a run of skips in between.
    """
    run = 0
    for entry in history:
        if entry["state"] == "unused":
            run += 1
            continue
        if run:
            yield {"collapsed": True, "count": run}
            run = 0
        yield {"collapsed": False, "entry": entry}
    if run:
        yield {"collapsed": True, "count": run}


def bar(count, peak, width):
    if peak <= 0 or width <= 0:
        return ""
    filled = int(round(count / peak * width))
    if count > 0:
        filled = max(1, filled)
    return "█" * filled + "░" * (width - filled)


def clear_body(win, y, x, height, width, pal):
    """Blank the inside of a box so a shorter list cannot leave a longer one behind."""
    inner = width - 2
    if inner <= 0:
        return
    for row in range(y + 1, y + height - 1):
        put(win, row, x + 1, " " * inner)


class UI:
    def __init__(self, store, sock_path, demo=False):
        self.store = store
        self.sock_path = sock_path
        self.demo = demo
        self.selected = 0
        self.follow = True
        self.show_raw = False
        self.expanded = False
        self.order = "seen"
        self.query = ""          # the active search/filter term
        self.typing = False      # collecting a query at the / prompt
        self.pending = ""
        self.filtering = False   # show only matches, rather than jump to them
        self.page = 10           # rows the tool pane last showed
        self.raw_log = []
        self.frame = 0

    # -- panels ----------------------------------------------------------

    def draw_header(self, win, width, pal):
        box(win, 0, 0, 3, width, "petsitter tool monitor", pal)
        live = time.time() - self.store.last_event_at < 2.0
        dot = "●" if live else "○"
        put(win, 1, 2, dot, pal("fired" if live else "unused", bold=live))

        stats = [
            ("requests", len(self.store.requests), "title"),
            ("tools", len(self.store.tools), "title"),
            ("fired", self.store.fired_total, "fired"),
            ("withheld", self.store.withheld_total, "withheld"),
        ]
        x = 4
        for label, value, color in stats:
            put(win, 1, x, f"{label} ", pal("unused", dim=True))
            x += len(label) + 1
            text = str(value)
            put(win, 1, x, text, pal(color, bold=True))
            x += len(text) + 3

        tag = "DEMO" if self.demo else os.path.basename(self.sock_path)
        put(win, 1, max(x, width - len(tag) - 3), tag,
            pal("added", bold=True) if self.demo else pal("unused", dim=True))

    def matches(self, tool):
        """A tool matches on its name, its description, or a trick that gated it."""
        if not self.query:
            return True
        q = self.query.lower()
        if q in tool["name"].lower() or q in (tool["description"] or "").lower():
            return True
        return any(q in (h.get("by") or "").lower() or q in (h.get("reason") or "").lower()
                   for h in tool["history"])

    def visible_tools(self):
        """What the tool pane is showing, after any filter."""
        tools = self.store.ordered_tools(self.order)
        if self.filtering and self.query:
            found = [t for t in tools if self.matches(t)]
            return found or tools
        return tools

    def jump(self, step):
        """Move the selection to the next tool matching the query."""
        tools = self.visible_tools()
        if not tools:
            return
        if not self.query:
            self.selected = max(0, min(len(tools) - 1, self.selected + step))
            return
        n = len(tools)
        for offset in range(1, n + 1):
            i = (self.selected + step * offset) % n
            if self.matches(tools[i]):
                self.selected = i
                return

    def draw_tools(self, win, y, x, height, width, pal):
        """The tool belt: one row per tool, with its recent life as a strip."""
        label = dict(SORT_ORDERS).get(self.order, self.order)
        title = "tools" if self.order == "seen" else f"tools \u00b7 {label}"
        box(win, y, x, height, width, title, pal, active=not self.expanded)
        clear_body(win, y, x, height, width, pal)
        tools = self.visible_tools()
        visible = height - 2
        self.page = max(1, visible)
        if not tools:
            put(win, y + 2, x + 2, "waiting for events\u2026", pal("unused", dim=True))
            hint = "generating traffic" if self.demo else self.sock_path
            put(win, y + 3, x + 2, hint[-(width - 4):], pal("unused", dim=True))
            return

        self.selected = max(0, min(self.selected, len(tools) - 1))
        first = max(0, min(self.selected - visible // 2, len(tools) - visible))
        first = max(0, first)

        # The strip is the point of this pane -- a name with no history beside
        # it says nothing -- so it keeps its width and the name gives way.
        strip_width = max(4, min(10, width - 18))
        for index, tool in enumerate(tools[first:first + visible]):
            row = y + 1 + index
            chosen = (first + index) == self.selected
            base = pal("unused")
            if chosen:
                put(win, row, x + 1, " " * (width - 2), curses.A_REVERSE)
                base |= curses.A_REVERSE

            latest = tool["history"][0]["state"] if tool["history"] else "unused"
            glyph, color, _ = STATES[latest]
            attr = pal(color, bold=latest == "fired")
            if chosen:
                attr |= curses.A_REVERSE
            put(win, row, x + 2, glyph, attr)

            name_room = width - strip_width - 6
            hit = bool(self.query) and self.matches(tool)
            name_attr = base | (curses.A_BOLD if chosen or hit else 0)
            if hit and not chosen:
                name_attr = pal("added", bold=True)
            put(win, row, x + 4, tool["name"][:name_room].ljust(max(0, name_room)), name_attr)

            # When the order is a number, show the number being sorted on --
            # a strip of glyphs cannot be compared at a glance the way a count can.
            if self.order in ("fired", "withheld", "offered", "rate"):
                if self.order == "rate":
                    text = (f"{tool['fired'] * 100 // tool['offered']}%"
                            if tool["offered"] else "  \u2014")
                else:
                    text = str(tool[self.order])
                color = {"fired": "fired", "withheld": "withheld",
                         "offered": "unused", "rate": "fired"}[self.order]
                attr = pal(color, bold=True)
                if chosen:
                    attr |= curses.A_REVERSE
                put(win, row, x + width - strip_width - 2,
                    text.rjust(strip_width), attr)
                continue

            # the recent-states strip is where a gating pattern becomes visible
            sx = x + width - strip_width - 2
            cells = self.store.strip(tool, strip_width)
            put(win, row, sx, " " * strip_width, curses.A_REVERSE if chosen else 0)
            for offset, (mark, state) in enumerate(cells):
                cell_attr = pal(STATES[state][1], bold=state == "fired",
                                dim=state in ("unused", "withheld"))
                if chosen:
                    cell_attr |= curses.A_REVERSE
                put(win, row, sx + strip_width - len(cells) + offset, mark, cell_attr)

    def draw_tool_detail(self, win, y, x, height, width, pal):
        """One tool's life across requests: when it was allowed, and who blocked it."""
        tools = self.visible_tools()
        tool = tools[self.selected] if tools and self.selected < len(tools) else None
        box(win, y, x, height, width, tool["name"] if tool else "tool",
            pal, active=self.expanded)
        clear_body(win, y, x, height, width, pal)
        if tool is None:
            put(win, y + 2, x + 3, "no tool selected", pal("unused", dim=True))
            return

        if tool["description"]:
            put(win, y + 1, x + 3, tool["description"][:width - 6], pal("unused", dim=True))

        total = tool["offered"] or 1
        counts = [("fired", tool["fired"], "fired"),
                  ("withheld", tool["withheld"], "withheld"),
                  ("unused", tool["unused"], "unused")]
        cx = x + 3
        for label, value, color in counts:
            text = f"{label} {value}"
            put(win, y + 2, cx, text, pal(color, bold=value > 0))
            cx += len(text) + 3
        rate = f"{tool['fired'] * 100 // total}% fired of {tool['offered']} offers"
        put(win, y + 2, min(cx, x + width - len(rate) - 3), rate, pal("title"))

        meter = max(8, min(30, width - 10))
        allowed = tool["fired"] + tool["unused"]
        put(win, y + 3, x + 3, bar(allowed, tool["offered"] or 1, meter), pal("fired"))
        put(win, y + 3, x + 4 + meter, "passed the gate", pal("unused", dim=True))

        line = y + 5
        limit = y + height - 1
        put(win, line - 1, x + 3, "recent", pal("title", bold=True))
        for row in _collapse_unused(tool["history"]):
            if line >= limit:
                put(win, limit - 1, x + 3, "\u2026", pal("unused", dim=True))
                break
            if row["collapsed"]:
                # A run of "offered, unused" between the fired/withheld rows
                # that are actually worth a line each. Left uncollapsed, a
                # tool that is mostly ignored pushes every real event off the
                # bottom of the pane before you can see it.
                glyph, color, _ = STATES["unused"]
                put(win, line, x + 3, glyph, pal(color, dim=True))
                put(win, line, x + 5,
                    ("\u00d7%d" % row["count"]).ljust(9), pal("unused", dim=True))
                detail = "offered, unused"
                room = x + width - 2 - (x + 15)
                if room > 1:
                    put(win, line, x + 15, detail[:room].ljust(room),
                        pal("unused", dim=True))
                line += 1
                continue
            entry = row["entry"]
            glyph, color, label = STATES[entry["state"]]
            dim = entry["state"] in ("unused", "withheld")
            put(win, line, x + 3, glyph, pal(color, bold=not dim))
            put(win, line, x + 5, entry["request_id"][:8].ljust(9),
                pal("unused", dim=True))
            if entry["state"] == "fired":
                detail = entry["args"] or label
            elif entry["state"] == "withheld" and entry["by"]:
                detail = "withheld by " + entry["by"]
                if entry["reason"]:
                    detail += "  (" + entry["reason"] + ")"
            else:
                detail = label
            room = x + width - 2 - (x + 15)
            if room > 1:
                put(win, line, x + 15, detail[:room].ljust(room),
                    pal(color, dim=dim or entry["state"] == "fired"))
            line += 1

    def draw_request_strip(self, win, y, x, height, width, pal):
        """Requests still matter, but as context rather than as the hierarchy."""
        box(win, y, x, height, width, "recent requests", pal)
        clear_body(win, y, x, height, width, pal)
        requests = self.store.recent_requests()
        if not requests:
            put(win, y + 1, x + 3, "none yet", pal("unused", dim=True))
            return
        columns = max(1, (width - 4) // 32)
        for index, request in enumerate(requests[: columns * (height - 2)]):
            column = index % columns
            row = y + 1 + index // columns
            if row >= y + height - 1:
                break
            cx = x + 2 + column * 32
            if request["done"]:
                put(win, row, cx, "\u2714", pal("fired"))
            else:
                put(win, row, cx, SPINNER[(self.frame // 2) % len(SPINNER)],
                    pal("added", bold=True))
            put(win, row, cx + 2, request["id"][:8], pal("unused"))
            label = (request["x_title"] or request["model"] or "")[:10]
            put(win, row, cx + 11, label.ljust(10), pal("unused", dim=True))
            put(win, row, cx + 22, "\u25b8%d" % request["fired"], pal("fired"))
            put(win, row, cx + 26, "\u2298%d" % request["withheld"], pal("withheld"))

    def draw_footer(self, win, y, width, pal):
        if self.typing:
            put(win, y, 0, " " * (width - 1))
            put(win, y, 1, "/", pal("accent", bold=True))
            put(win, y, 2, self.pending, pal("title", bold=True))
            put(win, y, 2 + len(self.pending), "\u2588", pal("accent"))
            hint = "enter to search   esc to cancel"
            put(win, y, max(0, width - len(hint) - 2), hint, pal("unused", dim=True))
            return
        if self.query:
            shown = len(self.visible_tools())
            total = len(self.store.tools)
            label = f"/{self.query}"
            put(win, y, 1, label, pal("added", bold=True))
            mode = f"  {shown}/{total} shown" if self.filtering else f"  n/N to step"
            put(win, y, 1 + len(label), mode, pal("unused", dim=True))
            keys = "esc clear   \\ filter"
            put(win, y, max(0, width - len(keys) - 2), keys, pal("unused", dim=True))
            return
        self._draw_footer_keys(win, y, width, pal)

    def _draw_footer_keys(self, win, y, width, pal):
        keys = [("j/k", "tool"), ("pgup/pgdn", "page"), ("/", "search"),
                ("s", "sort"), ("f", "follow"), ("r", "raw"), ("q", "quit")]
        x = 2
        for key, label in keys:
            put(win, y, x, key, pal("accent", bold=True))
            x += len(key) + 1
            put(win, y, x, label, pal("unused", dim=True))
            x += len(label) + 3
        flag = "FOLLOW" if self.follow else "PAUSED"
        put(win, y, max(x, width - len(flag) - 2), flag,
            pal("fired" if self.follow else "added", bold=True))

    def draw_raw(self, win, height, width, pal):
        box(win, 0, 0, height - 1, width, "raw events", pal, active=True)
        visible = height - 3
        for index, line in enumerate(self.raw_log[-visible:]):
            put(win, 1 + index, 2, line, pal("unused"))
        self.draw_footer(win, height - 1, width, pal)

    # -- loop ------------------------------------------------------------

    def run(self, stdscr, sink, stop):
        curses.curs_set(0)
        stdscr.nodelay(True)
        # Claim the mouse so the terminal reports real scroll events instead of
        # faking KEY_UP/KEY_DOWN presses for wheel scroll (xterm's
        # alternateScroll and most terminals do this by default in the
        # alternate screen curses uses) -- unclaimed, every idle scroll while
        # watching was indistinguishable from deliberately navigating, which
        # flips the footer to PAUSED for no reason the user actually did.
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS)
        except curses.error:
            pass
        stdscr.timeout(80)
        pal = Palette()
        pal.setup()

        while not stop.is_set():
            drained = 0
            while drained < 200:
                try:
                    event = sink.get_nowait()
                except queue.Empty:
                    break
                drained += 1
                self.store.apply(event)
                if self.show_raw:
                    self.raw_log.append(json.dumps(event)[:400])
                    del self.raw_log[:-500]
            if drained and self.follow:
                tools = self.store.ordered_tools(self.order)
                self.selected = self.store.most_active_index(tools)

            self.frame += 1
            height, width = stdscr.getmaxyx()
            stdscr.erase()

            if height < 12 or width < 52:
                put(stdscr, 0, 0, "terminal too small", pal("withheld", bold=True))
            elif self.show_raw:
                self.draw_raw(stdscr, height, width, pal)
            else:
                self.draw_header(stdscr, width, pal)
                strip_height = min(6, max(3, height // 5))
                middle_y = 3
                middle_height = height - strip_height - middle_y - 1
                left_width = max(26, min(36, width // 3))
                self.draw_tools(stdscr, middle_y, 0, middle_height, left_width, pal)
                self.draw_tool_detail(stdscr, middle_y, left_width, middle_height,
                                      width - left_width, pal)
                self.draw_request_strip(stdscr, middle_y + middle_height, 0,
                                        strip_height, width, pal)
                self.draw_footer(stdscr, height - 1, width, pal)

            # Repaint every cell rather than trusting the incremental diff.
            # The panes reorder themselves as requests resolve, and at this
            # size a full repaint costs nothing.
            stdscr.touchwin()
            stdscr.refresh()

            key = stdscr.getch()
            if key == -1:
                continue

            # While the / prompt is open every key is part of the query, so it
            # has to be handled before the normal bindings.
            if self.typing:
                if key in (27,):                       # esc
                    self.typing, self.pending = False, ""
                elif key in (curses.KEY_ENTER, 10, 13):
                    self.typing = False
                    self.query = self.pending
                    self.pending = ""
                    self.follow = False
                    if self.query:
                        self.selected = 0
                        if not self.filtering:
                            # land on the first match rather than the first row
                            tools = self.visible_tools()
                            for i, t in enumerate(tools):
                                if self.matches(t):
                                    self.selected = i
                                    break
                elif key in (curses.KEY_BACKSPACE, 127, 8):
                    self.pending = self.pending[:-1]
                elif 32 <= key < 127:
                    self.pending += chr(key)
                continue

            if key in (ord("q"),):
                stop.set()
            elif key == 27:                            # esc clears a search
                if self.query:
                    self.query = ""
                    self.filtering = False
                else:
                    stop.set()
            elif key == ord("/"):
                self.typing, self.pending = True, ""
            elif key == ord("n"):
                self.jump(1); self.follow = False
            elif key == ord("N"):
                self.jump(-1); self.follow = False
            elif key == ord("\\"):
                self.filtering = not self.filtering
                self.selected = 0
            elif key in (ord("j"), curses.KEY_DOWN):
                self.selected += 1
                self.follow = False
            elif key in (ord("k"), curses.KEY_UP):
                self.selected = max(0, self.selected - 1)
                self.follow = False
            elif key in (curses.KEY_NPAGE, 6):         # PgDn, ctrl-f
                self.selected += self.page
                self.follow = False
            elif key in (curses.KEY_PPAGE, 2):         # PgUp, ctrl-b
                self.selected = max(0, self.selected - self.page)
                self.follow = False
            elif key in (curses.KEY_HOME, ord("g")):
                self.selected = 0
                self.follow = False
            elif key in (curses.KEY_END, ord("G")):
                self.selected = max(0, len(self.visible_tools()) - 1)
                self.follow = False
            elif key in (curses.KEY_ENTER, 10, 13, ord("s")):
                keys = [o for o, _ in SORT_ORDERS]
                self.order = keys[(keys.index(self.order) + 1) % len(keys)]
                self.selected = 0
            elif key == ord("f"):
                self.follow = not self.follow
            elif key == ord("r"):
                self.show_raw = not self.show_raw
            elif key == ord("c"):
                self.store.__init__()
                self.raw_log.clear()
                self.selected = 0
                self.query = ""
                self.filtering = False
            elif key == curses.KEY_RESIZE:
                continue
            elif key == curses.KEY_MOUSE:
                # Drain it and do nothing else: scrolling to look at the
                # screen isn't a request to stop following.
                try:
                    curses.getmouse()
                except curses.error:
                    pass


# --------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Live view of the tools petsitter offers, withholds, and fires.")
    parser.add_argument("--socket", default=str(DEFAULT_SOCKET_PATH),
                        help="unix datagram socket to bind (default: %(default)s)")
    parser.add_argument("--demo", action="store_true",
                        help="generate synthetic agentic traffic through the real socket")
    parser.add_argument("--raw", action="store_true",
                        help="print JSON events to stdout instead of drawing")
    args = parser.parse_args(argv)

    sink = queue.Queue()
    stop = threading.Event()
    threads = [threading.Thread(target=listener, args=(args.socket, sink, stop), daemon=True)]
    if args.demo:
        threads.append(threading.Thread(target=demo_sender, args=(args.socket, stop), daemon=True))
    for thread in threads:
        thread.start()

    if args.raw:
        try:
            while True:
                print(json.dumps(sink.get()), flush=True)
        except KeyboardInterrupt:
            stop.set()
        return 0

    store = Store()
    ui = UI(store, args.socket, demo=args.demo)
    try:
        curses.wrapper(ui.run, sink, stop)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=1.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())

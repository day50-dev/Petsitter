#!/usr/bin/env python3
"""toolwatch_web - a browser view of what tools petsitter hands the model.

Same trick, same transport, different front end. It binds the identical unix
datagram socket contrib/toolwatch.py listens on and decodes the identical
JSON events the Tool Monitor trick publishes -- it just draws them as a page
instead of curses, because a sortable list, a detail pane, and a search box
are all easier to get right in HTML than in a terminal. Shares no code with
petsitter and imports nothing from it -- only from toolwatch.py itself, for
the socket listener and the demo generator, which are already standalone.

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


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>toolwatch</title>
<style>
:root {
  --bg: #0b0e14; --panel: #10151f; --border: #1f2937; --text: #c7d0dc;
  --dim: #5b6675; --title: #6ab0ff; --accent: #d78cff;
  --fired: #6bd88a; --withheld: #ff7a7a; --added: #f0c96b; --unused: #7c8698;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 13px/1.5 ui-monospace, 'SF Mono', 'Fira Code', Consolas, monospace;
}
header {
  display: flex; align-items: center; gap: 18px; padding: 10px 16px;
  border-bottom: 1px solid var(--border); background: var(--panel);
}
header h1 { font-size: 14px; margin: 0; color: var(--title); font-weight: 600; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--dim); display: inline-block; }
.dot.live { background: var(--fired); box-shadow: 0 0 6px var(--fired); }
.stat { color: var(--dim); }
.stat b { color: var(--text); font-weight: 600; margin-left: 4px; }
.stat.fired b { color: var(--fired); }
.stat.withheld b { color: var(--withheld); }
.tag { margin-left: auto; color: var(--dim); }
.tag.demo { color: var(--added); }
.controls {
  display: flex; align-items: center; gap: 10px; padding: 8px 16px;
  border-bottom: 1px solid var(--border); background: var(--panel);
}
.controls input[type=text] {
  background: var(--bg); border: 1px solid var(--border); color: var(--text);
  padding: 4px 8px; border-radius: 4px; font: inherit; width: 220px;
}
.controls input[type=text]:focus { outline: 1px solid var(--accent); }
button {
  background: var(--bg); border: 1px solid var(--border); color: var(--text);
  padding: 4px 10px; border-radius: 4px; font: inherit; cursor: pointer;
}
button:hover { border-color: var(--accent); }
button.on { border-color: var(--fired); color: var(--fired); }
label.chk { display: flex; align-items: center; gap: 5px; color: var(--dim); cursor: pointer; }
.spacer { flex: 1; }
main {
  display: grid; grid-template-columns: minmax(260px, 360px) 1fr;
  grid-template-rows: 1fr auto; height: calc(100vh - 96px);
}
.pane { border: 1px solid var(--border); background: var(--panel); overflow: auto; }
#tools-pane { grid-row: 1; grid-column: 1; border-right: none; }
#detail-pane { grid-row: 1; grid-column: 2; }
#requests-pane { grid-row: 2; grid-column: 1 / 3; max-height: 140px; border-top: none; }
.pane-title {
  position: sticky; top: 0; background: var(--panel); padding: 6px 12px;
  border-bottom: 1px solid var(--border); color: var(--title); font-weight: 600;
  font-size: 12px; display: flex; justify-content: space-between;
}
.empty { padding: 24px 16px; color: var(--dim); }
.tool-row {
  display: flex; align-items: center; gap: 8px; padding: 6px 12px; cursor: pointer;
  border-bottom: 1px solid #141a26;
}
.tool-row:hover { background: #141a26; }
.tool-row.selected { background: #182136; }
.tool-row .name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tool-row .name mark { background: none; color: var(--added); font-weight: 700; }
.tool-row .strip { display: flex; gap: 1px; font-size: 11px; letter-spacing: -1px; color: var(--dim); }
.tool-row .num { min-width: 40px; text-align: right; font-weight: 600; }
.glyph.fired, .num.fired, .strip .c-fired { color: var(--fired); }
.glyph.withheld, .num.withheld, .strip .c-withheld { color: var(--withheld); }
.glyph.added, .strip .c-added { color: var(--added); }
.glyph.unused, .strip .c-unused { color: var(--unused); }
#detail-pane .body { padding: 12px 16px; }
#detail-name { font-size: 15px; font-weight: 700; color: var(--title); }
#detail-desc { color: var(--dim); margin: 4px 0 10px; }
.counts { display: flex; gap: 18px; margin-bottom: 10px; }
.counts span b { margin-left: 4px; }
.meter-row { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; }
.meter { flex: 1; height: 8px; background: #1a2231; border-radius: 4px; overflow: hidden; max-width: 260px; }
.meter i { display: block; height: 100%; background: var(--fired); }
.meter-label { color: var(--dim); }
.recent-title { color: var(--title); font-weight: 600; margin-bottom: 4px; }
.history-row { display: flex; gap: 10px; padding: 2px 0; }
.history-row .rid { color: var(--dim); min-width: 70px; }
.history-row .detail { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.history-row.dim { color: var(--dim); }
#requests-pane .body { display: flex; flex-wrap: wrap; gap: 4px 18px; padding: 8px 12px; }
.req-chip { display: flex; align-items: center; gap: 6px; color: var(--dim); font-size: 12px; }
.req-chip .rid { color: var(--text); }
.req-chip .done { color: var(--fired); }
.req-chip .pending { color: var(--added); }
#raw-pane { display: none; padding: 8px 16px; white-space: pre-wrap; word-break: break-all; }
#raw-pane.on { display: block; }
#raw-pane div { border-bottom: 1px solid #141a26; padding: 3px 0; color: var(--dim); }
footer {
  padding: 6px 16px; border-top: 1px solid var(--border); background: var(--panel);
  color: var(--dim); font-size: 12px;
}
</style>
</head>
<body>

<header>
<span class="dot" id="live-dot"></span>
<h1>toolwatch</h1>
<span class="stat">requests<b id="s-requests">0</b></span>
<span class="stat">tools<b id="s-tools">0</b></span>
<span class="stat fired">fired<b id="s-fired">0</b></span>
<span class="stat withheld">withheld<b id="s-withheld">0</b></span>
<span class="tag" id="s-tag">__TAG__</span>
</header>

<div class="controls">
<input type="text" id="search" placeholder="search name, description, gating trick, reason&hellip;">
<label class="chk"><input type="checkbox" id="filter-toggle"> filter to matches</label>
<button id="sort-btn" title="cycle sort order">sort: first seen</button>
<button id="follow-btn" class="on" title="jump to whatever just fired">follow</button>
<button id="raw-btn" title="show the raw event log instead">raw</button>
<span class="spacer"></span>
<button id="clear-btn" title="forget everything seen so far">clear</button>
</div>

<main>
  <div class="pane" id="tools-pane">
    <div class="pane-title"><span>tools</span></div>
    <div id="tools-list"></div>
  </div>
  <div class="pane" id="detail-pane">
    <div class="pane-title"><span id="detail-title">tool</span></div>
    <div class="body" id="detail-body"><div class="empty">no tool selected</div></div>
  </div>
  <div class="pane" id="requests-pane">
    <div class="pane-title"><span>recent requests</span></div>
    <div class="body" id="requests-list"></div>
  </div>
</main>
<div id="raw-pane"></div>

<footer>
The panel worth watching is the detail pane's <b>recent</b> list: each withheld row names the trick that
withheld it and its reason, when the trick reported one. Click a tool on the left to inspect its history
across every request it's been offered in.
</footer>

<script>
const STATES = {
  fired:    {glyph: "▸", cls: "fired",    label: "fired"},
  unused:   {glyph: "·", cls: "unused",   label: "offered, unused"},
  withheld: {glyph: "⊘", cls: "withheld", label: "withheld"},
  added:    {glyph: "+",      cls: "added",    label: "added by a trick"},
};
const SORT_ORDERS = [
  ["seen", "first seen"], ["fired", "most fired"], ["withheld", "most withheld"],
  ["offered", "most offered"], ["rate", "fire rate"], ["name", "name"],
];
const MAX_HISTORY = 200;
const MAX_REQUESTS = 400;

const store = {
  tools: new Map(),      // name -> tool record, in first-seen order
  requests: new Map(),   // id -> summary, in first-seen order
  events: 0, firedTotal: 0, withheldTotal: 0, lastEventAt: 0,
};

function tool(name) {
  let t = store.tools.get(name);
  if (!t) {
    t = {name, description: "", offered: 0, withheld: 0, fired: 0, unused: 0, history: []};
    store.tools.set(name, t);
  }
  return t;
}

function applyEvent(event) {
  store.events += 1;
  store.lastEventAt = Date.now();
  const rid = event.request_id || "?";
  let request = store.requests.get(rid);
  if (!request) {
    request = {id: rid, model: "", x_title: "", offered: [], fired: 0, withheld: 0, done: false};
    store.requests.set(rid, request);
    while (store.requests.size > MAX_REQUESTS) {
      store.requests.delete(store.requests.keys().next().value);
    }
  }

  if (event.event === "request") {
    request.model = event.model || "";
    request.x_title = event.x_title || "";
    const names = [];
    for (const entry of event.offered || []) {
      let name;
      if (entry && typeof entry === "object") {
        name = entry.name || "?";
        if (entry.description) tool(name).description = entry.description;
      } else {
        name = String(entry);
      }
      tool(name);
      names.push(name);
    }
    request.offered = names;
  } else if (event.event === "response") {
    request.done = true;
    const offered = (event.offered && event.offered.length) ? event.offered : request.offered;
    const withheld = new Set(event.withheld || []);
    const added = event.added || [];

    const firedArgs = new Map();
    for (const call of event.fired || []) {
      if (call && call.name && !firedArgs.has(call.name)) firedArgs.set(call.name, call.arguments || "");
    }

    const blamed = new Map(), reasons = new Map();
    for (const entry of event.by_trick || []) {
      for (const name of entry.withheld || []) if (!blamed.has(name)) blamed.set(name, entry.trick || "?");
    }
    for (const note of event.notes || []) {
      if (note.reason && note.trick) reasons.set(note.trick, note.reason);
    }

    request.fired = firedArgs.size;
    request.withheld = withheld.size;

    const names = offered.concat(added.filter(n => !offered.includes(n)));
    for (const name of names) {
      const t = tool(name);
      let state;
      if (firedArgs.has(name)) state = "fired";
      else if (withheld.has(name)) state = "withheld";
      else state = "unused";
      t[state] += 1;
      t.offered += 1;
      const by = blamed.get(name) || "";
      t.history.unshift({
        request_id: rid, at: Date.now(), state, by,
        reason: reasons.get(by) || "", args: firedArgs.get(name) || "", x_title: request.x_title,
      });
      if (t.history.length > MAX_HISTORY) t.history.length = MAX_HISTORY;
      if (state === "fired") store.firedTotal += 1;
      else if (state === "withheld") store.withheldTotal += 1;
    }
  }
}

function orderedTools(order) {
  const tools = Array.from(store.tools.values());
  const byName = (a, b) => a.name < b.name ? -1 : a.name > b.name ? 1 : 0;
  if (order === "fired") tools.sort((a, b) => b.fired - a.fired || b.offered - a.offered || byName(a, b));
  else if (order === "withheld") tools.sort((a, b) => b.withheld - a.withheld || b.offered - a.offered || byName(a, b));
  else if (order === "offered") tools.sort((a, b) => b.offered - a.offered || b.fired - a.fired || byName(a, b));
  else if (order === "rate") tools.sort((a, b) => {
    const ra = a.offered ? a.fired / a.offered : -1, rb = b.offered ? b.fired / b.offered : -1;
    return rb - ra || b.fired - a.fired || byName(a, b);
  });
  else if (order === "name") tools.sort(byName);
  return tools;
}

function mostActiveIndex(tools) {
  let bestFired = 0, bestAny = 0, newestFired = 0, newestAny = 0;
  tools.forEach((t, i) => {
    if (!t.history.length) return;
    const entry = t.history[0];
    if (entry.at > newestAny) { newestAny = entry.at; bestAny = i; }
    if (entry.state === "fired" && entry.at > newestFired) { newestFired = entry.at; bestFired = i; }
  });
  return newestFired ? bestFired : bestAny;
}

function collapseUnused(history) {
  const rows = [];
  let run = 0;
  for (const entry of history) {
    if (entry.state === "unused") { run += 1; continue; }
    if (run) { rows.push({collapsed: true, count: run}); run = 0; }
    rows.push({collapsed: false, entry});
  }
  if (run) rows.push({collapsed: true, count: run});
  return rows;
}

// -- UI state -----------------------------------------------------------

const ui = {selected: null, order: "seen", query: "", filtering: false, follow: true, raw: false};
const rawLog = [];

function matches(t, q) {
  if (!q) return true;
  q = q.toLowerCase();
  if (t.name.toLowerCase().includes(q) || (t.description || "").toLowerCase().includes(q)) return true;
  return t.history.some(h => (h.by || "").toLowerCase().includes(q) || (h.reason || "").toLowerCase().includes(q));
}

function visibleTools() {
  const tools = orderedTools(ui.order);
  if (ui.filtering && ui.query) {
    const found = tools.filter(t => matches(t, ui.query));
    return found.length ? found : tools;
  }
  return tools;
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
}

function highlight(name, q) {
  if (!q) return esc(name);
  const i = name.toLowerCase().indexOf(q.toLowerCase());
  if (i < 0) return esc(name);
  return esc(name.slice(0, i)) + "<mark>" + esc(name.slice(i, i + q.length)) + "</mark>" + esc(name.slice(i + q.length));
}

function render() {
  const dot = document.getElementById("live-dot");
  dot.classList.toggle("live", Date.now() - store.lastEventAt < 2000);
  document.getElementById("s-requests").textContent = store.requests.size;
  document.getElementById("s-tools").textContent = store.tools.size;
  document.getElementById("s-fired").textContent = store.firedTotal;
  document.getElementById("s-withheld").textContent = store.withheldTotal;

  const tools = visibleTools();
  if (ui.selected === null || !tools.some(t => t.name === ui.selected)) {
    ui.selected = tools.length ? tools[0].name : null;
  }
  if (ui.follow && tools.length) {
    ui.selected = tools[mostActiveIndex(tools)].name;
  }

  renderToolList(tools);
  renderDetail(tools.find(t => t.name === ui.selected) || null);
  renderRequests();
  if (ui.raw) renderRaw();
}

function renderToolList(tools) {
  const list = document.getElementById("tools-list");
  if (!tools.length) {
    list.innerHTML = '<div class="empty">waiting for events&hellip;</div>';
    return;
  }
  const numeric = ["fired", "withheld", "offered", "rate"].includes(ui.order);
  list.innerHTML = tools.map(t => {
    const latest = t.history.length ? t.history[0].state : "unused";
    const g = STATES[latest];
    const sel = t.name === ui.selected ? " selected" : "";
    let right;
    if (numeric) {
      const val = ui.order === "rate"
        ? (t.offered ? Math.floor(t.fired * 100 / t.offered) + "%" : "—")
        : t[ui.order];
      const cls = {fired: "fired", withheld: "withheld", offered: "", rate: "fired"}[ui.order];
      right = `<span class="num ${cls}">${val}</span>`;
    } else {
      const cells = t.history.slice(0, 10).map(h => {
        const s = STATES[h.state];
        return `<span class="c-${s.cls}">${s.glyph}</span>`;
      }).reverse().join("");
      right = `<span class="strip">${cells}</span>`;
    }
    return `<div class="tool-row${sel}" data-name="${esc(t.name)}">` +
      `<span class="glyph ${g.cls}">${g.glyph}</span>` +
      `<span class="name">${highlight(t.name, ui.filtering ? "" : ui.query)}</span>${right}</div>`;
  }).join("");
  list.querySelectorAll(".tool-row").forEach(row => {
    row.addEventListener("click", () => {
      ui.selected = row.dataset.name;
      ui.follow = false;
      setFollowBtn();
      render();
    });
  });
}

function renderDetail(t) {
  document.getElementById("detail-title").textContent = t ? t.name : "tool";
  const body = document.getElementById("detail-body");
  if (!t) { body.innerHTML = '<div class="empty">no tool selected</div>'; return; }

  const total = t.offered || 1;
  const rate = Math.floor(t.fired * 100 / total);
  const allowed = t.fired + t.unused;
  const meterPct = Math.min(100, Math.round(allowed / (t.offered || 1) * 100));

  let html = "";
  if (t.description) html += `<div id="detail-desc">${esc(t.description)}</div>`;
  html += '<div class="counts">' +
    `<span class="fired">fired<b>${t.fired}</b></span>` +
    `<span class="withheld">withheld<b>${t.withheld}</b></span>` +
    `<span class="unused">unused<b>${t.unused}</b></span>` +
    `<span class="meter-label">${rate}% fired of ${t.offered} offers</span></div>`;
  html += `<div class="meter-row"><div class="meter"><i style="width:${meterPct}%"></i></div>` +
    '<span class="meter-label">passed the gate</span></div>';
  html += '<div class="recent-title">recent</div>';

  const rows = collapseUnused(t.history);
  html += rows.map(row => {
    if (row.collapsed) {
      return `<div class="history-row dim"><span class="glyph unused" style="min-width:14px">${STATES.unused.glyph}</span>` +
        `<span class="rid">×${row.count}</span><span class="detail">offered, unused</span></div>`;
    }
    const e = row.entry;
    const s = STATES[e.state];
    let detail;
    if (e.state === "fired") detail = e.args || s.label;
    else if (e.state === "withheld" && e.by) detail = "withheld by " + e.by + (e.reason ? "  (" + e.reason + ")" : "");
    else detail = s.label;
    const dim = e.state === "unused" || e.state === "withheld" ? " dim" : "";
    return `<div class="history-row${dim}"><span class="glyph ${s.cls}" style="min-width:14px">${s.glyph}</span>` +
      `<span class="rid">${esc(e.request_id.slice(0, 8))}</span><span class="detail">${esc(detail)}</span></div>`;
  }).join("");
  body.innerHTML = html;
}

function renderRequests() {
  const list = document.getElementById("requests-list");
  const requests = Array.from(store.requests.values()).reverse().slice(0, 80);
  if (!requests.length) { list.innerHTML = '<span class="empty" style="padding:0">none yet</span>'; return; }
  list.innerHTML = requests.map(r => {
    const state = r.done ? '<span class="done">✔</span>' : '<span class="pending">◌</span>';
    const label = (r.x_title || r.model || "").slice(0, 14);
    return `<span class="req-chip">${state}<span class="rid">${esc(r.id.slice(0, 8))}</span>` +
      `<span>${esc(label)}</span><span class="fired">▸${r.fired}</span>` +
      `<span class="withheld">⊘${r.withheld}</span></span>`;
  }).join("");
}

function renderRaw() {
  const pane = document.getElementById("raw-pane");
  pane.innerHTML = rawLog.slice(-300).map(l => `<div>${esc(l)}</div>`).join("");
  pane.scrollTop = pane.scrollHeight;
}

function setFollowBtn() {
  document.getElementById("follow-btn").classList.toggle("on", ui.follow);
}

document.getElementById("search").addEventListener("input", e => {
  ui.query = e.target.value;
  ui.follow = false;
  setFollowBtn();
  render();
});
document.getElementById("filter-toggle").addEventListener("change", e => {
  ui.filtering = e.target.checked;
  render();
});
document.getElementById("sort-btn").addEventListener("click", () => {
  const keys = SORT_ORDERS.map(o => o[0]);
  ui.order = keys[(keys.indexOf(ui.order) + 1) % keys.length];
  document.getElementById("sort-btn").textContent = "sort: " + SORT_ORDERS.find(o => o[0] === ui.order)[1];
  render();
});
document.getElementById("follow-btn").addEventListener("click", () => {
  ui.follow = !ui.follow;
  setFollowBtn();
  render();
});
document.getElementById("raw-btn").addEventListener("click", () => {
  ui.raw = !ui.raw;
  document.getElementById("raw-btn").classList.toggle("on", ui.raw);
  document.getElementById("raw-pane").classList.toggle("on", ui.raw);
  document.querySelector("main").style.display = ui.raw ? "none" : "grid";
  if (ui.raw) renderRaw();
});
document.getElementById("clear-btn").addEventListener("click", () => {
  store.tools.clear(); store.requests.clear();
  store.events = 0; store.firedTotal = 0; store.withheldTotal = 0;
  rawLog.length = 0;
  ui.selected = null;
  render();
});

const source = new EventSource("/events");
source.onmessage = e => {
  const event = JSON.parse(e.data);
  applyEvent(event);
  rawLog.push(JSON.stringify(event).slice(0, 400));
  if (rawLog.length > 500) rawLog.splice(0, rawLog.length - 500);
  render();
};

setFollowBtn();
render();
</script>
</body>
</html>
"""


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
        body = PAGE.replace("__TAG__", self.tag).encode("utf-8")
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

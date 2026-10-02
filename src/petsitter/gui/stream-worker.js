// One connection to petsitter for the whole browser, shared by every tab.
//
// A browser holds at most 6 connections to one host over HTTP/1.1, and every
// tab of this origin shares them. Event streams stay open for as long as the
// page does, so a few dashboard tabs (each with logs, pause state and a Live
// page streaming) used to take all six, and every other request queued behind
// them: page loads, /api calls, everything. This worker holds the only stream
// (/api/stream) and hands each tab the topics it subscribed to.
//
// Messages from a page:  {subscribe: topic} | {unsubscribe: topic} | {bye: true}
// Messages to a page:    {topic, data} | {state: true|false} (stream up/down)

const subs = new Map();     // topic -> Set of ports
const recent = new Map();   // topic -> recent data, replayed to late subscribers
const KEEP = {logs: 200, pause: 1};
const ports = new Set();
let es = null;
let sid = null;

function keep(topic, data) {
  const list = recent.get(topic);
  if (!list) return;
  list.push(data);
  const max = KEEP[topic] || 500;
  if (list.length > max) list.splice(0, list.length - max);
}

// Tell the server which topics are wanted, one update at a time, each the
// difference between what's wanted now and what the server already has.
// Separate requests for each change could arrive out of order: an iframe
// replaced by another for the same topic sends "unsubscribe" then
// "subscribe", and if those two cross, the topic goes quiet.
let serverTopics = new Set();
let syncing = Promise.resolve();

function sync() {
  syncing = syncing.then(async () => {
    if (!sid) return;
    const want = new Set(subs.keys());
    const subscribe = [...want].filter(t => !serverTopics.has(t));
    const unsubscribe = [...serverTopics].filter(t => !want.has(t));
    if (!subscribe.length && !unsubscribe.length) return;
    const forSid = sid;
    try {
      const r = await fetch('/api/stream/' + forSid, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({subscribe, unsubscribe}),
      });
      if (r.ok && forSid === sid) serverTopics = new Set((await r.json()).topics || []);
    } catch (_) {}
  });
}

function connect() {
  es = new EventSource('/api/stream');
  es.onmessage = e => {
    let m;
    try { m = JSON.parse(e.data); } catch (_) { return; }
    if (m.sid) {
      // A new stream (first connect, or after a reconnect): the server
      // resends each topic's backlog, so start the replay buffers afresh.
      sid = m.sid;
      serverTopics = new Set();
      for (const t of recent.keys()) recent.set(t, []);
      sync();
      for (const p of ports) p.postMessage({state: true});
      return;
    }
    keep(m.topic, m.data);
    for (const p of subs.get(m.topic) || []) p.postMessage({topic: m.topic, data: m.data});
  };
  es.onerror = () => {
    sid = null;   // EventSource reconnects by itself and says hello again
    for (const p of ports) p.postMessage({state: false});
  };
}

function drop(port) {
  ports.delete(port);
  const gone = [];
  for (const [topic, set] of subs) {
    set.delete(port);
    if (!set.size) { subs.delete(topic); recent.delete(topic); gone.push(topic); }
  }
  if (gone.length) sync();
}

onconnect = e => {
  const port = e.ports[0];
  ports.add(port);
  port.onmessage = ev => {
    const m = ev.data || {};
    if (m.subscribe) {
      let set = subs.get(m.subscribe);
      if (!set) {
        subs.set(m.subscribe, set = new Set());
        recent.set(m.subscribe, []);
        sync();
      } else {
        for (const data of recent.get(m.subscribe) || []) port.postMessage({topic: m.subscribe, data});
      }
      set.add(port);
    } else if (m.unsubscribe) {
      const set = subs.get(m.unsubscribe);
      if (set) {
        set.delete(port);
        if (!set.size) { subs.delete(m.unsubscribe); recent.delete(m.unsubscribe); sync(); }
      }
    } else if (m.bye) {
      drop(port);
    }
  };
  port.start();
  if (!es) connect();
  else if (sid) port.postMessage({state: true});
};

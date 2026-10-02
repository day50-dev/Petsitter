// PetsitterStream: live updates from petsitter for this page, over the one
// connection the whole browser shares (stream-worker.js). See that file for
// why: a browser allows only 6 connections to a host, across all its tabs.
//
//   const stop = PetsitterStream.subscribe("logs", entry => ...);
//   PetsitterStream.onStatus(up => ...);   // the stream is up / down
//
// Topics: "logs", "pause", "live:<extension id>".
//
// Where SharedWorker doesn't exist (Chrome on Android), the page keeps one
// stream of its own instead, with the same protocol.
//
// It also makes a Live page's `new EventSource("events")` (served at
// /api/tricks/ui/<id>/) use the shared stream, so extension pages need no
// changes and each one doesn't cost a connection.
(function () {
  const handlers = new Map();   // topic -> Set of callbacks
  const statusCbs = [];
  let up = false;
  let send;

  function recv(m) {
    if ('state' in m) {
      up = !!m.state;
      statusCbs.forEach(cb => { try { cb(up); } catch (_) {} });
      return;
    }
    for (const cb of handlers.get(m.topic) || []) {
      try { cb(m.data); } catch (_) {}
    }
  }

  let worker = null;
  if (window.SharedWorker) {
    try {
      const src = document.currentScript && document.currentScript.src;
      const version = src ? new URL(src).search : '';
      worker = new SharedWorker('/static/stream-worker.js' + version, {name: 'petsitter-stream'});
    } catch (_) { worker = null; }
  }
  if (worker) {
    worker.port.onmessage = e => recv(e.data || {});
    worker.port.start();
    send = m => worker.port.postMessage(m);
    window.addEventListener('pagehide', () => send({bye: true}));
  } else {
    let es = null, sid = null;
    const topics = new Set();
    // One update at a time, each the full difference (see stream-worker.js).
    let serverTopics = new Set(), syncing = Promise.resolve();
    const sync = () => {
      syncing = syncing.then(async () => {
        if (!sid) return;
        const subscribe = [...topics].filter(t => !serverTopics.has(t));
        const unsubscribe = [...serverTopics].filter(t => !topics.has(t));
        if (!subscribe.length && !unsubscribe.length) return;
        const forSid = sid;
        try {
          const r = await fetch('/api/stream/' + forSid, {method: 'POST', headers: {'Content-Type': 'application/json'},
                                                          body: JSON.stringify({subscribe, unsubscribe})});
          if (r.ok && forSid === sid) serverTopics = new Set((await r.json()).topics || []);
        } catch (_) {}
      });
    };
    es = new EventSource('/api/stream');
    es.onmessage = e => {
      let m;
      try { m = JSON.parse(e.data); } catch (_) { return; }
      if (m.sid) { sid = m.sid; serverTopics = new Set(); sync(); recv({state: true}); return; }
      recv(m);
    };
    es.onerror = () => { sid = null; recv({state: false}); };
    send = m => {
      if (m.subscribe) { topics.add(m.subscribe); sync(); }
      else if (m.unsubscribe) { topics.delete(m.unsubscribe); sync(); }
    };
    window.addEventListener('pagehide', () => es && es.close());
  }

  window.PetsitterStream = {
    subscribe(topic, cb) {
      let set = handlers.get(topic);
      if (!set) { handlers.set(topic, set = new Set()); send({subscribe: topic}); }
      set.add(cb);
      return () => {
        set.delete(cb);
        if (!set.size) { handlers.delete(topic); send({unsubscribe: topic}); }
      };
    },
    onStatus(cb) { statusCbs.push(cb); if (up) cb(true); },
  };

  // A Live page: route its own "events" stream through the shared one.
  const page = location.pathname.match(/^\/api\/tricks\/ui\/([^/]+)\//);
  if (!page) return;
  const tid = page[1];
  const Native = window.EventSource;
  function Shared(url, opts) {
    const target = new URL(url, location.href);
    if (target.origin !== location.origin || target.pathname !== `/api/tricks/ui/${tid}/events`) {
      return new Native(url, opts);
    }
    const listeners = {};
    const es = {
      url: target.href, readyState: 0, withCredentials: false,
      onopen: null, onmessage: null, onerror: null,
      addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
      removeEventListener(type, fn) { listeners[type] = (listeners[type] || []).filter(f => f !== fn); },
      close() { es.readyState = 2; stop(); },
    };
    const fire = (type, ev) => {
      if (es.readyState === 2) return;
      if (es['on' + type]) es['on' + type].call(es, ev);
      (listeners[type] || []).forEach(fn => fn.call(es, ev));
    };
    const stop = window.PetsitterStream.subscribe('live:' + tid,
      data => fire('message', new MessageEvent('message', {data: JSON.stringify(data)})));
    window.PetsitterStream.onStatus(ok => {
      if (es.readyState === 2) return;
      es.readyState = ok ? 1 : 0;
      fire(ok ? 'open' : 'error', new Event(ok ? 'open' : 'error'));
    });
    return es;
  }
  Shared.CONNECTING = 0; Shared.OPEN = 1; Shared.CLOSED = 2;
  window.EventSource = Shared;
})();

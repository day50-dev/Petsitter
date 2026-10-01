"""Shows what your AI tool actually sends: who sent each request, its system prompt, every message, the tools it offered, and how big it all is.

Coding tools build a lot of context you never see: a long system prompt,
dozens of tool definitions, and a conversation that grows with every turn
until the tool compacts it. This extension records each request and shows it
on its Live tab, so you can read exactly what the model was given and watch a
conversation's size climb (and drop when it's compacted).

## How to use

Install it and open its **Live** tab, then use your AI tool (or send a message
from **Try it**). Each request appears in the list; click one to see:

- where it came from: address, X-Title, User-Agent, channel, model
- what the context is made of: system prompt, tool definitions, conversation,
  tool results, as estimated tokens and a share of the whole
- the conversation's size over time
- the full system prompt (flagged when it changed since the last request),
  the tools offered, and every message

Put it **first** in the channel, so it sees what your tool sent before other
extensions change it.

## How it works

- `pre_hook` takes a snapshot of the messages and the payload's tools. The
  system prompt already includes anything other extensions added to it
  (Rules File, for example), because system prompts are applied before
  pre_hooks run.
- Sizes are estimates: characters / 4. Exact counts would need each model's
  own tokenizer.
- Every request publishes a small summary event to the Live tab. The full text
  of the last 30 requests is kept here and fetched when you open one
  (`ui_action({"action": "detail", "id": ...})`), so a long session doesn't
  hold hundreds of megabytes of context in memory.
- A conversation is identified by the program plus its first user message, the
  same heuristic Tool Monitor uses.
"""

import hashlib
import json
import threading
import time
from collections import OrderedDict

from petsitter.observability import (
    current_client_addr,
    current_trickset,
    current_user_agent,
    request_meta,
)
from petsitter.trick import Trick

KEEP_FULL = 30            # requests whose full text is kept for the detail view
MAX_TEXT = 200_000        # characters kept per message / system prompt


def _text(content) -> str:
    """The readable text of a message's content, whatever shape it came in."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                elif block.get("type") in ("tool_result", "tool_use"):
                    inner = block.get("content", block.get("input", ""))
                    parts.append(inner if isinstance(inner, str) else json.dumps(inner, default=str))
                else:
                    parts.append(json.dumps(block, default=str)[:2000])
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return json.dumps(content, default=str)


def _tokens(chars: int) -> int:
    return round(chars / 4)


class ContextMonitorTrick(Trick):
    """Records each request's context for its Live tab."""

    __brief__ = "Shows what your AI tool sends: system prompt, messages, tools, and their size"
    __display_name__ = "Context Monitor"
    __category__ = "Diagnostics"
    ui_page = "context_monitor.html"

    def __init__(self):
        self._full: OrderedDict[str, dict] = OrderedDict()
        self._last_system: dict[str, str] = {}   # conversation -> system prompt hash
        self._seq: dict[str, int] = {}           # conversation -> requests seen
        self._lock = threading.Lock()

    def pre_hook(self, context: list, params: dict) -> list:
        try:
            self._record(context or [], params or {})
        except Exception:
            # A viewer must never break a request.
            pass
        return context

    def _record(self, context: list, params: dict) -> None:
        meta = request_meta()
        x_title = meta.get("x_title", "") or ""
        ua = current_user_agent()
        ts = current_trickset()
        channel = getattr(ts, "name", "") or ""

        system = "\n\n".join(_text(m.get("content")) for m in context
                             if isinstance(m, dict) and m.get("role") == "system")
        messages = []
        history = results = 0
        for m in context:
            if not isinstance(m, dict) or m.get("role") == "system":
                continue
            text = _text(m.get("content"))
            calls = m.get("tool_calls") or []
            if calls:
                text = (text + "\n" if text else "") + "\n".join(
                    f"-> {(c.get('function') or {}).get('name', '?')}({(c.get('function') or {}).get('arguments', '')})"
                    for c in calls if isinstance(c, dict))
            role = m.get("role", "?")
            if role == "tool":
                results += len(text)
            else:
                history += len(text)
            messages.append({"role": role, "text": text[:MAX_TEXT], "chars": len(text)})

        tools = []
        for t in params.get("tools") or meta.get("tools") or []:
            body = t.get("function") if isinstance(t, dict) and isinstance(t.get("function"), dict) else t
            name = (body or {}).get("name", "?") if isinstance(body, dict) else str(body)
            tools.append({"name": name, "chars": len(json.dumps(t, default=str))})

        first_user = next((mm["text"][:512] for mm in messages if mm["role"] == "user"), "")
        who = x_title or ("ua:" + ua.split("/")[0].split(" ")[0] if ua else "")
        conv = hashlib.sha1(f"{who}\x00{first_user}".encode("utf-8", "replace")).hexdigest()[:8]
        sys_hash = hashlib.sha1(system.encode("utf-8", "replace")).hexdigest()[:12]

        parts = {
            "system": _tokens(len(system)),
            "tools": _tokens(sum(t["chars"] for t in tools)),
            "history": _tokens(history),
            "results": _tokens(results),
        }
        rid = meta.get("request_id", "") or hashlib.sha1(f"{time.time()}".encode()).hexdigest()[:8]
        with self._lock:
            seq = self._seq.get(conv, 0) + 1
            self._seq[conv] = seq
            prev_sys = self._last_system.get(conv)
            self._last_system[conv] = sys_hash
            self._full[rid] = {"system": system[:MAX_TEXT], "messages": messages, "tools": tools}
            while len(self._full) > KEEP_FULL:
                self._full.popitem(last=False)

        self.publish({
            "event": "context",
            "id": rid,
            "ts": time.time(),
            "conv": conv,
            "seq": seq,
            "from": current_client_addr(),
            "x_title": x_title,
            "user_agent": ua,
            "channel": channel,
            "model": meta.get("model", "") or params.get("model", ""),
            "parts": parts,
            "total": sum(parts.values()),
            "messages": len(messages),
            "tools": len(tools),
            "system_chars": len(system),
            "system_changed": prev_sys is not None and prev_sys != sys_hash,
            "first_line": next((mm["text"].strip().split("\n")[0][:120] for mm in reversed(messages)
                                if mm["role"] == "user" and mm["text"].strip()), ""),
        })

    def ui_action(self, data):
        action = (data or {}).get("action") if isinstance(data, dict) else None
        if action == "detail":
            with self._lock:
                full = self._full.get(str(data.get("id", "")))
            return full or {"gone": True, "keep": KEEP_FULL}
        if action == "clear":
            self.live_feed.clear()
            with self._lock:
                self._full.clear()
        return {"ok": True}

    def info(self, capabilities: dict) -> dict:
        capabilities["context_monitor"] = True
        return capabilities

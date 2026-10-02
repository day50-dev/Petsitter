"""Lets you edit the conversation the model sees, either side of it: unstick a refusal, or cut what's taking up room.

Two things go wrong in a long conversation, and both are fixed by changing what's
in it:

- **A refusal sticks.** The model says "I'm not allowed to read emails" to a
  perfectly reasonable request, and from then on it reads its own refusal in the
  history and keeps refusing. Change that reply to "Sure, happy to help with
  that" and it carries on as if it had agreed.
- **The context fills up.** Images and tool output (web searches, file reads)
  stay in the history long after they've done their job, and every turn pays
  for them again. Remove them and a short note takes their place.

Your tool never sees the change: it keeps resending the original, and petsitter
swaps in your version on every request.

## How to use

Install it and open its **Live** tab. Recent conversations are listed on the
left; pick one to see it as a chat, you on the right and the model on the left,
with each message's rough size. On any message:

- **Edit** rewrites it. Works on the model's replies as well as yours.
- **Remove** replaces it with a short note (which you can edit), so the
  conversation still reads sensibly. A tool's output keeps its link to its call.
- **Remove images** drops the images from a message and leaves a note.
- **Revert** puts the original back.

Then send your next message from your tool as usual. Edited messages are marked,
and the original is a click away.

Put it **first** in the channel, so what you edit is what your tool sent.

## How it works

- `pre_hook` identifies the conversation (the program plus its first message,
  as Context Monitor does) and fingerprints each message by its role and
  content. Any message with an edit is swapped for the edited version, every
  request, since the client resends the original each time.
- Edits apply only to the conversation they were made in. They're kept in
  memory, for the 30 most recent conversations, and a restart drops them.
- The Live tab gets a small event per request; a conversation's messages are
  fetched when you open it (`ui_action({"action": "detail", ...})`).
"""

import hashlib
import json
import threading
import time
from collections import OrderedDict

from petsitter.observability import current_user_agent, request_meta
from petsitter.trick import Trick

KEEP_CONVERSATIONS = 30
MAX_TEXT = 200_000          # characters of a message kept for the Live tab

IMAGE_NOTE = "(The user shared an image here; it has been dealt with.)"
REMOVED_NOTES = {
    "tool": "(This tool's output was removed after it was used.)",
    "user": "(An earlier message was removed here.)",
    "assistant": "(An earlier reply was removed here.)",
    "system": "(Earlier instructions were removed here.)",
}


def _is_image(part) -> bool:
    return isinstance(part, dict) and (part.get("type") in ("image_url", "image", "input_image")
                                       or "image_url" in part)


def _text(content) -> str:
    """A message's readable text, whatever shape its content is."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(p.get("text", "")) for p in content
                         if isinstance(p, dict) and isinstance(p.get("text"), str))
    return json.dumps(content, default=str)


def _images(content) -> list[int]:
    """Rough sizes (characters) of the images in a message."""
    if not isinstance(content, list):
        return []
    sizes = []
    for p in content:
        if _is_image(p):
            sizes.append(len(json.dumps(p, default=str)))
    return sizes


def _args(raw) -> str:
    """Tool-call arguments in one form, however the client re-serialized them."""
    try:
        return json.dumps(json.loads(raw) if isinstance(raw, str) else raw, sort_keys=True)
    except (TypeError, ValueError):
        return str(raw).strip()


def _fingerprint(msg: dict) -> str:
    """Identifies a message across requests. Clients don't always resend a
    reply exactly as they got it (null vs "", a trailing newline, re-serialized
    tool arguments), so those differences don't count."""
    content = msg.get("content")
    if content is None or isinstance(content, str):
        content = (content or "").strip()
    calls = [((c.get("function") or {}).get("name"), _args((c.get("function") or {}).get("arguments")))
             for c in msg.get("tool_calls") or [] if isinstance(c, dict)]
    blob = json.dumps({"role": msg.get("role"), "content": content, "calls": calls,
                       "tool_call_id": msg.get("tool_call_id") or None}, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8", "replace")).hexdigest()[:16]


def _conversation(context: list) -> tuple[str, str]:
    """(key, who): the program plus its first user message, as Context Monitor does."""
    meta = request_meta()
    x_title = meta.get("x_title", "") or ""
    ua = current_user_agent()
    who = x_title or (ua.split("/")[0].split(" ")[0] if ua else "")
    first = next((_text(m.get("content"))[:512] for m in context
                  if isinstance(m, dict) and m.get("role") == "user"), "")
    key = hashlib.sha1(f"{who}\x00{first}".encode("utf-8", "replace")).hexdigest()[:8]
    return key, who


def apply_edit(msg: dict, edit: dict) -> dict:
    """msg with edit applied, as a new dict."""
    out = dict(msg)
    kind = edit.get("kind")
    if kind == "replace":
        out["content"] = edit.get("text", "")
    elif kind == "remove":
        out["content"] = edit.get("note") or REMOVED_NOTES.get(msg.get("role"), "(Removed.)")
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            # The calls stay: their outputs follow, and a tool output needs its call.
            out["content"] = out["content"] or None
    elif kind == "drop_images":
        content = msg.get("content")
        if isinstance(content, list):
            kept = [p for p in content if not _is_image(p)]
            kept.append({"type": "text", "text": edit.get("note") or IMAGE_NOTE})
            out["content"] = kept
    return out


class ContextEditorTrick(Trick):
    """Swaps edited messages into the conversation on every request."""

    __brief__ = "Edit what the model sees, either side: unstick a refusal, or cut images and tool output"
    __display_name__ = "Context Editor"
    __category__ = "Context & Prompts"
    needs_window = 0     # reads the reply once it's sent, to show it; never changes it
    ui_page = "context_editor.html"

    def __init__(self):
        self._lock = threading.Lock()
        # conversation -> {"who", "first", "seen", "messages": [...], "edits": {key: edit}}
        self._convs: OrderedDict[str, dict] = OrderedDict()

    # -- the request ---------------------------------------------------------

    def pre_hook(self, context: list, params: dict) -> list:
        if not context:
            return context
        try:
            key, who = _conversation(context)
            request_meta()["ctxedit_conv"] = key
            return self._apply(key, who, context)
        except Exception:
            return context   # an editor must never break a request

    def post_hook(self, context: list) -> list:
        """Add the model's reply to the conversation as soon as it's back, so it
        can be edited before the next message (the refusal you want to fix is
        usually the newest reply)."""
        try:
            key = request_meta().get("ctxedit_conv")
            reply = context[-1] if context else None
            if key and isinstance(reply, dict) and reply.get("role") == "assistant":
                with self._lock:
                    conv = self._convs.get(key)
                    if conv is None:
                        return context
                    snapshot = conv.get("messages") or []
                    fp = _fingerprint(reply)
                    n = 1 + sum(1 for item in snapshot if item["key"].split(":")[0] == fp)
                    snapshot.append({"key": f"{fp}:{n}", "original": dict(reply), "latest": True})
                    conv["messages"] = snapshot
                self.publish({"event": "context", "conv": key, "reply": True, "ts": time.time()})
        except Exception:
            pass
        return context

    def _apply(self, key: str, who: str, context: list) -> list:
        counts: dict[str, int] = {}
        snapshot, out, edited = [], [], 0
        with self._lock:
            conv = self._convs.pop(key, None) or {"edits": {}}
            self._convs[key] = conv
            while len(self._convs) > KEEP_CONVERSATIONS:
                self._convs.popitem(last=False)
            edits = conv["edits"]
            for msg in context:
                if not isinstance(msg, dict):
                    out.append(msg)
                    continue
                fp = _fingerprint(msg)
                n = counts[fp] = counts.get(fp, 0) + 1
                mkey = f"{fp}:{n}"     # identical messages ("ok") told apart by occurrence
                edit = edits.get(mkey)
                new = apply_edit(msg, edit) if edit else msg
                edited += bool(edit)
                out.append(new)
                snapshot.append({"key": mkey, "original": msg})
            first = next((_text(m.get("content")).strip().split("\n")[0][:120]
                          for m in context if isinstance(m, dict) and m.get("role") == "user"), "")
        size = sum(len(json.dumps(m.get("content"), default=str)) for m in out if isinstance(m, dict))
        with self._lock:
            conv.update(who=who, first=first, seen=time.time(), messages=snapshot, tokens=round(size / 4))
        self.publish({"event": "context", "conv": key, "who": who, "first": first,
                      "messages": len(out), "tokens": round(size / 4), "edited": edited, "ts": time.time()})
        if edited:
            self.report(f"Swapped in {edited} edited message{'s' if edited != 1 else ''}",
                        conversation=key)
        return out

    # -- the Live tab --------------------------------------------------------

    def ui_action(self, data):
        data = data if isinstance(data, dict) else {}
        action = data.get("action")
        if action == "conversations":
            with self._lock:
                return {"conversations": [
                    {"conv": k, "who": c.get("who", ""), "first": c.get("first", ""),
                     "messages": len(c.get("messages", [])), "edited": len(c["edits"]),
                     "tokens": c.get("tokens", 0),
                     "seen": c.get("seen", 0)}
                    for k, c in reversed(self._convs.items()) if c.get("messages")]}
        if action == "detail":
            return self._detail(str(data.get("conv", "")))
        if action in ("replace", "remove", "drop_images", "revert"):
            return self._edit(str(data.get("conv", "")), str(data.get("key", "")), action, data)
        if action == "clear":
            self.live_feed.clear()
        return {"ok": True}

    def _detail(self, key: str) -> dict:
        with self._lock:
            conv = self._convs.get(key)
            if conv is None:
                return {"gone": True}
            messages, edits = list(conv.get("messages", [])), dict(conv["edits"])
        rows = []
        for item in messages:
            msg, mkey = item["original"], item["key"]
            edit = edits.get(mkey)
            shown = apply_edit(msg, edit) if edit else msg
            rows.append({
                "key": mkey,
                "latest": bool(item.get("latest")),
                "role": msg.get("role", "?"),
                "text": _text(shown.get("content"))[:MAX_TEXT],
                "original": _text(msg.get("content"))[:MAX_TEXT] if edit else None,
                "images": _images(shown.get("content")),
                "had_images": len(_images(msg.get("content"))),
                "tool_calls": [{"name": (c.get("function") or {}).get("name", ""),
                                "arguments": str((c.get("function") or {}).get("arguments", ""))[:4000]}
                               for c in msg.get("tool_calls") or [] if isinstance(c, dict)],
                "tool_call_id": msg.get("tool_call_id", ""),
                "chars": len(json.dumps(shown.get("content"), default=str)),
                "edit": edit,
            })
        # The whole conversation as the model gets it (after the edits), and
        # what the edits saved. Rough, like every size here: characters / 4.
        sent = sum(r["chars"] for r in rows)
        with self._lock:
            if key in self._convs:
                self._convs[key]["tokens"] = round(sent / 4)   # the list shows the size after edits too
        original = sum(len(json.dumps(item["original"].get("content"), default=str)) for item in messages)
        return {"conv": key, "who": conv.get("who", ""), "messages": rows,
                "tokens": round(sent / 4), "saved": max(0, round((original - sent) / 4))}

    def _edit(self, key: str, mkey: str, action: str, data: dict) -> dict:
        with self._lock:
            conv = self._convs.get(key)
            if conv is None:
                return {"gone": True}
            if action == "revert":
                conv["edits"].pop(mkey, None)
            elif action == "replace":
                conv["edits"][mkey] = {"kind": "replace", "text": str(data.get("text", ""))}
            elif action == "remove":
                conv["edits"][mkey] = {"kind": "remove", "note": str(data.get("note", "")) or None}
            elif action == "drop_images":
                conv["edits"][mkey] = {"kind": "drop_images", "note": str(data.get("note", "")) or None}
        if action != "revert":
            self.report({"replace": "Edited a message", "remove": "Removed a message",
                         "drop_images": "Removed the images from a message"}[action], conversation=key)
        return self._detail(key)

    def info(self, capabilities: dict) -> dict:
        capabilities["context_editor"] = True
        return capabilities

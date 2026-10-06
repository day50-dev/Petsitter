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

It can also do the second job by itself: pick a **Compaction** technique and
old tool output or screenshots are removed from every request automatically.

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

**Compaction**, at the bottom of the conversation list, runs one published
technique on every request in the channel: *Observation masking* (The
Complexity Trap), *clear_tool_uses_20250919* (Anthropic context editing) or
*only_n_most_recent_images* (Anthropic's computer-use demo). Each name links to
its source. What it changed is marked in the chat like an edit. See
[Compaction](https://github.com/day50-dev/Petsitter/blob/main/docs/compaction.md)
for what each one keeps.

Put it **first** in the channel, so what you edit is what your tool sent.

**Continue in another program.** Every conversation has an id. Open it on the
Live tab and press **continue in another program**: that copies
`(context:import:<id>)`. Paste it into any other program connected to petsitter
(followed by whatever you want to say next) and the model there picks the
conversation up: its messages are swapped in where you pasted, on every
request. That program's own system prompt and tools apply; the old program's
tool calls come along renamed `legacy_<name>` (and declared, so every API
accepts them), so the model sees what was done and that it can't call those
tools here. Conversations are saved in `~/.cache/petsitter/contexts/` as the
model last saw them, edits included, and survive a restart.

## How it works

- `pre_hook` identifies the conversation (the program plus its first message,
  as Context Monitor does) and fingerprints each message by its role and
  content. Any message with an edit is swapped for the edited version, every
  request, since the client resends the original each time.
- Edits apply only to the conversation they were made in. They're kept in
  memory, for the 30 most recent conversations, and a restart drops them.
- Compaction runs after your edits, in the same `pre_hook`, on what's about to
  be sent. It goes by role, position and size only, so it costs nothing. The
  choice is saved as the `compaction` setting.
- The Live tab gets a small event per request; a conversation's messages are
  fetched when you open it (`ui_action({"action": "detail", ...})`).
- A conversation's id is `reserved("ctx", ...)` derived from its key, so it's
  the same on every request. After edits, before compaction, the conversation
  is written to `~/.cache/petsitter/contexts/<id>.json` (`$PETSITTER_CONTEXTS_DIR`
  overrides the folder), and again with the reply in `post_hook`.
- `(context:import:<id>)` is the trick's prompt keyword with
  `strip_prompt_keyword = False`, so the proxy leaves it where it was typed.
  `pre_hook` replaces the message holding it with the saved conversation
  (`as_imported`: system messages dropped, tool calls and tool results renamed
  `legacy_<name>`), then the message's remaining text, or a one-line "Continued
  from..." note when there is none. It does this on every request, since the
  program keeps resending the marker. The `legacy_` names are added to the
  request's tools with a "can't be run here" description, so APIs that check
  the history against the tools accept it. An unknown id leaves the message as
  it is and says so on the Live tab.
"""

import copy
import hashlib
import json
import os
import re
import threading
import time
from collections import OrderedDict

from petsitter.compaction import TECHNIQUES, compact
from petsitter.observability import LOG_DIR, current_user_agent, request_meta
from petsitter.trick import Trick, reserved, reserved_id, reserved_pattern

# Every conversation is saved here under its id, as the model last saw it, so
# another program can pick it up with (context:import:<id>).
CONTEXTS_DIR = LOG_DIR / "contexts"


def _contexts_dir():
    """Where conversations are saved; $PETSITTER_CONTEXTS_DIR overrides it
    (the tests use that)."""
    from pathlib import Path
    return Path(os.environ.get("PETSITTER_CONTEXTS_DIR") or CONTEXTS_DIR)


def import_re(keyword: str = "context") -> "re.Pattern":
    """(<keyword>:import:<id>), the keyword being the trick's prompt keyword."""
    return re.compile(r"\(\s*" + re.escape(keyword) + r"\s*:\s*import\s*:\s*(" + reserved_pattern("ctx").pattern +
                      r")\s*\)", re.IGNORECASE)
LEGACY_PREFIX = "legacy_"
LEGACY_NOTE = "From an earlier conversation in another program. It can't be run here."

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


def context_id(key: str) -> str:
    """A conversation's id for (context:import:<id>): a reserved name, the same
    on every request of the conversation."""
    return reserved("ctx", reserved_id(hashlib.sha256(f"ctx\x00{key}".encode()).digest()))


def _legacy(name: str) -> str:
    name = name or "tool"
    return name if name.startswith(LEGACY_PREFIX) else (LEGACY_PREFIX + name)[:64]


def as_imported(messages: list) -> tuple[list, set]:
    """Another program's conversation, ready to continue in this one: its
    system prompt left out (this program's own applies), and its tool calls
    renamed legacy_<name>, so the model sees what was done and that it can't
    call those tools here. Returns (messages, the legacy tool names)."""
    out, names = [], set()
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") in ("system", "developer"):
            continue
        msg = copy.deepcopy(msg)
        for call in msg.get("tool_calls") or []:
            fn = call.get("function") if isinstance(call, dict) else None
            if isinstance(fn, dict):
                fn["name"] = _legacy(fn.get("name"))
                names.add(fn["name"])
        if msg.get("role") == "tool" and msg.get("name"):
            msg["name"] = _legacy(msg["name"])
        out.append(msg)
    return out, names


def _without_marker(content, marker: str, fallback: str):
    """The importing message with the (context:import:...) taken out."""
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and isinstance(p.get("text"), str) and marker in p["text"]:
                text = p["text"].replace(marker, "").strip()
                if text:
                    parts.append({**p, "text": text})
            else:
                parts.append(p)
        return parts or fallback
    text = str(content or "").replace(marker, "").strip()
    return text or fallback


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
    # (context:import:<id>) is handled in pre_hook, where it was typed, so it
    # isn't stripped before the trick sees it.
    prompt_keyword = "context"
    strip_prompt_keyword = False
    ui_page = "context_editor.html"
    config_fields = [
        {"key": "compaction", "label": "Compaction", "type": "choice",
         "options": ["off", *TECHNIQUES], "default": "off",
         "description": "A published technique run on every request. observation_masking: tool results "
                        "older than the last 10 become a one-line note. clear_tool_uses_20250919: over 100k "
                        "tokens, tool results older than the last 3 are cleared. only_n_most_recent_images: "
                        "keeps the last 3 images inside tool results (screenshots a tool took), not images "
                        "pasted into messages. Also set from the Live tab, where each links to its source."},
    ]

    def __init__(self):
        self.compaction = "off"   # a key of petsitter.compaction.TECHNIQUES, or "off"
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
            context, legacy = self._import(context)
            if legacy:
                # Declared too, so an API that checks history against the tools
                # (Anthropic's does) accepts the renamed calls.
                tools = [t for t in (params.get("tools") or [])
                         if (t.get("function") or {}).get("name") not in legacy]
                tools.extend({"type": "function", "function": {
                    "name": n, "description": LEGACY_NOTE, "parameters": {"type": "object", "properties": {}}}}
                    for n in sorted(legacy))
                params["tools"] = tools
            return self._apply(key, who, context)
        except Exception:
            return context   # an editor must never break a request

    # -- moving a conversation between programs -------------------------------

    def _import(self, context: list) -> tuple[list, set]:
        """Each (context:import:<id>) in a user message becomes that saved
        conversation, in place, followed by whatever else the message said.
        The program keeps resending the message with the marker in it, so this
        happens on every request, like an edit."""
        out, legacy = [], set()
        for msg in context:
            text = _text(msg.get("content")) if isinstance(msg, dict) and msg.get("role") == "user" else ""
            keyword = (self.prompt_keyword or "context").lower()
            m = import_re(keyword).search(text) if keyword in text.lower() else None
            saved = self._load(m.group(1)) if m else None
            if m and saved is None:
                self.report(f"No saved conversation {m.group(1)}: left as it is")
            if saved is None:
                out.append(msg)
                continue
            imported, names = as_imported(saved.get("messages") or [])
            legacy |= names
            out.extend(imported)
            fallback = f"(Continued from an earlier conversation in {saved.get('who') or 'another program'}.)"
            out.append({**msg, "content": _without_marker(msg.get("content"), m.group(0), fallback)})
            self.report(f"Imported {len(imported)} messages from {saved.get('who') or 'another program'}",
                        conversation=m.group(1))
        return out, legacy

    def _path(self, cid: str):
        return _contexts_dir() / f"{cid}.json"

    def _load(self, cid: str) -> dict | None:
        try:
            return json.loads(self._path(cid).read_text())
        except (OSError, ValueError):
            return None

    def _save(self, key: str, messages: list) -> None:
        """The conversation as the model saw it (edits in, before compaction),
        under its id. Never breaks a request."""
        with self._lock:
            conv = self._convs.get(key) or {}
        cid = context_id(key)
        try:
            _contexts_dir().mkdir(parents=True, exist_ok=True)
            tmp = self._path(cid).with_suffix(".tmp")
            tmp.write_text(json.dumps({"id": cid, "who": conv.get("who", ""), "first": conv.get("first", ""),
                                       "saved": time.time(), "messages": messages}, default=str))
            os.replace(tmp, self._path(cid))
        except OSError as e:
            self.report(f"Couldn't save the conversation for importing: {e}")

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
                    sent = conv.get("sent") or []
                    # the list's size counts the reply now, not when the chat is next opened
                    conv["chars"] = conv.get("chars", 0) + len(json.dumps(reply.get("content"), default=str))
                self._save(key, sent + [reply])
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
            where = []        # out index -> snapshot index
            for msg in context:
                if not isinstance(msg, dict):
                    out.append(msg)
                    where.append(None)
                    continue
                fp = _fingerprint(msg)
                n = counts[fp] = counts.get(fp, 0) + 1
                mkey = f"{fp}:{n}"     # identical messages ("ok") told apart by occurrence
                edit = edits.get(mkey)
                new = apply_edit(msg, edit) if edit else msg
                edited += bool(edit)
                out.append(new)
                where.append(len(snapshot))
                snapshot.append({"key": mkey, "original": msg})
            first = next((_text(m.get("content")).strip().split("\n")[0][:120]
                          for m in context if isinstance(m, dict) and m.get("role") == "user"), "")
        sent = list(out)   # what's saved for importing: edits in, no compaction
        technique = getattr(self, "compaction", None) or "off"
        compacted, removed = compact(out, technique)
        if removed:
            for i, m in enumerate(compacted):
                if m is not out[i] and where[i] is not None:
                    snapshot[where[i]]["compacted"] = m
            out = compacted
        size = sum(len(json.dumps(m.get("content"), default=str)) for m in out if isinstance(m, dict))
        with self._lock:
            conv.update(who=who, first=first, seen=time.time(), messages=snapshot, chars=size, sent=sent)
        self._save(key, sent)
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
                    {"conv": k, "id": context_id(k), "who": c.get("who", ""), "first": c.get("first", ""),
                     "messages": len(c.get("messages", [])), "edited": len(c["edits"]),
                     "tokens": round(c.get("chars", 0) / 4),
                     "seen": c.get("seen", 0)}
                    for k, c in reversed(self._convs.items()) if c.get("messages")],
                    "compaction": getattr(self, "compaction", None) or "off",
                    "techniques": TECHNIQUES}
        if action == "set_compaction":
            technique = str(data.get("technique") or "off")
            if technique != "off" and technique not in TECHNIQUES:
                return {"error": f"unknown technique {technique!r}"}
            self.compaction = technique
            return {"compaction": technique, "save_config": {"compaction": technique}}
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
        edited_chars = 0
        for item in messages:
            msg, mkey = item["original"], item["key"]
            edit = edits.get(mkey)
            shown = apply_edit(msg, edit) if edit else msg
            edited_chars += len(json.dumps(shown.get("content"), default=str))
            compacted = item.get("compacted")
            if compacted is not None:
                shown = compacted
            rows.append({
                "key": mkey,
                "latest": bool(item.get("latest")),
                "role": msg.get("role", "?"),
                "text": _text(shown.get("content"))[:MAX_TEXT],
                "original": _text(msg.get("content"))[:MAX_TEXT] if edit or compacted is not None else None,
                "compacted": compacted is not None,
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
                self._convs[key]["chars"] = sent   # the list shows the size after edits too
        original = sum(len(json.dumps(item["original"].get("content"), default=str)) for item in messages)
        technique = TECHNIQUES.get(getattr(self, "compaction", None) or "", {}).get("name", "")
        return {"id": context_id(key), "keyword": self.prompt_keyword or "context", "conv": key, "who": conv.get("who", ""), "messages": rows,
                "tokens": round(sent / 4), "saved": max(0, round((original - edited_chars) / 4)),
                "compacted": max(0, round((edited_chars - sent) / 4)), "technique": technique}

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

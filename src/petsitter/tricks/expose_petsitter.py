"""Tells the model it's talking through petsitter, and lets it see (and, if you allow it, change) the extensions in its channel.

Petsitter is invisible to the model on purpose, and models were never trained
on a proxy rewriting their conversation, so ask one about it and it will say
there's no such thing. This extension says so in the system prompt and gives
the model a tool to look: each extension in the channel, what it's for,
whether it's on, and its settings.

Turn on **Let the model change settings** and it gets a second tool to change
them: switch an extension on or off, pick a compaction technique, switch a tool
off or back on. What a model does with that is an open question, and the
reason to try it.

## How to use

Install it, and put it **first** in the channel, so the other extensions see
its answers like any other reply. Then ask your tool something like "what is
petsitter doing to this conversation?".

To let the model change things, open **Settings** and turn on **Let the model
change settings**. A change the model makes is saved like one of yours and
stays until you change it. The extension's page says who changed it and when:
"changed by qwen3 in Open WebUI, 2h ago". Its **Live** tab lists everything
the model looked at and changed.

Installing it changes what the model sees: a paragraph at the end of the
system prompt, and one or two tools. Replies still stream; only the model's
tool calls are held until the reply ends.

## How it works

- `system_prompt` adds a short paragraph about petsitter.
- `pre_hook` adds `__96178c403fd9__get_petsitter_configuration`, and
  `__96178c403fd9__set_petsitter_configuration` when changes are allowed, to
  the tools. The names start with petsitter's reserved prefix (`get_prefix()`),
  so they can't collide with your tool's.
- `post_hook` (`needs_window = 1`: the text streams, tool calls are held to the
  end): when the model calls one of them, petsitter answers and asks the same
  model again (`call_upstream_sync`), up to 5 times. Your tool sees the text
  the model wrote before the call, then its answer, never the call. If the model called your tool's own
  tools in the same turn, those are dropped; it can call them again once it
  has its answer.
- The model can't change this extension's own settings. Settings of type
  `password` are never shown to it.
"""

import json
import time

from petsitter.observability import current_trickset, current_user_agent, request_meta
from petsitter.trick import Trick, call_upstream_sync, get_prefix

# petsitter's own tools carry its reserved prefix: they can't collide with your
# tool's, and anything can tell they're petsitter's.
GET = get_prefix() + "get_petsitter_configuration"
SET = get_prefix() + "set_petsitter_configuration"
MAX_ROUNDS = 5

GET_TOOL = {"type": "function", "function": {
    "name": GET,
    "description": "Show petsitter's extensions in this channel: what each is for, whether it's on, "
                   "and its settings.",
    "parameters": {"type": "object", "properties": {}},
}}
SET_TOOL = {"type": "function", "function": {
    "name": SET,
    "description": "Change one setting of a petsitter extension in this channel, or turn the extension "
                   "on or off (setting \"enabled\"). The change is saved and stays until changed again.",
    "parameters": {"type": "object", "properties": {
        "extension": {"type": "string", "description": f"The extension's id or name, from {GET}"},
        "setting": {"type": "string", "description": f"A setting key from {GET}, or \"enabled\""},
        "value": {"type": "string", "description": "The new value as text: \"true\" or \"false\" for an on/off "
                                                   "setting, a number, or one of the setting's options"},
    }, "required": ["extension", "setting", "value"]},
}}

ABOUT = ("This conversation passes through petsitter, a proxy between you and the user's tool. Its "
         "extensions can change requests and replies on the way through: hide secrets, remove old tool "
         "output or images, switch tools off, and more. If something seems changed or missing, that may "
         f"be why. {GET} shows what's running.")


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "on", "yes", "1"):
        return True
    if isinstance(value, str) and value.strip().lower() in ("false", "off", "no", "0"):
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    raise ValueError(f"expected true or false, got {value!r}")


def _unwrap(value, setting: str, field: dict):
    """Models wrap a value: {"compaction": "observation_masking"}, or name the
    option as a key, {"observation_masking": true}. Take what they meant."""
    if isinstance(value, dict) and len(value) == 1:
        key, inner = next(iter(value.items()))
        if key == setting:
            return inner
        if str(key) in [str(o) for o in field.get("options") or []] and inner is True:
            return key
    return value


class ExposePetsitterTrick(Trick):
    """Lets the model see, and optionally change, petsitter's extensions."""

    __brief__ = "Tells the model petsitter is there, and lets it see (and if you allow it, change) the extensions"
    __display_name__ = "Expose Petsitter"
    __category__ = "Agents"
    config_fields = [
        {"key": "allow_changes", "label": "Let the model change settings", "type": "boolean", "default": False,
         "description": "Adds a tool the model can use to change extensions' settings and turn them on or off."},
    ]
    # The text streams; the tool calls are held to the end either way, and a
    # call to one of these is answered there and replaced (see post_hook).
    needs_window = 1

    def __init__(self):
        self.allow_changes = False

    def _allowed(self) -> bool:
        value = getattr(self, "allow_changes", False)
        try:
            return _as_bool(value) if value != "" else False
        except ValueError:
            return False

    def _ours(self) -> set[str]:
        return {GET, SET} if self._allowed() else {GET}

    # -- the request ------------------------------------------------------------

    def system_prompt(self, to_add: str) -> str:
        return ABOUT + (f" {SET} changes it." if self._allowed() else "")

    def pre_hook(self, context: list, params: dict) -> list:
        tools = [t for t in (params.get("tools") or [])
                 if (t.get("function") or {}).get("name") not in (GET, SET)]
        tools.append(GET_TOOL)
        if self._allowed():
            tools.append(SET_TOOL)
        params["tools"] = tools
        request_meta()["expose_tools"] = tools
        return context

    def post_hook(self, context: list) -> list:
        if not context or not isinstance(context[-1], dict):
            return context
        reply = context[-1]
        ours = self._ours()
        mine = [c for c in reply.get("tool_calls") or [] if (c.get("function") or {}).get("name") in ours]
        if not mine:
            return context
        tools = request_meta().get("expose_tools") or [GET_TOOL]
        said = reply.get("content") or ""   # already sent, when the reply streamed
        convo = list(context[:-1])
        for _ in range(MAX_ROUNDS):
            for n, call in enumerate(mine):
                if not call.get("id"):
                    call["id"] = f"call_petsitter_{n}"
            convo.append({**reply, "tool_calls": mine})
            for call in mine:
                convo.append({"role": "tool", "tool_call_id": call["id"],
                              "content": json.dumps(self._run(call), default=str)})
            try:
                reply = call_upstream_sync(convo, tools)
            except Exception as e:
                reply = {"role": "assistant", "content": f"(petsitter couldn't get the model's answer: {e})"}
                break
            mine = [c for c in reply.get("tool_calls") or [] if (c.get("function") or {}).get("name") in ours]
            if not mine:
                break
        else:
            self.report(f"Stopped after {MAX_ROUNDS} rounds of petsitter tool calls", **self._who())
        # Its own tools never reach your tool (still wanted after 5 rounds:
        # dropped); calls to your tool's tools in the final reply go through.
        rest = [c for c in reply.get("tool_calls") or [] if (c.get("function") or {}).get("name") not in ours]
        reply = {k: v for k, v in reply.items() if k != "tool_calls"}
        if rest:
            reply["tool_calls"] = rest
        elif not reply.get("content"):
            reply["content"] = (f"(The model was still using petsitter's tools after {MAX_ROUNDS} rounds; "
                                "Expose Petsitter's Live tab shows what it did.)")
        # What it said before calling ("Let me check...") stays, the answer after it.
        if said and reply.get("content"):
            reply["content"] = said.rstrip() + "\n\n" + reply["content"]
        elif said:
            reply["content"] = said
        return context[:-1] + [reply]

    # -- the tools --------------------------------------------------------------

    def _run(self, call: dict) -> dict:
        fn = call.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}") if isinstance(fn.get("arguments"), str) else (fn.get("arguments") or {})
        except ValueError:
            return {"error": "arguments aren't JSON"}
        try:
            if fn.get("name") == GET:
                result = self.configuration()
                self.report("Looked at the configuration", **self._who())
                return result
            if fn.get("name") == SET and self._allowed():
                result = self.change(str(args.get("extension", "")), str(args.get("setting", "")), args.get("value"))
                if "error" in result:
                    self.report(f"Couldn't change {args.get('extension')}: {result['error']}", **self._who())
                return result
        except Exception as e:
            self.report(f"{fn.get('name')} failed: {e}", **self._who())
            return {"error": str(e)}
        return {"error": f"unknown tool {fn.get('name')!r}"}

    def configuration(self) -> dict:
        ts = current_trickset()
        if ts is None:
            return {"error": "this request isn't in a petsitter channel"}
        extensions = []
        for i, trick in enumerate(ts.tricks):
            tid = ts.trick_ids[i] if i < len(ts.trick_ids) else ""
            fields = {f.get("key"): f for f in type(trick).config_fields or [] if isinstance(f, dict)}
            settings = []
            for s in trick.current_settings():
                field = fields.get(s["key"], {})
                if s.get("type") == "password":
                    continue
                entry = {"key": s["key"], "label": s["label"], "type": s.get("type", "text"), "value": s["value"]}
                if field.get("options"):
                    entry["options"] = list(field["options"])
                if field.get("description"):
                    entry["description"] = field["description"]
                settings.append(entry)
            item = {"id": tid,
                    "name": getattr(trick, "__display_name__", "") or type(trick).__name__,
                    "about": getattr(trick, "__brief__", ""),
                    "enabled": ts.trick_enabled[i] if i < len(ts.trick_enabled) else True,
                    "settings": settings}
            if trick is self:
                item["this_is_me"] = True
            if ts.trick_changed_by.get(tid):
                item["changed_by"] = ts.trick_changed_by[tid]
            extensions.append(item)
        return {"channel": "Default" if ts.name == "_default" else ts.name,
                "can_change": self._allowed(), "extensions": extensions}

    def change(self, extension: str, setting: str, value) -> dict:
        ts = current_trickset()
        if ts is None:
            return {"error": "this request isn't in a petsitter channel"}
        want = extension.strip().lower()
        for i, trick in enumerate(ts.tricks):
            tid = ts.trick_ids[i] if i < len(ts.trick_ids) else ""
            names = {tid.lower(), type(trick).__name__.lower(), (getattr(trick, "__display_name__", "") or "").lower()}
            if want in names:
                break
        else:
            return {"error": f"no extension {extension!r} in this channel"}
        name = getattr(trick, "__display_name__", "") or type(trick).__name__
        if trick is self or isinstance(trick, type(self)):
            return {"error": "Expose Petsitter's own settings can only be changed by the user"}
        who = self._who()
        mark = {"by": who["model"] or "the model", "program": who["program"], "at": time.time()}
        raw = value
        if setting == "enabled":
            on = _as_bool(value)
            ts.set_trick_enabled(tid, on, changed_by=mark)
            self.report(f"Turned {name} {'on' if on else 'off'}", **who)
            return {"ok": True, "extension": name, "enabled": on}
        field = next((f for f in type(trick).config_fields or [] if isinstance(f, dict) and f.get("key") == setting), None)
        if field is None or field.get("type") == "password":
            keys = [f.get("key") for f in type(trick).config_fields or [] if isinstance(f, dict) and f.get("type") != "password"]
            return {"error": f"{name} has no setting {setting!r}; its settings are {keys + ['enabled']}"}
        kind = field.get("type", "text")
        value = _unwrap(value, setting, field)
        if kind == "boolean":
            value = _as_bool(value)
        elif kind == "number":
            value = float(value) if not isinstance(value, (int, float)) else value
        elif kind == "choice":
            if str(value) not in [str(o) for o in field.get("options") or []]:
                return {"error": f"{setting} must be one of {field.get('options')}, as a plain string; got {json.dumps(raw)}"}
            value = str(value)
        else:
            value = "" if value is None else str(value)
        ts.set_trick_config(tid, {setting: value}, changed_by=mark)
        self.report(f"Set {name}'s {field.get('label') or setting} to {value!r}", **who)
        return {"ok": True, "extension": name, "setting": setting, "value": value}

    @staticmethod
    def _who() -> dict:
        meta = request_meta()
        ua = current_user_agent()
        return {"model": meta.get("model") or "",
                "program": meta.get("x_title") or (ua.split("/")[0].split(" ")[0] if ua else "")}

    def info(self, capabilities: dict) -> dict:
        capabilities["expose_petsitter"] = True
        return capabilities

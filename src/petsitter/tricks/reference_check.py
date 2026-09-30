"""Catches the model answering from memory when it should be using your search or docs tool, by making it cite what it looked up.

In a retrieval setup (the model has a tool to search docs, a wiki or a
knowledge base), the most common hallucination looks like this: the model never
searches, or searches, finds nothing useful, and answers from memory anyway,
sounding just as sure as when it's right. This trick stamps every search result
with an id the model can't guess, requires it to cite those ids, and challenges
it when it doesn't:

```
You:   What is the Cascade valve rated to?
       (model answers "900 PSI", citing an id that was never issued)
       (petsitter challenges it, re-showing the retrieved text)
Reply: The Cascade valve is rated to 400 PSI.
```

It is invisible: the stamps and citations are stripped before anything reaches
your tool, so the reply is exactly what the model wrote. If the model still can't
back its answer after a few challenges, the answer passes through untouched.

## How to use

Nothing to type. It turns itself on whenever the request includes a tool whose
name or description matches `tool_patterns` (search, lookup, docs, wiki, rag and
so on). Set `challenge_missing_call` to false if it's too noisy on turns that
don't need a lookup. Type `(refcheck)` for a tally of answers checked, challenged
and fabricated ids caught.

## How it works

- `pre_hook` stamps every matching `role: "tool"` result. JSON lists (or lists
  under keys like `results`, `docs`, `hits`) get a `ref_id` field per item;
  prose gets `[ref_id:...]` per paragraph, plus one id for the whole result. It
  also adds a system-prompt contract asking for a trailing `<refs>` block.
- `ref_id = HMAC(per-process secret, tool_call_id + chunk)[:12]`: deterministic,
  so re-stamping each turn gives the same ids and no ledger is needed (loading
  mid-conversation works retroactively), and unguessable, so it can't be forged.
- `post_hook` parses `<refs>`. A valid issued id passes; `ref_id:none` is an
  honest pass (so forging is never the cheapest way out). Otherwise it
  challenges via `callmodel_sync`, up to `max_rounds`, naming forged ids, or
  asking it to retrieve if nothing was retrieved. If the model responds with a
  tool call, that goes to your harness to run.
- Per-request state rides `request_meta()`; only the `(refcheck)` stats are on
  the instance.
- Limit: a model can cite a real id for a claim that id doesn't support. Catching
  that needs a per-claim entailment check.
"""

import hmac
import json
import logging
import re
import secrets
from hashlib import sha256

from petsitter.observability import request_meta
from petsitter.trick import Trick, callmodel_sync

logger = logging.getLogger("petsitter")

# Substrings that mark a tool as one that retrieves reference material. Matched
# against the tool's name and its description, since MCP tools are frequently
# named things like ``mcp__ctx7__get`` while describing themselves plainly.
DEFAULT_TOOL_PATTERNS = (
    "search,find,research,reference,lookup,retrieve,query,"
    "manual,knowledge,doc,wiki,rag,kb,grep,fetch"
)

REFS_OPEN = "<refs>"
REFS_CLOSE = "</refs>"

# A whole <refs> block, tolerating a missing closing tag.
REFS_RE = re.compile(
    re.escape(REFS_OPEN) + r"(.*?)(?:" + re.escape(REFS_CLOSE) + r"|\Z)",
    re.DOTALL | re.IGNORECASE,
)
# Deliberately loose on the value so that a forged "#131" is *captured* rather
# than skipped - an id we cannot classify is the whole thing we are hunting.
CITE_RE = re.compile(r"(?:ref_?id|reference_?id)\s*[:=]\s*#?([^\s\]\)>,;\"']+)", re.IGNORECASE)
ID_RE = re.compile(r"\bref_id:([0-9a-f]{12})\b")

# Keys a RAG tool is likely to hang its result list off of.
LIST_KEYS = ("results", "documents", "docs", "chunks", "matches", "hits", "items", "data")


class ReferenceCheckTrick(Trick):
    """Forces the model to attribute retrieved claims to unforgeable ids."""

    __brief__ = "Challenges answers that cite no valid reference from a retrieval tool"
    __display_name__ = "Reference Check"
    __category__ = "Reasoning & Quality"
    prompt_keyword = "refcheck"
    config_fields = [
        {
            "key": "tool_patterns",
            "label": "Reference tool patterns",
            "description": (
                "Comma-separated substrings. A tool counts as a reference "
                "lookup when any of these appears in its name or description."
            ),
            "type": "text",
            "default": DEFAULT_TOOL_PATTERNS,
        },
        {
            "key": "max_rounds",
            "label": "Challenge rounds",
            "description": (
                "How many times to challenge an unattributed answer before "
                "giving up and passing it through untouched."
            ),
            "type": "number",
            "default": 3,
        },
        {
            "key": "challenge_missing_call",
            "label": "Challenge missing lookups",
            "description": (
                "Also challenge answers given without calling a reference tool "
                "at all. This is the most common failure, and the noisiest "
                "check - it fires on any turn that skipped retrieval."
            ),
            "type": "boolean",
            "default": True,
        },
    ]

    def __init__(
        self,
        tool_patterns: str = DEFAULT_TOOL_PATTERNS,
        max_rounds: int = 3,
        challenge_missing_call: bool = True,
    ):
        self.tool_patterns = tool_patterns or DEFAULT_TOOL_PATTERNS
        self.max_rounds = max_rounds
        self.challenge_missing_call = challenge_missing_call
        # Per-process, so an id cannot be computed by anything that only sees
        # the text. Ids are re-derived every turn, so a restart costs nothing.
        self._secret = secrets.token_bytes(32)
        self._stats = {"checked": 0, "challenged": 0, "forged": 0, "none": 0, "gave_up": 0}

    # -- prompt keyword ------------------------------------------------------

    def handle_prompt_keyword(self, request: str, messages: list | None = None, payload: dict | None = None) -> dict | None:
        s = self._stats
        if s["checked"] == 0:
            return {
                "role": "assistant",
                "content": "Reference check is loaded but has not seen a retrieval turn yet.",
            }
        return {
            "role": "assistant",
            "content": (
                f"Reference check: {s['checked']} answers checked, "
                f"{s['challenged']} challenged, {s['forged']} fabricated ids caught, "
                f"{s['none']} claims the model admitted it could not source, "
                f"{s['gave_up']} passed through after exhausting challenges."
            ),
        }

    # -- hooks ---------------------------------------------------------------

    def pre_hook(self, context: list, params: dict) -> list:
        """Stamp reference tool output, and state the attribution contract."""
        ref_tools = self._reference_tools(params.get("tools") or [])
        # Carried on the request envelope rather than on self: tricks are
        # shared between concurrent requests, so instance state would let one
        # request's tools decide another request's verdict.
        request_meta()["reference_tools"] = ref_tools
        if not ref_tools:
            return context

        names = self._tool_names_by_call_id(context)
        stamped = 0
        for msg in context:
            if msg.get("role") != "tool":
                continue
            name = msg.get("name") or names.get(msg.get("tool_call_id", ""), "")
            if name and name not in ref_tools:
                continue
            content = msg.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            msg["content"] = self._stamp(msg.get("tool_call_id", ""), content)
            stamped += 1

        if stamped:
            logger.info("reference check: stamped %d tool result(s)", stamped)

        return self._inject_contract(context, sorted(ref_tools))

    def post_hook(self, context: list) -> list:
        """Verify attribution, challenge if it is missing, strip the block."""
        meta = request_meta()
        ref_tools = meta.get("reference_tools")
        if ref_tools is None:
            # post_hook reached without pre_hook having run this request (a
            # direct call, or a trickset that filtered pre_hook out). Fall back
            # to the payload the proxy parked on the envelope.
            ref_tools = self._reference_tools(meta.get("tools") or [])
        if not context or not ref_tools:
            return context

        answer = context[-1]
        if answer.get("tool_calls") or not isinstance(answer.get("content"), str):
            return context  # the model is still working; nothing to attribute yet

        issued = self._issued_ids(context)
        self._stats["checked"] += 1

        # The challenge history accumulates: a model that cannot see it already
        # failed once will happily fail the same way again.
        working = list(context)
        rounds = 0
        limit = max(0, int(self.max_rounds or 0))
        while True:
            body, cited = _split_refs(answer.get("content") or "")
            valid, forged, none_count = _classify(cited, issued)
            self._stats["forged"] += len(forged)
            self._stats["none"] += none_count

            if valid or (none_count and not forged):
                break  # attributed, or honestly declined - both pass

            if not issued and not self.challenge_missing_call:
                break  # retrieval was skipped and we were told not to police that

            if rounds >= limit:
                if rounds:
                    self._stats["gave_up"] += 1
                    logger.info("reference check: gave up after %d rounds", rounds)
                break

            rounds += 1
            self._stats["challenged"] += 1
            logger.info(
                "reference check: challenge %d/%d (issued=%d cited=%d forged=%s)",
                rounds, limit, len(issued), len(cited), forged or "-",
            )
            try:
                working = callmodel_sync(working, self._challenge_text(issued, forged, body, context))
            except Exception as e:
                logger.warning("reference check: challenge call failed: %s", e)
                break
            answer = working[-1]
            if answer.get("tool_calls"):
                # It went to look it up after all. Let the harness run it; the
                # results come back stamped on the next request.
                logger.info("reference check: model retrieved instead of answering")
                return context[:-1] + [answer]

        # Whatever happened above, the client gets a clean body.
        answer = dict(answer)
        answer["content"] = _split_refs(answer.get("content") or "")[0]
        return context[:-1] + [answer]

    def info(self, capabilities: dict) -> dict:
        capabilities["reference_check"] = True
        return capabilities

    # -- internal ------------------------------------------------------------

    def _patterns(self) -> list[str]:
        return [p.strip().lower() for p in (self.tool_patterns or "").split(",") if p.strip()]

    def _reference_tools(self, tools: list) -> set[str]:
        """Names of the tools that look like reference lookups."""
        pats = self._patterns()
        if not pats:
            return set()
        out = set()
        for t in tools:
            fn = t.get("function", t) if isinstance(t, dict) else {}
            name = str(fn.get("name", ""))
            hay = (name + " " + str(fn.get("description", ""))).lower()
            if name and any(p in hay for p in pats):
                out.add(name)
        return out

    @staticmethod
    def _tool_names_by_call_id(context: list) -> dict[str, str]:
        """Map tool_call_id -> tool name, so tool results can be attributed."""
        out: dict[str, str] = {}
        for msg in context:
            for tc in msg.get("tool_calls") or []:
                cid = tc.get("id", "")
                name = (tc.get("function") or {}).get("name", "")
                if cid and name:
                    out[cid] = name
        return out

    def _id(self, call_id: str, chunk: str) -> str:
        seed = f"{call_id}\x00{chunk}".encode("utf-8", "replace")
        return hmac.new(self._secret, seed, sha256).hexdigest()[:12]

    def _stamp(self, call_id: str, content: str) -> str:
        """Insert a ref_id per document, plus one for the result as a whole.

        Structured results keep their structure - the id goes in as a field, so
        a model (or a downstream trick) parsing the tool output still can.
        """
        if ID_RE.search(content):
            return content  # already stamped this turn

        whole = self._id(call_id, content)

        try:
            data = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            data = None

        if data is not None:
            items, container = _locate_items(data)
            if items and all(isinstance(i, dict) for i in items):
                for item in items:
                    item["ref_id"] = self._id(call_id, json.dumps(item, sort_keys=True, default=str))
                if isinstance(container, dict):
                    container["ref_id"] = whole
                    return json.dumps(container, indent=2, default=str)
                return json.dumps(items, indent=2, default=str)

        parts = [p for p in re.split(r"\n\s*\n", content) if p.strip()]
        if len(parts) <= 1:
            return f"[ref_id:{whole}]\n{content}"
        body = "\n\n".join(f"[ref_id:{self._id(call_id, p)}]\n{p}" for p in parts)
        return f"[ref_id:{whole}] (whole result)\n\n{body}"

    @staticmethod
    def _issued_ids(context: list) -> set[str]:
        """Every id we stamped into this transcript. No ledger required."""
        out: set[str] = set()
        for msg in context:
            if msg.get("role") != "tool":
                continue
            content = msg.get("content")
            if isinstance(content, str):
                out.update(ID_RE.findall(content))
        return out

    def _inject_contract(self, context: list, ref_tools: list[str]) -> list:
        marker = "You are being reference-checked."
        text = (
            f"\n\n{marker} The tools {', '.join(ref_tools)} return material "
            f"stamped with ids of the form ref_id:xxxxxxxxxxxx.\n"
            "Every claim you draw from that material must be attributed. After "
            "your answer, and only at the very end, emit:\n"
            f"{REFS_OPEN}\n<short restatement of a claim> -> ref_id:<the id it came from>\n"
            f"...\n{REFS_CLOSE}\n"
            "Use only ids that actually appear in the tool results - never "
            "invent one, and never reuse an id for a claim it does not support.\n"
            "If you cannot source a claim, write ref_id:none for it. That is a "
            "legitimate answer and is preferred over guessing.\n"
            "If you have not consulted the material yet, call the tool rather "
            "than answering from memory.\n"
            f"The {REFS_OPEN} block is stripped before anyone sees your answer, "
            "so it must sit outside the answer proper and the answer itself must "
            "remain exactly what was asked for - do not mention this process, "
            "and do not put ids in the answer body."
        )
        if context and context[0].get("role") == "system":
            if marker not in (context[0].get("content") or ""):
                context[0]["content"] = (context[0].get("content") or "") + text
        else:
            context.insert(0, {"role": "system", "content": text.strip()})
        return context

    def _challenge_text(self, issued: set[str], forged: list[str], body: str, context: list) -> str:
        if not issued:
            if not self.challenge_missing_call:
                return ""
            return (
                "You answered without consulting the reference tools available "
                "to you. Either call one now, or - if you are certain the "
                "answer needs no source - re-send your answer with a "
                f"{REFS_OPEN} block whose every line ends in ref_id:none. Do "
                "not restate the answer with more confidence than you can source."
            )

        lines = [
            "Your answer was not attributed to the material you retrieved.",
        ]
        if forged:
            lines.append(
                "These ids do not exist and were never issued: "
                + ", ".join(sorted(set(forged)))
                + ". Inventing an id is worse than admitting you have no source."
            )
        lines.append("Here is the retrieved material again, with its valid ids:")
        lines.append("")
        for msg in context:
            if msg.get("role") == "tool" and isinstance(msg.get("content"), str):
                lines.append(msg["content"])
        lines.append("")
        lines.append(
            "Re-send your answer. It must end with a "
            f"{REFS_OPEN} block attributing each claim to one of the ids above, "
            "or to ref_id:none where the material genuinely does not support it. "
            "Drop or soften any claim you cannot attribute. Keep the answer "
            "itself in the same form as before."
        )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _locate_items(data):
    """Find the list of documents in a parsed tool result, with its container."""
    if isinstance(data, list):
        return data, None
    if isinstance(data, dict):
        for key in LIST_KEYS:
            v = data.get(key)
            if isinstance(v, list) and v:
                return v, data
    return None, None


def _split_refs(content: str) -> tuple[str, list[str]]:
    """Return the answer with every <refs> block removed, and the ids cited."""
    cited: list[str] = []
    for m in REFS_RE.finditer(content):
        cited.extend(CITE_RE.findall(m.group(1)))
    body = REFS_RE.sub("", content).strip()
    return body, cited


def _classify(cited: list[str], issued: set[str]) -> tuple[list[str], list[str], int]:
    """Split cited ids into (valid, forged, count of honest ref_id:none)."""
    valid, forged, none_count = [], [], 0
    for c in cited:
        low = c.strip().lower()
        if low == "none":
            none_count += 1
        elif low in issued:
            valid.append(low)
        else:
            forged.append(c.strip())
    return valid, forged, none_count

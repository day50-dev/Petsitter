"""Tells the model which software you want it to use (your database, package manager, HTTP client) and what to avoid, so it stops suggesting whatever was most popular in its training data.

Ask a model to "add a database" and it will often reach for whatever it saw most
online, not what you already run. This trick keeps a short list of your choices
and puts it in front of the model on every request. If the model wants to use
something else, it has to say which listed tool it's passing over and why (or,
with `strict` on, ask you instead).

## How to use

Point the `recommender_path` setting at a text file, put entries in the
`recommendations` setting (separated by `;`), or both. One entry per line:

```
# my stack
database: postgres (already in prod)
package manager: uv
avoid: mongodb (ops burden)
!jquery
ripgrep
```

`category: choice` (or `=`) is a choice for a job; `!x`, `avoid: x` or `never x`
is something to steer away from; a bare line is a general preference. A trailing
`(...)` is passed along as the reason. Edit the list from any chat:

```
(recommend)                        show the list
(recommend: http client = httpx)   add or replace
(recommend: avoid jquery)
(recommend: drop database)
(recommend: reload)                re-read the file after editing it
```

Changes are saved back to the file when one is set; otherwise they last until
restart. With an empty list the trick does nothing.

## How it works

- Entries merge file, then inline, then runtime; later wins per key. A category
  holds one choice; uncategorized entries are keyed by name.
- The avoid prefixes also accept `no`, `ban` and `not`; drop also accepts
  `remove`, `forget` and `unset`. ` #` starts a comment, so `c#` survives.
- Saving rewrites the whole file from the merged list (comments are lost) and
  folds inline and runtime entries into it.
"""

import logging
import re
from pathlib import Path

from petsitter.trick import Trick

logger = logging.getLogger("petsitter")

# "avoid: x", "never x", "no: x" - anything on the do-not-reach-for side.
AVOID_RE = re.compile(r"^(?:avoid|never|no|ban|not)(?:\s*:\s*|\s+)(.+)$", re.IGNORECASE)
# A trailing "(reason)" is kept as a note rather than treated as part of the name.
NOTE_RE = re.compile(r"^(.*?)\s*\(([^()]*)\)\s*$")
# "(recommend: drop database)" / "(recommend: remove mongodb)"
DROP_RE = re.compile(r"^(?:drop|remove|forget|unset)(?:\s*:\s*|\s+)(.+)$", re.IGNORECASE)


class RecommenderListTrick(Trick):
    """Injects the user's preferred software choices into the system prompt."""

    __brief__ = "Makes the model pick software from the user's preferred list"
    __display_name__ = "Recommender List"
    __category__ = "Output & Style"
    prompt_keyword = "recommend"
    config_fields = [
        {
            "key": "recommender_path",
            "label": "Recommendations file",
            "description": (
                "Path to a text file of preferred software, one entry per "
                "line, as 'category: choice' or 'avoid: choice'. Runtime "
                "edits made with (recommend: ...) are saved back here."
            ),
            "type": "path",
            "default": "",
        },
        {
            "key": "recommendations",
            "label": "Inline recommendations",
            "description": (
                "Entries to use on top of the file, separated by ';' - e.g. "
                "'database: postgres; package manager: uv; avoid: mongodb'."
            ),
            "type": "text",
            "default": "",
        },
        {
            "key": "strict",
            "label": "Strict",
            "description": (
                "Forbid choices outside the list outright, instead of asking "
                "the model to justify a deviation."
            ),
            "type": "boolean",
            "default": False,
        },
    ]

    def __init__(self, recommender_path: str = "", recommendations: str = "", strict: bool = False):
        self.recommender_path = recommender_path or ""
        self.recommendations = recommendations or ""
        self.strict = strict
        self._path: Path | None = None
        self._file_entries: list[dict] = []
        self._inline_entries: list[dict] = []
        self._runtime_entries: list[dict] = []
        self._load()

    def configure(self, config: dict) -> None:
        super().configure(config)
        self._load()

    def startup(self) -> None:
        self._load()

    # -- prompt keyword ------------------------------------------------------

    def handle_prompt_keyword(self, request: str, messages: list | None = None, payload: dict | None = None) -> dict | None:
        req = request.strip()

        if not req:
            return {"role": "assistant", "content": self._describe()}

        if req.lower() in ("reload", "refresh"):
            self._load()
            return {
                "role": "assistant",
                "content": f"Reloaded the recommender list.\n\n{self._describe()}",
            }

        drop = DROP_RE.match(req)
        if drop:
            return {"role": "assistant", "content": self._drop(drop.group(1).strip())}

        entries = _parse(req)
        if not entries:
            return {
                "role": "assistant",
                "content": (
                    "Nothing to add. Use (recommend: database = postgres), "
                    "(recommend: avoid mongodb), (recommend: drop database), "
                    "or (recommend) on its own to see the list."
                ),
            }

        added = [_label(e) for e in entries]
        for e in entries:
            self._add(e)
        saved = self._save()
        note = f"\nSaved to {self._path}." if saved else ""
        return {
            "role": "assistant",
            "content": "Recommending: " + "; ".join(added) + "." + note,
        }

    # -- hooks ---------------------------------------------------------------

    def system_prompt(self, to_add: str) -> str:
        entries = self._entries()
        if not entries:
            return ""

        prefer = [e for e in entries if e["kind"] == "prefer"]
        avoid = [e for e in entries if e["kind"] == "avoid"]

        lines = [
            "The user maintains a list of the software they want used. When "
            "you need to choose a tool, library, language, framework, "
            "database, or service - whether or not the user names one - pick "
            "from this list first."
        ]

        if prefer:
            lines.append("")
            lines.append("Preferred:")
            for e in prefer:
                lines.append("- " + _describe_entry(e))

        if avoid:
            lines.append("")
            lines.append("Avoid unless the user explicitly asks for it:")
            for e in avoid:
                lines.append("- " + _describe_entry(e))

        lines.append("")
        if self.strict:
            lines.append(
                "Do not propose anything outside this list for a job it "
                "covers. If the list has no fit, say so and ask the user "
                "rather than picking a substitute yourself."
            )
        else:
            lines.append(
                "If nothing on the list fits the job, say which entry you "
                "are passing over and why before proposing something else. "
                "Never silently substitute a different tool for a listed one."
            )
        return "\n".join(lines)

    def info(self, capabilities: dict) -> dict:
        entries = self._entries()
        if entries:
            capabilities["recommender_list"] = {
                "count": len(entries),
                "categories": [e["category"] for e in entries if e["category"]],
                "strict": bool(self.strict),
            }
        return capabilities

    # -- internal ------------------------------------------------------------

    def _entries(self) -> list[dict]:
        """File, then inline, then runtime - later entries win per key."""
        merged: dict[tuple, dict] = {}
        for e in self._file_entries + self._inline_entries + self._runtime_entries:
            merged[_key(e)] = e
        return list(merged.values())

    def _add(self, entry: dict) -> None:
        key = _key(entry)
        self._runtime_entries = [e for e in self._runtime_entries if _key(e) != key]
        self._runtime_entries.append(entry)

    def _drop(self, target: str) -> str:
        t = target.strip().strip("\"'").lower()
        if not t:
            return "Nothing to drop."

        def matches(e: dict) -> bool:
            return e["category"].lower() == t or e["choice"].lower() == t

        gone = [e for e in self._entries() if matches(e)]
        if not gone:
            return f"Nothing in the recommender list matches {target!r}."

        self._file_entries = [e for e in self._file_entries if not matches(e)]
        self._inline_entries = [e for e in self._inline_entries if not matches(e)]
        self._runtime_entries = [e for e in self._runtime_entries if not matches(e)]
        saved = self._save()
        note = f" Saved to {self._path}." if saved else ""
        return "Dropped: " + "; ".join(_label(e) for e in gone) + "." + note

    def _describe(self) -> str:
        entries = self._entries()
        if not entries:
            return (
                "The recommender list is empty. Set the 'recommender_path' or "
                "'recommendations' config for this trickset, or add entries "
                "with (recommend: database = postgres)."
            )
        head = f"Recommender list ({len(entries)} entries"
        head += f", from {self._path})" if self._path else ")"
        return head + ":\n" + "\n".join(
            "- " + ("avoid " if e["kind"] == "avoid" else "") + _describe_entry(e)
            for e in entries
        )

    def _load(self) -> None:
        self._file_entries = []
        self._path = None

        path = (self.recommender_path or "").strip()
        if path:
            p = Path(path).expanduser()
            if not p.exists():
                logger.warning("Recommendations file not found: %s", p)
            else:
                try:
                    self._file_entries = _parse(p.read_text(encoding="utf-8", errors="replace"))
                    self._path = p
                    logger.info(
                        "Loaded %d recommendations from %s", len(self._file_entries), p
                    )
                except OSError as e:
                    logger.error("Failed to read recommendations file %s: %s", p, e)

        self._inline_entries = _parse(self.recommendations or "")

    def _save(self) -> bool:
        """Persist the merged list back to the file, if one is configured."""
        if self._path is None:
            return False
        try:
            body = "\n".join(_render(e) for e in self._entries())
            self._path.write_text(body + "\n", encoding="utf-8")
        except OSError as e:
            logger.error("Failed to write recommendations file %s: %s", self._path, e)
            return False
        # Everything now lives in the file; stop double-counting it.
        self._file_entries = self._entries()
        self._runtime_entries = []
        self._inline_entries = []
        self.recommendations = ""
        return True


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def _parse(text: str) -> list[dict]:
    """Turn a block of text into entry dicts, one per line or ';'-separated."""
    entries: list[dict] = []
    for raw_line in (text or "").splitlines():
        for chunk in raw_line.split(";"):
            entry = _parse_one(chunk)
            if entry:
                entries.append(entry)
    return entries


def _parse_one(line: str) -> dict | None:
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    # A comment only counts when it's set off by whitespace, so "c#" survives.
    s = re.split(r"\s+#", s, maxsplit=1)[0].strip()
    if not s:
        return None

    note = ""
    m = NOTE_RE.match(s)
    if m:
        s, note = m.group(1).strip(), m.group(2).strip()
        if not s:
            return None

    kind = "prefer"
    if s.startswith("!"):
        kind, s = "avoid", s[1:].strip()
    else:
        m = AVOID_RE.match(s)
        if m:
            kind, s = "avoid", m.group(1).strip()
    if not s:
        return None

    category, choice = "", s
    m = re.match(r"^([^:=]+?)\s*[:=]\s*(.+)$", s)
    if m:
        category, choice = m.group(1).strip(), m.group(2).strip()

    return {"kind": kind, "category": category, "choice": choice, "note": note}


def _key(entry: dict) -> tuple:
    """What makes two entries the same. A category holds one choice."""
    if entry["category"]:
        return (entry["kind"], entry["category"].lower())
    return (entry["kind"], "", entry["choice"].lower())


def _label(entry: dict) -> str:
    if entry["kind"] == "avoid":
        return f"avoid {entry['choice']}"
    if entry["category"]:
        return f"{entry['category']} = {entry['choice']}"
    return entry["choice"]


def _describe_entry(entry: dict) -> str:
    if entry["category"]:
        out = f"{entry['category']}: {entry['choice']}"
    else:
        out = entry["choice"]
    if entry["note"]:
        out += f" ({entry['note']})"
    return out


def _render(entry: dict) -> str:
    """Back to file-format, so a saved file round-trips through _parse."""
    body = _describe_entry(entry)
    return f"avoid: {body}" if entry["kind"] == "avoid" else body

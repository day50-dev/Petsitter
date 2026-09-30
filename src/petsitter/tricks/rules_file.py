"""Adds the contents of a rules file (like AGENTS.md or CLAUDE.md) to every request, so every AI tool you use follows the same instructions.

Each coding tool has its own rules file: CLAUDE.md for Claude Code, AGENTS.md for
others, and so on. If you switch between tools you end up copying the same rules
around. Because petsitter sits in front of all of them, this trick lets you write
the rules once ("use pytest, not unittest", "never commit directly to main") and
have every tool follow them.

## How to use

Set the `rules_path` setting to your markdown file. Or switch files from any
chat:

```
(rules: ~/notes/my-rules.md)
```

petsitter replies directly with how many characters it loaded. Type `(rules)`
alone to see which file is active. With no file set, the trick does nothing.

## How it works

- `system_prompt` appends "The following rules MUST be followed for every
  response:" followed by the file content.
- The file is read and cached on configure, on startup, and when the keyword sets
  a new path. Edits to the file are picked up only on one of those, so re-run
  `(rules: <path>)` after editing.
- A missing or unreadable file is logged and treated as no rules.
"""

import logging
from pathlib import Path

from petsitter.trick import Trick

logger = logging.getLogger("petsitter")


class RulesFileTrick(Trick):
    """Injects a shared rules file into the system prompt."""

    __brief__ = "Injects a rules file (AGENTS.md-style) into the system prompt"
    __display_name__ = "Rules File"
    __category__ = "Context & Prompts"
    prompt_keyword = "rules"
    config_fields = [
        {
            "key": "rules_path",
            "label": "Rules file",
            "description": (
                "Path to the markdown rules file to inject into the system "
                "prompt. Leave blank to disable, or switch files at runtime "
                "with (rules: <path>)."
            ),
            "type": "path",
            "default": "",
        },
    ]

    def __init__(self, rules_path: str = ""):
        self.rules_path = rules_path or ""
        self._rules_path: Path | None = None
        self._rules_content: str = ""
        self._load_rules()

    def configure(self, config: dict) -> None:
        super().configure(config)
        self._load_rules()

    def startup(self) -> None:
        self._load_rules()

    # -- prompt keyword ------------------------------------------------------

    def handle_prompt_keyword(self, request: str, messages: list | None = None, payload: dict | None = None) -> dict | None:
        path = request.strip().strip("\"'")
        if path:
            self.rules_path = path
            self._load_rules()
            if self._rules_content:
                return {
                    "role": "assistant",
                    "content": (
                        f"Loaded {len(self._rules_content)} chars of rules "
                        f"from {self.rules_path}"
                    ),
                }
            return {
                "role": "assistant",
                "content": f"No rules loaded from {self.rules_path} (file missing or empty)",
            }
        if self._rules_content:
            return {
                "role": "assistant",
                "content": (
                    f"Rules loaded from {self._rules_path} "
                    f"({len(self._rules_content)} chars)"
                ),
            }
        return {
            "role": "assistant",
            "content": (
                "No rules file configured. Set the 'rules_path' config for "
                "this trickset, or use (rules: /path/to/rules.md)."
            ),
        }

    # -- hooks ---------------------------------------------------------------

    def system_prompt(self, to_add: str) -> str:
        if not self._rules_content:
            return ""
        return (
            "The following rules MUST be followed for every response:\n\n"
            + self._rules_content
        )

    def info(self, capabilities: dict) -> dict:
        if self._rules_content:
            capabilities["rules_file"] = str(self._rules_path)
        return capabilities

    # -- internal ------------------------------------------------------------

    def _load_rules(self) -> None:
        self._rules_content = ""
        self._rules_path = None

        path = (self.rules_path or "").strip()
        if not path:
            return

        p = Path(path).expanduser()
        if not p.exists():
            logger.warning("Rules file not found: %s", p)
            return

        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            logger.error("Failed to read rules file %s: %s", p, e)
            return

        self._rules_path = p
        self._rules_content = content.strip()
        logger.info("Loaded %d chars of rules from %s", len(self._rules_content), p)

"""Browse and swap system prompts from a repository of AI tool harnesses.

Trigger: (swapharness: path/to/file) in any user message.
On install, clones https://github.com/x1xhlol/system-prompts-and-models-of-ai-tools
into ~/.petsitter/harnesses/.  Then use the prompt keyword to navigate the
directory tree with folder/file icons and select a system prompt file.
The selected content is injected into the system prompt on every request
until a different file is chosen or the trick is uninstalled.
"""

import logging
from pathlib import Path

from src.trick import Trick

logger = logging.getLogger("petsitter")

REPO_URL = "https://github.com/x1xhlol/system-prompts-and-models-of-ai-tools"
CACHE_DIR = Path.home() / ".config" / "petsitter" / "harnesses"


class SwapHarnessTrick(Trick):
    prompt_keyword = "swapharness"
    __brief__ = "Browse and swap system prompts from AI tool repos"
    __display_name__ = "Swap Harness"

    def install(self) -> None:
        import subprocess

        if CACHE_DIR.exists():
            logger.info("Harness repo already cloned at %s", CACHE_DIR)
            return
        CACHE_DIR.parent.mkdir(parents=True, exist_ok=True)
        logger.info("Cloning %s into %s ...", REPO_URL, CACHE_DIR)
        try:
            subprocess.run(
                ["git", "clone", "--depth", "1", REPO_URL, str(CACHE_DIR)],
                capture_output=True, text=True, timeout=120,
                check=True,
            )
            logger.info("Cloned harness repo (%d entries)", len(list(CACHE_DIR.iterdir())))
        except subprocess.CalledProcessError as e:
            logger.error("git clone failed: %s", e.stderr)
            raise
        except FileNotFoundError:
            logger.error("git not found — install git to use SwapHarnessTrick")
            raise

    def handle_prompt_keyword(self, request: str, messages: list | None = None, payload: dict | None = None) -> dict | None:
        path = request.strip().rstrip("/")
        base = CACHE_DIR

        if not base.exists():
            return {
                "role": "assistant",
                "content": (
                    "Harness repo not cloned yet.  "
                    "Run (swapharness: ) again once the clone completes."
                ),
            }

        target = base / path if path else base

        if not target.exists():
            return {"role": "assistant", "content": f"Not found: {path}"}

        if target.is_dir():
            entries = sorted(target.iterdir())
            lines = [f"📁  {path or ''}" if path else "Select a harness\n"]
            for e in entries:
                icon = "📁" if e.is_dir() else "📄"
                label = e.name + "/" if e.is_dir() else e.name
                lines.append(f"{icon}  {label}")

            # Check for question tool - if missing, provide prompt-based fallback guidance
            tools = (payload or {}).get("tools") or []
            has_question_tool = any(
                "question" in (t.get("function", {}).get("name", "").lower())
                for t in tools
            ) if tools else False

            if not has_question_tool:
                lines.append("")
                lines.append("⚠️  No question tool found. Invoke with (swapharness: <word>) where word is one of:")
                lines.extend(f"  - {e.name}" for e in entries if e.is_dir())

            lines.append("")
            lines.append("(swapharness: path/to/file) to select")
            return {"role": "assistant", "content": "\n".join(lines)}

        content = target.read_text(encoding="utf-8", errors="replace")
        self._selected_path = path
        self._selected_content = content
        preview = content[:600]
        logger.info("Swapped harness to %s (%d chars)", path, len(content))
        return {
            "role": "assistant",
            "content": (
                f"✅  Harness set to **{path}** ({len(content)} chars)\n\n"
                f"```\n{preview}\n```"
            ),
        }

    def system_prompt(self, to_add: str) -> str:
        content = getattr(self, "_selected_content", None)
        if content:
            return to_add + "\n" + content if to_add else content
        return to_add

    def info(self, capabilities: dict) -> dict:
        path = getattr(self, "_selected_path", None)
        if path:
            capabilities["swapped_harness"] = path
        return capabilities

    def uninstall(self) -> None:
        import shutil

        if CACHE_DIR.exists():
            shutil.rmtree(CACHE_DIR, ignore_errors=True)
            logger.info("Removed harness cache %s", CACHE_DIR)

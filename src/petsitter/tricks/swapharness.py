"""Browse and swap system prompts from a repository of AI tool harnesses.

Trigger: (swapharness: path/to/file) in any user message.
On install, clones https://github.com/x1xhlol/system-prompts-and-models-of-ai-tools
into ~/.petsitter/harnesses/.  Then use the prompt keyword to navigate the
directory tree with folder/file icons and select a system prompt file.
The selected content is injected into the system prompt on every request
until a different file is chosen or the trick is uninstalled.
"""

import logging
import subprocess
import threading
from pathlib import Path

from petsitter.trick import Trick

logger = logging.getLogger("petsitter")

REPO_URL = "https://github.com/x1xhlol/system-prompts-and-models-of-ai-tools"
CACHE_DIR = Path.home() / ".config" / "petsitter" / "harnesses"

# The clone is the one part of this trick that can fail, take a while, or never
# have been attempted at all -- `install()` only runs when the trick is added,
# and the caller logs its exception rather than surfacing it. The keyword is
# where a person actually finds out, so it has to be able to say which of those
# happened instead of an unconditional "not cloned yet".
_clone_lock = threading.Lock()
_clone_status = "idle"      # idle | running | failed
_clone_error = ""


def _clone_repo() -> None:
    """Clone the harness repo into CACHE_DIR. Raises on failure."""
    CACHE_DIR.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Cloning %s into %s ...", REPO_URL, CACHE_DIR)
    subprocess.run(
        ["git", "clone", "--depth", "1", REPO_URL, str(CACHE_DIR)],
        capture_output=True, text=True, timeout=300, check=True,
    )
    logger.info("Cloned harness repo (%d entries)", len(list(CACHE_DIR.iterdir())))


def _describe_failure(exc: Exception) -> str:
    if isinstance(exc, FileNotFoundError):
        return "git is not installed, or is not on PATH."
    if isinstance(exc, subprocess.TimeoutExpired):
        return "git clone timed out after 5 minutes."
    if isinstance(exc, subprocess.CalledProcessError):
        return (exc.stderr or "").strip() or f"git clone exited {exc.returncode}."
    return f"{type(exc).__name__}: {exc}"


def _ensure_clone() -> tuple[str, str]:
    """Make sure a clone exists or is on its way. Returns (status, error).

    Status is "ready", "running", or "failed". A failed clone is retried the
    next time someone asks, so a transient network problem is not permanent.
    """
    global _clone_status, _clone_error
    if CACHE_DIR.exists():
        return "ready", ""
    with _clone_lock:
        if _clone_status == "running":
            return "running", ""
        if _clone_status == "failed":
            # Report the failure once, then go back to idle so the next ask
            # retries. Restarting it here instead would mean the message below
            # is never reachable and the user only ever sees "downloading".
            error = _clone_error
            _clone_status, _clone_error = "idle", ""
            return "failed", error
        _clone_status, _clone_error = "running", ""

    def worker():
        global _clone_status, _clone_error
        try:
            _clone_repo()
        except Exception as e:
            message = _describe_failure(e)
            logger.error("harness clone failed: %s", message)
            with _clone_lock:
                _clone_status, _clone_error = "failed", message
        else:
            with _clone_lock:
                _clone_status, _clone_error = "idle", ""

    threading.Thread(target=worker, name="swapharness-clone", daemon=True).start()
    return "running", ""


class SwapHarnessTrick(Trick):
    prompt_keyword = "swapharness"
    replace_system_prompt = True
    __brief__ = "Browse and swap system prompts from AI tool repos"
    __display_name__ = "Swap Harness"

    def install(self) -> None:
        """Clone up front, synchronously: adding a trick is allowed to take a moment."""
        if CACHE_DIR.exists():
            logger.info("Harness repo already cloned at %s", CACHE_DIR)
            return
        try:
            _clone_repo()
        except Exception as e:
            logger.error("git clone failed: %s", _describe_failure(e))
            raise

    def handle_prompt_keyword(self, request: str, messages: list | None = None, payload: dict | None = None) -> dict | None:
        path = request.strip().rstrip("/")
        base = CACHE_DIR

        if not base.exists():
            status, error = _ensure_clone()
            if status == "failed":
                content = (
                    f"Couldn't download the harness library.\n\n{error}\n\n"
                    "Run (swapharness: ) again to retry."
                )
            elif status == "running":
                content = (
                    "Downloading the harness library now \u2014 it's a few hundred "
                    "prompts, so give it a moment.\n\n"
                    "Run (swapharness: ) again shortly and they'll be here."
                )
            else:
                content = "Harness library is ready. Run (swapharness: ) again."
            return {"role": "assistant", "content": content}

        target = base / path if path else base

        if not target.exists():
            return {"role": "assistant", "content": f"Not found: {path}"}

        if target.is_dir():
            entries = sorted(e for e in target.iterdir() if not e.name.startswith("."))
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
                lines.append("(swapharness: <word>) to browse a folder, or (swapharness: path/to/file) to select a harness.")

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

"""Streaming a reply through tricks that rewrite it.

Each trick says how much of the reply its post_hook needs to see at once
(Trick.needs_window): -1 the whole reply, 0 none (it only looks, after the
fact), or N characters. The channel's window is the largest of those, and
-1 wins outright: then the reply is held whole, as before.

With a window of N, the reply streams with only its last N characters held
back. Each time enough new text has arrived, the held-back tail plus the new
text is run through the rewriting post_hooks (as an ordinary assistant
message, the same shape they always get), everything but the last N
characters of the result goes to the client, and those N stay held. At the
end, whatever is left goes through once more along with any tool calls,
which are held until then.

Anything a trick looks for that is at most N characters long is therefore
always seen whole before any of it is sent: if it began in the part already
sent, it would have to be longer than N to still be incomplete. The tail is
already-rewritten text, so nothing has to map rewritten positions back to the
original stream. That asks two things of a trick with a window:

- local: its post_hook is right on any stretch of the reply, not only on
  the whole thing (replacing a character is; "capitalize the first word" isn't)
- idempotent: run again on its own output, it changes nothing, because the
  held-back tail goes through more than once
"""

from __future__ import annotations

from typing import Callable


def channel_window(tricks: list) -> tuple[int, list, list]:
    """(window, rewriters, observers) for these tricks.

    Only tricks with a post_hook count. Rewriters are those with a nonzero
    window; observers (window 0) just look at the finished reply.
    """
    from petsitter.trick import Trick

    hooked = [t for t in tricks if type(t).post_hook is not Trick.post_hook]
    rewriters = [t for t in hooked if _size(t) != 0]
    observers = [t for t in hooked if _size(t) == 0]
    if not rewriters:
        return 0, [], observers
    sizes = [_size(t) for t in rewriters]
    if any(s < 0 for s in sizes):
        return -1, rewriters, observers
    return max(sizes), rewriters, observers


def _size(trick) -> int:
    try:
        return int(trick.needs_window)
    except (TypeError, ValueError):
        return -1   # anything unreadable: play safe and hold the whole reply


class ReplyWindow:
    """One stream of reply text (and its tool calls) passing through a window.

    `rewrite` takes an assistant message ({"role", "content", maybe
    "tool_calls"}) and returns it rewritten.
    """

    def __init__(self, rewrite: Callable[[dict], dict], size: int):
        self.size = max(1, size)
        self._rewrite = rewrite
        self._pending = ""

    def feed(self, text: str) -> str:
        """Take new text from the model; return what can be sent now."""
        self._pending += text
        if len(self._pending) <= self.size:
            return ""
        out = self._rewrite({"role": "assistant", "content": self._pending}).get("content") or ""
        if len(out) <= self.size:
            self._pending = out
            return ""
        self._pending = out[-self.size:]
        return out[:-self.size]

    def finish(self, tool_calls: list | None = None) -> tuple[str, list | None]:
        """The reply has ended: the rest of the text, and the tool calls."""
        if not self._pending and not tool_calls:
            return "", tool_calls
        msg: dict = {"role": "assistant", "content": self._pending}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        msg = self._rewrite(msg)
        self._pending = ""
        return msg.get("content") or "", msg.get("tool_calls") or tool_calls

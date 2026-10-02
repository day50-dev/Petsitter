"""Automatic context compaction for Context Editor: published techniques.

Each technique here is one specific method from one specific source, run as
that source describes it, with its published defaults. None of them reads what
the messages say; they go by role, position and size, so they cost nothing to
run on every request.

They work on OpenAI-shaped messages. ``TECHNIQUES`` is what Context Editor's
Live page lists; docs/compaction.md has the sources and dates.
"""

import json

# What the Live page shows: id -> name, source, link. Order is the order shown.
TECHNIQUES = {
    "observation_masking": {
        "name": "Observation masking",
        "source": "The Complexity Trap (JetBrains, 2025)",
        "url": "https://arxiv.org/abs/2508.21433",
    },
    "clear_tool_uses_20250919": {
        "name": "clear_tool_uses_20250919",
        "source": "Anthropic context editing",
        "url": "https://platform.claude.com/docs/en/build-with-claude/context-editing",
    },
    "only_n_most_recent_images": {
        "name": "only_n_most_recent_images",
        "source": "Anthropic computer-use demo",
        "url": "https://github.com/anthropics/claude-quickstarts/blob/main/computer-use-demo/computer_use_demo/loop.py",
    },
}

# The Complexity Trap's configuration: SWE-agent's LastNObservations, n=10
# (config/default_no_demo_N=1_M=10.yaml), polling left at its default of 1.
MASKING_N = 10
MASKING_POLLING = 1

# clear_tool_uses_20250919's documented defaults.
CLEAR_TRIGGER_INPUT_TOKENS = 100_000
CLEAR_KEEP_TOOL_USES = 3
CLEAR_AT_LEAST_INPUT_TOKENS = None
CLEAR_EXCLUDE_TOOLS: tuple[str, ...] = ()
CLEAR_TOOL_INPUTS = False
# Anthropic doesn't publish the placeholder it uses.
CLEARED_PLACEHOLDER = "[Tool result cleared]"

# The computer-use demo's Streamlit default (3), and its chunk size, which
# loop.py sets to the same number.
IMAGES_KEEP = 3
IMAGES_MIN_REMOVAL_THRESHOLD = 3


def _is_image(part) -> bool:
    return isinstance(part, dict) and (part.get("type") in ("image_url", "image", "input_image")
                                       or "image_url" in part)


def _tokens(value) -> int:
    """Rough tokens, the way the rest of petsitter counts: characters / 4."""
    return len(json.dumps(value, default=str)) // 4


# -- The Complexity Trap: observation masking ---------------------------------

def _content_stats(content) -> tuple[int, int]:
    """(text lines, images), as SWE-agent's _get_content_stats counts them."""
    if isinstance(content, str):
        return len(content.splitlines()), 0
    if not isinstance(content, list):
        return 0, 0
    lines = sum(len(p.get("text", "").splitlines()) for p in content
                if isinstance(p, dict) and p.get("type") == "text")
    return lines, sum(1 for p in content if _is_image(p))


def observation_masking(messages: list, n: int = MASKING_N, polling: int = MASKING_POLLING) -> tuple[list, int]:
    """SWE-agent's LastNObservations: every observation but the last n becomes
    "Old environment output: (k lines omitted)". Observations are tool
    results. SWE-agent also spares its first observation, the task statement;
    in a chat API that's the user's message, which is never masked anyway."""
    observations = [i for i, m in enumerate(messages) if isinstance(m, dict) and m.get("role") == "tool"]
    last_removed = max(0, (len(observations) // polling) * polling - n)
    omit = set(observations[:last_removed])
    if not omit:
        return messages, 0
    out = []
    for i, m in enumerate(messages):
        if i in omit and not str(m.get("content") or "").startswith("Old environment output: ("):
            lines, images = _content_stats(m.get("content"))
            text = f"Old environment output: ({lines} lines omitted)"
            if images > 0:
                text += f" ({images} images omitted)"
            m = {**m, "content": text}
        out.append(m)
    return out, len(omit)


# -- Anthropic: clear_tool_uses_20250919 --------------------------------------

def clear_tool_uses(messages: list,
                    trigger_input_tokens: int = CLEAR_TRIGGER_INPUT_TOKENS,
                    keep_tool_uses: int = CLEAR_KEEP_TOOL_USES,
                    clear_at_least_input_tokens: int | None = CLEAR_AT_LEAST_INPUT_TOKENS,
                    exclude_tools=CLEAR_EXCLUDE_TOOLS,
                    clear_tool_inputs: bool = CLEAR_TOOL_INPUTS) -> tuple[list, int]:
    """Once the prompt is over the trigger, clear the oldest tool results,
    keeping the most recent keep_tool_uses; a cleared result becomes a
    placeholder. Skipped when it would clear less than clear_at_least."""
    if _tokens(messages) <= trigger_input_tokens:
        return messages, 0
    # Tool uses in order: (message index, call index, call id, tool name).
    uses = []
    for i, m in enumerate(messages):
        if isinstance(m, dict) and m.get("role") == "assistant":
            for j, c in enumerate(m.get("tool_calls") or []):
                if isinstance(c, dict):
                    uses.append((i, j, c.get("id"), (c.get("function") or {}).get("name", "")))
    old = uses[:max(0, len(uses) - keep_tool_uses)]
    clear = [u for u in old if u[3] not in set(exclude_tools)]
    if not clear:
        return messages, 0
    ids = {u[2] for u in clear if u[2]}
    out = list(messages)
    saved = 0
    count = 0
    for i, m in enumerate(out):
        if isinstance(m, dict) and m.get("role") == "tool" and m.get("tool_call_id") in ids \
                and m.get("content") != CLEARED_PLACEHOLDER:
            saved += _tokens(m.get("content")) - _tokens(CLEARED_PLACEHOLDER)
            out[i] = {**m, "content": CLEARED_PLACEHOLDER}
            count += 1
    if clear_tool_inputs:
        for i, j, _, _ in clear:
            m = out[i]
            calls = [dict(c) for c in m["tool_calls"]]
            fn = dict(calls[j].get("function") or {})
            if fn.get("arguments") not in ("{}", None):
                saved += _tokens(fn.get("arguments")) - 1
                fn["arguments"] = "{}"
                calls[j]["function"] = fn
                out[i] = {**m, "tool_calls": calls}
    if clear_at_least_input_tokens is not None and saved < clear_at_least_input_tokens:
        return messages, 0
    return out, count


# -- Anthropic computer-use demo: only_n_most_recent_images -------------------

def only_n_most_recent_images(messages: list, images_to_keep: int = IMAGES_KEEP,
                              min_removal_threshold: int = IMAGES_MIN_REMOVAL_THRESHOLD) -> tuple[list, int]:
    """_maybe_filter_to_n_most_recent_images: remove all but the last
    images_to_keep images in tool results, oldest first, in chunks of
    min_removal_threshold so the cached prefix changes less often. As in the
    demo, only images inside tool results count, and nothing replaces them."""
    results = [i for i, m in enumerate(messages)
               if isinstance(m, dict) and m.get("role") == "tool" and isinstance(m.get("content"), list)]
    total = sum(1 for i in results for p in messages[i]["content"] if _is_image(p))
    to_remove = total - images_to_keep
    to_remove -= to_remove % min_removal_threshold
    if to_remove <= 0:
        return messages, 0
    out = list(messages)
    removed = 0
    for i in results:
        kept = []
        for p in out[i]["content"]:
            if _is_image(p) and to_remove > 0:
                to_remove -= 1
                removed += 1
                continue
            kept.append(p)
        if len(kept) != len(out[i]["content"]):
            out[i] = {**out[i], "content": kept}
    return out, removed


_RUN = {
    "observation_masking": observation_masking,
    "clear_tool_uses_20250919": clear_tool_uses,
    "only_n_most_recent_images": only_n_most_recent_images,
}


def compact(messages: list, technique: str) -> tuple[list, int]:
    """(messages, how many things it removed). Unknown or "off": unchanged."""
    run = _RUN.get(technique or "")
    if run is None or not messages:
        return messages, 0
    return run(messages)

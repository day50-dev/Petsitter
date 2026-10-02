# Compaction

Pictures, web searches and file reads stay in a conversation long after
they've done their job, and every turn sends them again. A chat client can
reach the limit of any model this way. Compaction, part of
[Context Editor](tricks.md#context-editor), removes them automatically.

Open Context Editor's **Live** tab and pick one technique under **Compaction**,
below the conversation list. It applies to every request in that channel and is
saved as the extension's `compaction` setting. What it changed is marked in the
chat, with the original underneath.

Each option is one published technique from one source, run as the source
describes it, with its published defaults. None of them reads what the
messages say: they go by role, position and size, so they cost nothing per
request. They run in Context Editor's `pre_hook`, after your own edits. Put
Context Editor first in the channel, so it sees the conversation as your tool
sent it.

Techniques are listed with the date of their source, so the list can be
checked against newer work and the ones that have been overtaken removed.

## Observation masking

From *The Complexity Trap: Simple Observation Masking Is as Efficient as LLM
Summarization for Agent Context Management*, Lindenbauer et al., JetBrains
Research, August 2025 (NeurIPS 2025 DL4Code workshop).
[Paper](https://arxiv.org/abs/2508.21433) ·
[code](https://github.com/JetBrains-Research/the-complexity-trap)

The paper found that replacing old tool output with a placeholder halves cost
and matches LLM summarization's solve rate on SWE-bench Verified.

- Every tool result except the last **10** becomes
  `Old environment output: (N lines omitted)`, plus `(K images omitted)` when
  it had images.
- The model's own messages and tool calls are kept in full.
- Defaults: the paper's configuration
  (`config/default_no_demo_N=1_M=10.yaml`), which is SWE-agent's
  `LastNObservations` with `n: 10` and `polling: 1`.
- SWE-agent never masks its first observation, the task statement. Through a
  chat API the task is the user's message, which isn't a tool result, so it is
  never masked either.

## clear_tool_uses_20250919

Anthropic's context editing strategy, released September 2025.
[Docs](https://platform.claude.com/docs/en/build-with-claude/context-editing)

- Once the request is over **100,000** input tokens (`trigger`), the oldest
  tool results are cleared, keeping the **3** most recent tool uses (`keep`).
- Tool calls are kept (`clear_tool_inputs: false`); no tool is excluded
  (`exclude_tools`); no minimum to clear (`clear_at_least`).
- Anthropic runs this on its servers. Petsitter runs it itself, so it works
  with any provider; tokens are estimated as characters / 4.
- Anthropic doesn't publish its placeholder text. A cleared result here reads
  `[Tool result cleared]`.

## only_n_most_recent_images

From Anthropic's computer-use demo (`claude-quickstarts`,
`computer-use-demo/computer_use_demo/loop.py`), October 2024.
[Code](https://github.com/anthropics/claude-quickstarts/blob/main/computer-use-demo/computer_use_demo/loop.py)

- Keeps the **3** most recent images in tool results and removes older ones,
  oldest first.
- Removes them in chunks of **3** (`min_removal_threshold`), so the start of
  the conversation, and the provider's prompt cache, changes only every few
  images.
- Defaults: the demo's (`only_n_most_recent_images = 3`, and the chunk size
  set to the same number).
- As in the demo, only images inside tool results count, such as screenshots.
  Images you paste into your own messages are left alone, and nothing is put
  in place of a removed image.

## Not offered

Other well-known methods can't run as published inside a proxy, so they
aren't options:

- [AgentDiet](https://arxiv.org/abs/2509.23586) (September 2025) decides what
  to drop with an LLM.
- [StreamingLLM](https://arxiv.org/abs/2309.17453) (September 2023) keeps
  attention sinks in the model's KV cache, not in messages.
- ACON, Context-Folding and AgentFold need an LLM compressor or a trained
  agent.

# What each bundled trick does

The tricks that ship with petsitter, what each one is for, and how to turn it on.

[← back to the README](../README.md)

---

## Reference Templates

Tricks are managed with `pet` and grouped into [tricksets](tricksets.md#tricksets); there are no
per-trick command-line flags. Point petsitter at a model once, make a trickset,
and add tricks to it:

```bash
pet model default url http://localhost:11434
pet model default model qwen3:8b

pet new mine                    # create a trickset (X-Title '*', Model '*')
pet add mine json_mode          # add a trick to it
petsitter                       # start; settings come from the config file
```

Every example below assumes that, so it only shows the `pet add` line. Swap
`mine` for whichever trickset you're building, or use the dashboard's
Available Tricks list instead.

### Output Control

 * [JSON Mode](#json-mode) - Enforce valid JSON output
 * [Code Validator](#code-validator) - Self-healing validation through model self-description

### Capability Injection

 * [Tool Calling](#tool-calling) - Add tool calling to models without native support
 * [Conversational Tool](#conversational-tool) - ANDYBOT persona tool calling for small/older models 
 * [MCP Tools](#mcp-tools) - Inject tools from an mcp.json file into any harness

### Pipeline

 * [Kennel](#kennel) - Route cognitive subtasks to specialized models
 * [Multi-Model Consultant](#multi-model-consultant) - Two models cross-validate and improve each other's responses

### Security

 * [Secrets Protector](#secrets-protector) - Detect and pseudonymize secrets/PII before they reach the model

### Agent

 * [Swap Harness](#swap-harness) - Browse and swap system prompts from AI tool repositories
 * [Self-Improver](#self-improver) - Runtime agent that can add, modify, and list tricks

### Utility

 * [Rules File](#rules-file) - Inject a shared AGENTS.md-style rules file into the system prompt
 * [Reference Check](#reference-check) - Challenge answers that cite no valid reference from a retrieval tool
 * [Recommender List](#recommender-list) - Make the model pick software from your preferred list
 * [Export It](#export-it) - Export conversation as llcat-compatible JSON
 * [Traffic Logger](#traffic-logger) - Log every request/response as timestamped JSONL to a file

---

### JSON Mode

[tricks/json_mode.py](tricks/json_mode.py)

Enforces valid JSON output by adding formatting instructions to the system prompt, stripping markdown code blocks, and retrying with feedback if the response isn't valid JSON.

```bash
pet add mine json_mode
```

### Code Validator

[tricks/code_validator.py](tricks/code_validator.py)

After the model proposes a code change, asks it to describe what the change does, compares the description against the original user request, and retries with feedback if they don't match.

```bash
pet add mine code_validator
```

### Tool Calling

[tricks/tool_call.py](tricks/tool_call.py)

Enables tool calling for models without native support by injecting tool definitions into the prompt, parsing JSONRPC-style tool call responses, and converting them to OpenAI `tool_calls` format.

```bash
pet add mine tool_call
```

### Conversational Tool

[tricks/conversational_tool.py](tricks/conversational_tool.py)

A conversational approach to tool calling that uses the ANDYBOT persona instead of structured JSON output. The model says `DEAR ANDYBOT, <FUNCTION>` and ANDYBOT collects each parameter through dialogue:

1. Model recognises it needs to call a tool and says `DEAR ANDYBOT, GET_WEATHER`
2. ANDYBOT asks: *"Can you provide location?"*
3. Model responds: `Paris`
4. ANDYBOT builds the tool call and returns it to the application

This works well with small models (3B and under) and older models that struggle with reliable JSON output or native `tool_calls`. The conversational flow lets them express intent naturally instead of wrestling with syntax. It also supports inline arguments (`DEAR ANDYBOT, GET_WEATHER location=Paris`), optional parameters, and "I am confused"/"skip" recovery. The persona is only injected when the request actually carries `tools`.

```bash
pet add mine conversational_tool
pet add mine json_mode
```

### MCP Tools

[tricks/mcp_tools.py](tricks/mcp_tools.py)

Injects tools defined in an [mcp.json](https://github.com/sourcey/mcp-schema) file into any harness. Converts MCP tool definitions to OpenAI function-calling format and merges them into `params["tools"]`. Tools with name collisions take precedence over existing tool definitions.

Default path: `~/.config/petsitter/mcp.json`. Use the `mcp` prompt keyword to switch files at runtime.

```bash
pet add mine mcp_tools          # reads ~/.config/petsitter/mcp.json
```

To point it at a different file, use the `mcp` prompt keyword in a message:
`(mcp: /path/to/my-tools.json)`.

The `mcp.json` format follows the [MCP spec](https://modelcontextprotocol.io):
```json
{
  "mcpSpec": "1.0.0",
  "server": { "name": "my-tools", "version": "1.0.0" },
  "tools": [
    {
      "name": "search_docs",
      "description": "Search documentation by query",
      "inputSchema": {
        "type": "object",
        "properties": {
          "query": { "type": "string" },
          "limit": { "type": "number", "default": 10 }
        },
        "required": ["query"]
      }
    }
  ]
}
```

### Multi-Model Orchestration

A trick has full control of the request lifecycle - it can call any number of models, not just the one the user pointed at. This lets you decompose a problem into subtasks and route each one to the model best suited for it.

Petsitter supports this through **model configs** - JSON files that map role names to `{url, model, key}` objects. Tricks declare what roles they need; if a key is missing, petsitter prints a helpful error.

The `model` and `key` fields can be a string or boolean `false` - `false` means passthrough (don't set the field in the upstream request). This is distinct from `""` which clears the value.

Example `modelset.json`:
```json
{
    "default": {
        "url": "http://localhost:11434",
        "model": "Qwen3.5:8b"
    },
    "thinker": {
        "url": "http://localhost:11434",
        "model": "VibeThinker-3B-GGUF:q4_K_M"
    },
    "toolcall": {
        "url": "http://localhost:11434",
        "model": "lfm2.5:latest",
        "key": "sk-custom-key"
    }
}
```

#### Kennel

[tricks/kennel.py](tricks/kennel.py) is a reference implementation of the pattern above. It routes cognitive subtasks to three specialized models running in parallel - a **thinker** for chain-of-thought, a **tool-caller** for deciding which tools to invoke, and an **emitter** for generating the final response.

```bash
# Pull three small models that together fit on modest hardware (< 6B total)
ollama pull VibeThinker-3B    # reasoning / chain-of-thought
ollama pull LFM2.5-230M       # tool-calling (tiny, fast)
ollama pull Qwen3.5-2B        # response generation

# Each model sees a context optimized for its role
pet new kennel-demo
pet add kennel-demo kennel

# KennelTrick needs three model roles; scope them to this trickset
pet model thinker  url http://localhost:11434 --trickset kennel-demo
pet model thinker  model VibeThinker-3B-GGUF:q4_K_M --trickset kennel-demo
pet model toolcall url http://localhost:11434 --trickset kennel-demo
pet model toolcall model LFM2.5-230M --trickset kennel-demo
pet model default  url http://localhost:11434 --trickset kennel-demo
pet model default  model Qwen3.5-2B --trickset kennel-demo
```

The Models tab in the dashboard does the same thing with fewer keystrokes.

Pipeline:
1. **Thinker** gets the conversation + "think step by step" → produces reasoning
2. **Tool-caller** (if tools are present) gets context + reasoning + tool definitions → decides which tool to call
3. **Emitter** receives the enriched context and generates the final response

Kennel is one architecture; you could write a trick that routes by language, by file type, by user role, or by anything else you can express in a `post_hook`.

#### Multi-Model Consultant

[tricks/multiconsult.py](tricks/multiconsult.py)

Cross-validates responses between two models through iterative refinement and voting. Requires a `default` model and a `consultant` model in the modelset.

Pipeline per round:
1. **model1's** response (from the proxy call) is sent to **model2** for improvement
2. **model2** generates a fresh response to the original prompt
3. **model1** improves model2's fresh response
4. Both models vote on which improved output is better
5. If they agree, return the winner; if not, repeat once more
6. On second disagreement, randomly pick one as fallback

```bash
pet new consult
pet add consult multiconsult
pet model consultant url http://localhost:11434 --trickset consult
pet model consultant model qwen3:8b --trickset consult
```

Example `modelset.json`:
```json
{
    "default": {
        "url": "http://localhost:11434",
        "model": "llama3:8b"
    },
    "consultant": {
        "url": "http://localhost:11434",
        "model": "qwen3:8b"
    }
}
```

### Secrets Protector

[tricks/secrets_protector.py](tricks/secrets_protector.py)

Detects and pseudonymizes sensitive information before it reaches the model, then restores original values in the response:

- **Detection** - regex patterns for API keys (OpenAI, Anthropic, AWS, Google, Stripe), tokens (JWT, GitHub, Slack, Bearer), credentials (database URLs, private keys), and PII (emails, phones, SSNs, credit cards, IPs)
- **Format-preserving substitutes** - realistic replacements (e.g., `alice@example.com` → `user.0001@sanitized.local`) that preserve token boundaries so the model's tokenizer doesn't conflate distinct entries
- **Bidirectional vault** - consistent pseudonyms across the session (same secret → same substitute) with automatic restoration in both natural-language responses and tool call arguments

```bash
pet add mine secrets_protector
```

### Swap Harness

[tricks/swapharness.py](tricks/swapharness.py)

Browses and swaps system prompts from the [system-prompts-and-models-of-ai-tools](https://github.com/x1xhlol/system-prompts-and-models-of-ai-tools) repository. On first use, it clones the repo into `~/.config/petsitter/harnesses/`.

Use the `swapharness` prompt keyword to navigate the directory tree and select a system prompt file. The selected content is injected into the system prompt on every request until a different file is chosen or the trick is uninstalled.

```bash
pet add mine swapharness    # adding it runs install(), which clones the repo
```

Once installed, include `(swapharness: path)` in any user message to browse or select a harness:

```
User: (swapharness: Cursor Prompts)
Assistant: 📁 Cursor Prompts
           📄 Rules for All Models.md
           📄 Rules for Cursor.md
           📄 ...

User: (swapharness: Cursor Prompts/Rules for All Models.md)
Assistant: ✅ Harness set to Cursor Prompts/Rules for All Models.md (2847 chars)
           ────────────────────────────────────────────────
           You are Cursor, an advanced AI coding assistant...
```

The selected system prompt is prepended to every subsequent request. Run `(swapharness: install)` to clone the repo, or use the lifecycle CLI:

```bash
pet install swapharness             # clone the repo
pet uninstall swapharness           # remove the repo
pet lifecycle swapharness startup   # init per-session state
pet lifecycle swapharness shutdown  # cleanup session
```

### Self-Improver

[tricks/self_improver.py](tricks/self_improver.py)

Watches for the prompt keyword `petsitter` in your messages. When it sees `(petsitter: <request>)`, it strips the tag and spawns an agent loop with the default model. The agent has tools to add, modify, and list trick files - it reads instructions from `.agents/skills/self-improver/SKILL.md` to understand the petsitter trick API and conventions.

This is a reference implementation for the **prompt keywords** pattern (see below).

```bash
pet add mine self_improver
```

Example usage:
```
User: (petsitter: add a trick that logs every request to a file)
Model: Creates tricks/request_logger.py and explains how to load it
User: explain the CAP theorem (petsitter: add a thinking mode)
Model: Explains CAP theorem (tag stripped, petsitter handled separately)
```

### Export It

[tricks/exportit.py](tricks/exportit.py)

Exports the conversation history as an [llcat](https://github.com/day50-dev/llcat)-compatible JSON file. The output is the raw message array format used by OpenAI-compatible APIs, making it interoperable with llcat, prompt tools, and anything that speaks the Chat Completions message schema.

Use the `exportit` prompt keyword in any message to trigger the export:

```bash
pet add mine exportit
```

```
User: (exportit)
Assistant: Conversation exported to `/tmp/petsitter/convo-20260718-143022.json` (6 messages, llcat-compatible)

User: (exportit: backup before refactor)
Assistant: Conversation exported to `/tmp/petsitter/convo-20260718-143022.json` (6 messages, llcat-compatible)
Note: backup before refactor
```

The exported JSON is a plain array of messages in OpenAI Chat Completions format:

```json
[
  { "role": "system", "content": "You are a helpful assistant." },
  { "role": "user", "content": "What is the CAP theorem?" },
  { "role": "assistant", "content": "The CAP theorem states...", "tool_calls": [] },
  { "role": "user", "content": "Can you give an example?" },
  { "role": "assistant", "content": "Sure! Consider a distributed..." }
]
```

Tool calls, reasoning (chain-of-thought), and tool results are all preserved in their standard formats. You can load the exported file directly with `llcat -c convo.json` or pipe it into any OpenAI-compatible tool.

### Rules File

[tricks/rules_file.py](tricks/rules_file.py)

Reads a plain-markdown rules file (AGENTS.md / CLAUDE.md style) and injects its content into the system prompt on every request. Because petsitter sits in front of any tool pointed at it, the same rules file applies across opencode, Claude Code, Codex, etc. - write the rules once and keep every harness consistent.

The rules path is configured per-trickset (the scope where petsitter config lives): set the `rules_path` config field on the trick via the dashboard, or switch files at runtime with the `rules` prompt keyword:

```bash
pet add mine rules_file
```

```
User: (rules: /path/to/rules.md)
Assistant: Loaded 123 chars of rules from /path/to/rules.md

User: (rules)
Assistant: Rules loaded from /path/to/rules.md (123 chars)
```

Content is cached and reloaded when the path changes, on startup, or on request. With no path configured the trick stays dormant, so requests pass through untouched.


### Recommender List

[tricks/recommender_list.py](tricks/recommender_list.py)

Keeps a list of the software you actually want used - your database, your package manager, your HTTP client - and injects it into the system prompt, so when the model reaches for "a database" it reaches for yours instead of whatever was most common in its training data. It also carries a do-not-reach-for side, for the things you have already decided against.

The list is configured per-trickset: point `recommender_path` at a text file, put entries inline in `recommendations`, or both. Set `strict` to forbid off-list choices outright instead of asking the model to justify a deviation.

```bash
pet add mine recommender_list
```

The file format is one entry per line, `#` starts a comment:

```
# my stack
database: postgres (already in prod)
package manager: uv
http client: httpx
avoid: mongodb (ops burden)
!jquery
ripgrep
```

A line with a colon (or `=`) is a category choice, a line starting with `!` or `avoid:` / `never` is something to steer away from, and a bare line is a general preference with no category. A trailing `(...)` is kept as a note and passed to the model, so "why" travels with the choice. One category holds one choice - a later entry for the same category replaces the earlier one, which is how inline `recommendations` override the file.

Edit the list at runtime with the `recommend` prompt keyword:

```
User: (recommend)
Assistant: Recommender list (3 entries, from /home/me/.config/petsitter/stack.txt):
           - database: postgres (already in prod)
           - package manager: uv
           - avoid mongodb (ops burden)

User: (recommend: http client = httpx)
Assistant: Recommending: http client = httpx.
           Saved to /home/me/.config/petsitter/stack.txt.

User: (recommend: avoid jquery)
Assistant: Recommending: avoid jquery.
           Saved to /home/me/.config/petsitter/stack.txt.

User: (recommend: drop database)
Assistant: Dropped: database = postgres. Saved to /home/me/.config/petsitter/stack.txt.

User: (recommend: reload)
Assistant: Reloaded the recommender list.
           ...
```

Additions and drops are written back to the file when one is configured, so the list survives a restart; with no file they last for the session. Use `(recommend: reload)` after editing the file by hand. With an empty list the trick stays dormant, so requests pass through untouched.



### Reference Check

[tricks/reference_check.py](tricks/reference_check.py)

Catches the most common shape of hallucination in a retrieval setup: the model either never consults its reference tool, or consults it, finds nothing useful, and answers from memory anyway — sounding exactly as confident as when it is right.

Every result coming back from a reference-ish tool is stamped with an unforgeable `ref_id`, and the model is required to attribute its claims to those ids. A fabricated id is caught immediately:

```
User: What is the Cascade valve rated to?

  (model answers "900 PSI", citing <reference_id: #131>)
  (petsitter: that id was never issued — challenge, content re-presented)
  (model answers "400 PSI", citing ref_id:d65b74455d76)

Assistant: The Cascade valve is rated to 400 PSI.
```

```bash
pet add mine reference_check
```

**It is invisible.** The stamps exist only in the payload sent upstream; the attribution block exists only in the response coming back; both are gone before anything leaves petsitter. The response body a client receives is byte-identical to what the model produced — no badge, no checkmark, no note about what was validated, even when the check fails. That is a correctness requirement rather than a stylistic one: the output may be JSON, graph triples, or anything else with a parser waiting on the other end, and a trick that pollutes it breaks the consumer (and every structural trick stacked after it, such as [JSON Mode](#json-mode)).

#### How it works

**Stamping.** Petsitter never executes the RAG tool — your harness does. But both halves of the round trip pass through the proxy: the tool call goes out in one response, and the tool result comes back in the *next* request as a `role: "tool"` message. So `pre_hook` stamps the result on its way upstream. Nothing needs to integrate with anything; any RAG tool, MCP server, or harness works untouched.

Structured results keep their structure — the id goes in as a `ref_id` field, so anything parsing the tool output still can. Prose results get a stamp per paragraph. Either way you also get one id for the result as a whole.

**Unforgeable, and stateless.** `ref_id = HMAC(per-process secret, tool_call_id + chunk)[:12]`. The HMAC matters twice over. It is *deterministic*, so re-stamping the same chunk on every turn yields the same id — which it must, because your harness resends its own unstamped transcript each time. And it is *unguessable*, so the model cannot manufacture one. Verification then needs no ledger at all: recompute what was issued from the transcript in hand.

That is also why **loading the trick mid-conversation works retroactively** — the first request after you load it stamps every tool result already in the history. Unloading is equally clean; there is no residue in the transcript.

**The challenge.** If the answer carries no valid id, the trick spends tokens rather than failing. It re-presents the retrieved material with its ids and asks again, up to `max_rounds` times. Three situations, three challenges:

| Situation | What happens |
|---|---|
| Cited an id that was never issued | Challenged, and the fabricated id is named |
| Retrieved content present, nothing attributed | Challenged with the content re-presented |
| Never called the tool at all | Challenged to go retrieve; if it responds with a tool call, that goes to your harness to execute |

**`ref_id:none` is a first-class answer.** A model that cannot source a claim is expected to say so, and saying so passes the check. This is load-bearing: if the only outcome of failing were punishment, the cheapest escape would be to forge a *better* id — citing a real id that does not support the claim. An honest exit makes honesty the path of least resistance.

If the model still cannot attribute its answer after `max_rounds`, **the answer is passed through untouched.** The trick is a diagnostic, not a blocker.

#### Configuration

| Field | Default | What it does |
|---|---|---|
| `tool_patterns` | `search,find,research,reference,lookup,retrieve,query,manual,knowledge,doc,wiki,rag,kb,grep,fetch` | Comma-separated substrings. A tool counts as a reference lookup when any appears in its **name or description** — MCP tools are often named `mcp__ctx7__get` while describing themselves plainly. |
| `max_rounds` | `3` | Challenges before giving up. |
| `challenge_missing_call` | `true` | Also challenge answers given without calling a reference tool. The most common failure, and the noisiest check — it fires on any turn that skipped retrieval. |

With no reference-ish tool in the request, the trick is completely dormant.

Per-request state (which tools were in scope, what was stamped) rides the [request metadata channel](writing-tricks.md#request-metadata) rather than the trick instance, so concurrent requests through the same trickset cannot influence each other's verdicts.

#### Reading the results

Nothing is reported in the response, so the tally comes out of the logger (visible in the dashboard's Logs tab) or on demand:

```
User: (refcheck)
Assistant: Reference check: 12 answers checked, 3 challenged, 1 fabricated ids caught,
           2 claims the model admitted it could not source, 0 passed through after
           exhausting challenges.
```

A run that fires **zero** challenges is a real result — it says the model was not guessing, and you can unload the trick.

#### What it does not do

This is a heuristic, and it buys a large reduction rather than a guarantee. A model can cite a perfectly valid id and still misrepresent what that passage says — quote `ref_id:1313` for the reigning monarch and then attach the same id to a claim about cuttlefish. Nothing here catches that.

It is rarer than it sounds, though, and for a structural reason. To cite an id at all the model has to have attended to that span, since the id exists nowhere else; hallucination is largely what happens when generation runs off parametric memory without looking at the context. Misattribution requires reading the chunk closely enough to lift its id and ignoring it closely enough to say something unrelated. So the main effect is less "we caught a liar" than "we forced attention onto the source."

The corollary is worth keeping in mind: the residual errors that survive this check are *more* dangerous per unit than the ones you started with, because they now read as sourced. Catching those needs a per-claim entailment check against the cited chunk — a model call per claim, a different cost class entirely.

### Traffic Logger

[tricks/logger.py](tricks/logger.py)

Appends one timestamped JSON line to a JSONL file for every request and every response that passes through the trick — the debugging view into whatever harness is misbehaving. Point it at a file, reproduce the problem, and you have the full traffic in both directions: payloads, tools, and messages on the way out; the message list and model answer on the way back.

```bash
pet add mine logger            # writes ~/.cache/petsitter/traffic.jsonl
```

Each request produces two records:

```jsonl
{"timestamp":"2026-08-29T12:00:00.000+00:00","trick":"LoggerTrick","event":"request","direction":"out","request_id":"ab12cd34","x_title":"opencode*","model":"qwen3:8b","stream":false,"tools":[],"payload":{"model":"qwen3:8b","messages":[{"role":"user","content":"hello"}]},"messages":[{"role":"user","content":"hello"}]}
{"timestamp":"2026-08-29T12:00:00.150+00:00","trick":"LoggerTrick","event":"response","direction":"in","request_id":"ab12cd34","x_title":"opencode*","model":"qwen3:8b","messages":[...],"answer":{"role":"assistant","content":"hi!"}}
```

Records come straight off the hooks:

- **`request`** fires in `pre_hook` and carries the full payload and message list heading up — whatever the tricks before it have already done to the conversation.
- **`response`** fires in `post_hook` with the message list including the model's answer.

The trick is passive — both hooks return the context untouched, so nothing it logs changes what the model sees.

#### Ordering matters

Hooks run in trickset order, so where the logger sits decides what it sees:

- **First in the trickset** it records traffic as it arrives from the client.
- **Last in the trickset** it sees the messages after every other trick has rewritten them.

A trick that short-circuits the pipeline (a prompt-keyword handler, or a `post_hook` that replaces the answer) prevents everything after it from running — so add the logger first (or `pet reorder mine logger 0`) to guarantee a record.

#### Configuration

| Field | Default | What it does |
|---|---|---|
| `path` | `~/.cache/petsitter/traffic.jsonl` | Where the JSONL file is written. A directory path (or one ending in `/`) writes `traffic.jsonl` inside it. Parent directories are created on demand. |




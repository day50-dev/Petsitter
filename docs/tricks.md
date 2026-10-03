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
 * [Multi-Round](#multi-round) - Self-critique and revision, when you ask for it
 * [No Em-Dash](#no-em-dash) - Replace em-dashes with hyphens
 * [Politeify](#politeify) - Rewrite rude or shouting messages politely before the model sees them

### Capability Injection

 * [Tool Call](#tool-call) - Add tool calling to models without native support
 * [XML Tool](#xml-tool) - XML-style tool calling for small models
 * [Conversational Tool](#conversational-tool) - ANDYBOT persona tool calling for small/older models 
 * [MCP Tools](#mcp-tools) - Inject tools from an mcp.json file into any harness

### Pipeline

 * [Kennel](#kennel) - Route cognitive subtasks to specialized models
 * [Multi-Model Consultant](#multi-model-consultant) - Two models cross-validate and improve each other's responses

### Diagnostics

 * [Context Editor](#context-editor) - Edit what the model sees, either side: unstick a refusal, or cut images and tool output that are taking up room
 * [Context Monitor](#context-monitor) - See what your AI tool sends: who sent each request, its system prompt, messages, tools, and their size
 * [Tool Dashboard](#tool-dashboard) - See the tools offered and called, with their output, and switch any tool off

### Security

 * [Secrets Protector](#secrets-protector) - Hide passwords, API keys and personal details from the model, and put them back in its replies

### Agent

 * [Swap Harness](#swap-harness) - Browse and swap system prompts from AI tool repositories
 * [Self-Improver](#self-improver) - Runtime agent that can add, modify, and list tricks
 * [Expose Petsitter](#expose-petsitter) - Tells the model petsitter is there, and lets it see (and if you allow it, change) the extensions

### Utility

 * [Rules File](#rules-file) - Inject a shared AGENTS.md-style rules file into the system prompt
 * [Reference Check](#reference-check) - Challenge answers that cite no valid reference from a retrieval tool
 * [Recommender List](#recommender-list) - Make the model pick software from your preferred list
 * [Export It](#export-it) - Save the conversation as llcat-compatible JSON, before or after the extensions
 * [Traffic Logger](#traffic-logger) - Log every request as JSONL, before and after the extensions transform it

---

### JSON Mode

[tricks/json_mode.py](../src/petsitter/tricks/json_mode.py)

Enforces valid JSON output by adding formatting instructions to the system prompt, stripping markdown code blocks, and retrying with feedback if the response isn't valid JSON.

```bash
pet add mine json_mode
```

### Code Validator

[tricks/code_validator.py](../src/petsitter/tricks/code_validator.py)

After the model proposes a code change, asks it to describe what the change does, compares the description against the original user request, and retries with feedback if they don't match (up to 3 attempts). It costs two extra model calls per response and runs on every response, code or not, so it fits a trickset used only for code editing.

```bash
pet add mine code_validator
```

### Multi-Round

[tricks/multiround.py](../src/petsitter/tricks/multiround.py)

Include the word `multiround` in a message and the model answers, critiques its answer, and rewrites it. Only that message is affected, and the word is removed before the model sees it. The reply has both drafts:

```
<first_pass> ...original answer... </first_pass>
<revised> ...critiqued and improved answer... </revised>
```

Known issue: the critique round calls the async `callmodel` without awaiting it, so it currently fails instead of running.

```bash
pet add mine multiround
```

### No Em-Dash

[tricks/no_emdash.py](../src/petsitter/tricks/no_emdash.py)

Asks the model not to use em-dashes (U+2014) and replaces any in the reply with `-`. En-dashes and tool call arguments are left alone. Replies still stream, one character held back.

```bash
pet add mine no_emdash
```

### Politeify

[tricks/politeify.py](../src/petsitter/tricks/politeify.py)

Rewrites a hostile or sweary message into a polite one before the model sees it, keeping what you asked for and how urgent it is:

```
You type:        why the hell is this stupid test still failing, fix it
Model receives:  Could you help me understand why this test is still failing and fix it?
```

Only messages that might need it are rewritten: ones that hit a word list (outside code blocks) or have three or more ALL-CAPS words in a row. The word list is the LDNOOBW list (`data/profanity-en.txt`, CC BY 4.0) plus common swearing and name-calling it leaves out. Everything else goes through as written.

The rewrite is done by a model named `rephraser` if you add one under Models (an older `politeify` entry also works), otherwise `default`. Any chat model works; an uncensored one keeps the most of your urgency. It's handed a conversation in which it already agreed to send back only a code block holding the rewrite; a reply without a code block is ignored and the message goes as written. Nothing is masked or blocked.

Rewrites are cached (the 200 most recent), so earlier messages your tool resends are swapped without another call. A failed rewrite sends the message unchanged. Each rewrite and failure shows on the Live tab.

```bash
pet add mine politeify
pet model rephraser model qwen3:8b --trickset mine    # optional
```

| Field | Default | What it does |
|---|---|---|
| `min_length` | `12` | Messages shorter than this many characters are left alone. |

<a id="tool-calling"></a>
### Tool Call

[tricks/tool_call.py](../src/petsitter/tricks/tool_call.py)

Enables tool calling for models without native support by injecting tool definitions into the prompt, parsing JSON-RPC-style tool call responses, and converting them to OpenAI `tool_calls` format:

```
Model writes:  {"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"read_file","arguments":{"path":"app.py"}}}
Tool receives: tool_calls: [{"type": "function", "function": {"name": "read_file", ...}}]
```

If the model turns out to return native `tool_calls`, the trick stops adding its instructions.

```bash
pet add mine tool_call
```

### XML Tool

[tricks/xml_tool.py](../src/petsitter/tricks/xml_tool.py)

A simpler tool-call format for very small models that can't produce reliable JSON. The model writes:

```
<tool>list_files</tool>
<args>{"path": "~/mp3"}</args>
```

and your tool gets a normal `tool_calls` entry. Only tool names and descriptions are put in the system prompt, not parameter schemas. Like Tool Call, it steps aside if the model returns native `tool_calls`. Use it instead of, not alongside, the other tool-calling tricks.

```bash
pet add mine xml_tool
```

### Conversational Tool

[tricks/conversational_tool.py](../src/petsitter/tricks/conversational_tool.py)

A conversational approach to tool calling that uses the ANDYBOT persona instead of structured JSON output. The model says `DEAR ANDYBOT, <FUNCTION>` and ANDYBOT collects each parameter through dialogue:

1. Model recognises it needs to call a tool and says `DEAR ANDYBOT, GET_WEATHER`
2. ANDYBOT asks: *"ANDYBOT WOULD LIKE TO KNOW: location (required)?"*
3. Model responds: `Paris`
4. ANDYBOT builds the tool call and returns it to the application

This works well with small models (3B and under) and older models that struggle with reliable JSON output or native `tool_calls`. The conversational flow lets them express intent naturally instead of wrestling with syntax. It also supports inline arguments (`DEAR ANDYBOT, GET_WEATHER location=Paris`), optional parameters, and "I am confused"/"skip" recovery. The persona is only injected when the request actually carries `tools`.

```bash
pet add mine conversational_tool
pet add mine json_mode
```

### MCP Tools

[tricks/mcp_tools.py](../src/petsitter/tricks/mcp_tools.py)

Injects tools defined in an [mcp.json](https://github.com/sourcey/mcp-schema) file into any harness. Converts MCP tool definitions to OpenAI function-calling format and merges them into `params["tools"]`; an MCP tool replaces a client tool with the same name. Your harness still runs the calls.

The file is set with the `mcp_path` setting (default `~/.config/petsitter/mcp.json`).

```bash
pet add mine mcp_tools          # reads ~/.config/petsitter/mcp.json
```

To switch files at runtime, use the `mcp` prompt keyword in a message:
`(mcp: /path/to/my-tools.json)`. `(mcp)` alone shows what's loaded.

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

[tricks/kennel.py](../src/petsitter/tricks/kennel.py) is a reference implementation of the pattern above. It routes cognitive subtasks to three specialized models running in parallel - a **thinker** for chain-of-thought, a **tool-caller** for deciding which tools to invoke, and an **emitter** for generating the final response.

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

[tricks/multiconsult.py](../src/petsitter/tricks/multiconsult.py)

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

### Context Editor

[tricks/context_editor.py](../src/petsitter/tricks/context_editor.py)

Edit the conversation the model sees, either side of it, from its **Live** tab:

- **A refusal sticks.** "I'm not allowed to look at emails" to a reasonable request, and from then on the model reads its own refusal and keeps refusing. Edit that reply to "Sure, happy to help with that" and it carries on as if it had agreed.
- **The context fills up.** Images and tool output (web searches, file reads) stay in the history after they've done their job, and every turn pays for them again. Remove them and a short note takes their place; a tool output keeps its link to its call.

Recent conversations show as chats, you on the right and the model on the left, with a size bar for the whole conversation and a bar for each message's share of it. Every message, yours or the model's, has **edit**, **remove**, **remove images** and **revert**; the model's newest reply is editable as soon as it comes back. Your tool keeps resending the original, and petsitter swaps in your version on every request. Edits apply only to the conversation they were made in and are kept in memory, for the 30 most recent conversations (a restart drops them). Put it first in the channel.

**Compaction**, below the conversation list, does the cutting automatically on every request in the channel. Pick one published technique: *Observation masking* ([The Complexity Trap](https://arxiv.org/abs/2508.21433)), *clear_tool_uses_20250919* ([Anthropic context editing](https://platform.claude.com/docs/en/build-with-claude/context-editing)) or *only_n_most_recent_images* ([Anthropic's computer-use demo](https://github.com/anthropics/claude-quickstarts/blob/main/computer-use-demo/computer_use_demo/loop.py)). What it changed is marked in the chat. Details and defaults: [Compaction](compaction.md).

```bash
pet add mine context_editor
```

### Context Monitor

[tricks/context_monitor.py](../src/petsitter/tricks/context_monitor.py)

See what your tool sends. Records every request and shows it on its Live tab: where it came from (address, X-Title, User-Agent, channel, model), what the context is made of (system prompt, tool definitions, conversation, tool results, as estimated tokens and a share of the whole), how the conversation's size grows and drops when it's compacted, and the full system prompt (flagged when it changed), tools and messages. Sizes are characters / 4. The full text of the last 30 requests is kept.

It's on by default, first in line, and the third step of Start here opens it. Put it first in the channel, so it sees what your tool sent.

```bash
pet add mine context_monitor
```

### Tool Dashboard

[tricks/tool_monitor.py](../src/petsitter/tricks/tool_monitor.py)

Shows, per request, the tools your tool offered the model, which ones an extension withheld (and which extension), and every call the model made, parallel calls shown separately. Click a tool to see its calls: the arguments, the output (first 2,000 characters), and a **download** of the call's full arguments and output as JSON. **demo** on the Live tab plays made-up traffic.

Each tool has a switch: turn one off and the model isn't offered it from the next request on, even a tool built into your AI tool. The switches set `blocked_tools`, which is saved with the channel.

It's on by default. Put it first in the channel, so its "offered" list is what your tool really sent. The same events also go to a unix socket that `contrib/toolwatch.py` (terminal) and `contrib/toolwatch_web.py` (browser) listen on.

```bash
pet add mine tool_monitor
```

| Field | Default | What it does |
|---|---|---|
| `blocked_tools` | (empty) | Comma-separated tools the model isn't offered. Set by the Live tab's switches. |
| `include_schemas` | `false` | Also send each tool's full parameter schema. Events get much larger. |
| `socket_path` | `~/.cache/petsitter/toolmon.sock` | Unix datagram socket for the terminal and browser viewers. Events are dropped when nothing listens. |

### Secrets Protector

[tricks/secrets_protector.py](../src/petsitter/tricks/secrets_protector.py)

Finds secrets in what your tool sends, swaps each for an opaque stand-in like `__96178c403fd9__d4360d48-...` before the model sees it, and puts the real value back wherever the stand-in comes back: in the reply, and in the arguments of the model's tool calls.

- **Detection** combines three sources, since no one covers what people paste into a chat:
  - [gitleaks](https://github.com/gitleaks/gitleaks)' rules (bundled, MIT) for about 200 kinds of vendor keys and tokens.
  - [detect-secrets](https://github.com/Yelp/detect-secrets)' keyword detector for values named as secrets: `"password": "..."`, `api_key = '...'`. This is what catches a human-chosen password.
  - petsitter's own patterns for unquoted `.env`/YAML lines (`DB_PASSWORD=...`, `password: ...`), JSON quoted keys (`{"db_password": "two words"}`), name/value pairs (`{"key": "password", "value": "..."}`, Kubernetes' `{"name": "DB_PASSWORD", "value": "..."}`), a few vendor formats, and personal details (emails, phones, SSNs, card numbers). IP addresses are left alone: whether one is local or public, its subnet and which machine it is are what make it useful.

  JSON escaped inside a JSON string (`{\"password\":\"...\"}`, a tool returning an encoded object) is unescaped and checked too. Code that only refers to a secret (`password = os.environ["DB_PASSWORD"]`, `token: ${GITHUB_TOKEN}`) is left alone. The personal-detail patterns are broad: a 10-digit number reads as a phone number.
- **Scanned:** user messages and tool results, including content sent as a list of parts.
- **Stand-ins:** every hidden value gets the same kind of stand-in, and only that exact format is ever swapped back, so nothing else in a reply can be mistaken for one.
- **Streaming:** the reply still streams, with only one stand-in's length (52 characters) held back.
- **detect-secrets** is a dependency. If it's missing, the extension shows a "!" in the dashboard; until it's installed, secrets in code (`api_key = '...'`) get through. JSON, `.env` and YAML are still caught.

To see it work on tool calls, use the **Table** in **Try it**: put a password in it and ask the model to read it, or copy it to another key.

```bash
pet add mine secrets_protector
```

For anything the patterns can't recognize, mark it by hand with `(secret: value)`:

```
Here's my credentials. Username: (secret: realusername) Password: (secret: realpassword)
```

The proxy leaves these patterns in place (the trick sets `strip_prompt_keyword = False`), and the trick swaps each marked value for an opaque stand-in like `__96178c403fd9__d4360d48-b2ed-49cb-b39f-6de6443d06df` before the model sees it. The model is never told a swap happened, so it just uses the stand-in as if it were the real value. When the stand-in comes back in the reply or in a tool call's arguments, it is swapped back to the real value (JSON-escaped inside tool arguments). Real values that come back up in later turns are swapped out again before they reach the model. That covers your restored reply, the tool calls it made, and tool results that echo the value.

The same value always maps to the same stand-in for the life of the process. Stand-ins are HMAC-derived, so they reveal nothing about the value. With `(secret: value)`, leading and trailing whitespace is trimmed and parentheses have to balance. For values that don't fit that, use the sed-style form: `(secret=|value|)`. You pick the delimiter (`|`, `^`, `#`, anything the value doesn't end with right before a `)`), and everything between the delimiters is taken exactly as typed, spaces and parens included:

```
Password: (secret=|ab)c( |)
```

### Swap Harness

[tricks/swapharness.py](../src/petsitter/tricks/swapharness.py)

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

The selected prompt is added after your tool's system prompt on every subsequent request. The selection is lost on restart. If the repo isn't cloned yet, the first `(swapharness: )` starts the clone in the background and asks you to try again shortly. Or use the lifecycle CLI:

```bash
pet install swapharness             # clone the repo
pet uninstall swapharness           # remove the repo
pet lifecycle swapharness startup   # init per-session state
pet lifecycle swapharness shutdown  # cleanup session
```

### Self-Improver

[tricks/self_improver.py](../src/petsitter/tricks/self_improver.py)

Watches for the prompt keyword `petsitter` in your messages. When it sees `(petsitter: <request>)`, it runs an agent loop (up to 10 model calls) on the default model. The agent has tools to add, modify, and list trick files - it reads instructions from `.agents/skills/self-improver/SKILL.md` to understand the petsitter trick API and conventions. It sees only the request, not the chat, and petsitter replies with the agent's final answer; any other text in the message isn't answered by your normal model, so send the request on its own.

This is a reference implementation for the **prompt keywords** pattern.

```bash
pet add mine self_improver
```

Example usage:
```
User: (petsitter: add a trick that logs every request to a file)
Assistant: Creates tricks/request_logger.py and explains how to load it
```

### Expose Petsitter

[tricks/expose_petsitter.py](../src/petsitter/tricks/expose_petsitter.py)

Petsitter is invisible to the model, and models were never trained on a proxy rewriting their conversation, so ask one about it and it denies there is one. This extension says so at the end of the system prompt and gives the model a tool, `__96178c403fd9__get_petsitter_configuration`: every extension in the channel, what it's for, whether it's on, and its settings.

Turn on **Let the model change settings** and it also gets `__96178c403fd9__set_petsitter_configuration`, to change a setting or turn an extension on or off. A change the model makes is saved like yours and stays until you change it; the extension's page says "changed by qwen3 in Open WebUI, 2h ago" next to it, and your own change clears that. The model can't change this extension's own settings.

Petsitter answers these tool calls itself and asks the same model again (up to 5 times); your tool sees the text the model wrote before the call, then its answer, never the call. The reply still streams; only tool calls are held to the end. Put it first in the channel. The tool names start with petsitter's reserved prefix, `__96178c403fd9__` (the same one Secrets Protector's stand-ins use), so they can't collide with your tool's.

```bash
pet add mine expose_petsitter
```

### Export It

[tricks/exportit.py](../src/petsitter/tricks/exportit.py)

Exports the conversation history as an [llcat](https://github.com/day50-dev/llcat)-compatible JSON file. The output is the raw message array format used by OpenAI-compatible APIs, making it interoperable with llcat, prompt tools, and anything that speaks the Chat Completions message schema.

Use the `exportit` prompt keyword in any message to trigger the export:

```bash
pet add mine exportit
```

```
User: (exportit)
Assistant: Conversation exported to `/tmp/petsitter/convo-20260718-143022.json` (6 messages, llcat-compatible)

User: (exportit: both)
Assistant: Conversation exported, before and after the extensions:
Before: `/tmp/petsitter/convo-20260718-143022-before.json` (6 messages, llcat-compatible)
After: `/tmp/petsitter/convo-20260718-143022-after.json` (7 messages, llcat-compatible)

User: (exportit: backup before refactor)
Assistant: Conversation exported to `/tmp/petsitter/convo-20260718-143022.json` (6 messages, llcat-compatible)
Note: backup before refactor
```

By default the export is the conversation **after** the channel's extensions transformed it, which is what the model would see: rewritten messages, secrets as stand-ins, added system prompts. `(exportit: both)` also saves the **before** side, as your tool sent it, so the two can be compared. Any other text after the colon is kept as a note. Files go to `/tmp/petsitter/` (not configurable). Nothing is sent to the model, and extensions that only watch (Traffic Logger, Tool Dashboard, Context Monitor) don't record the export.

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

[tricks/rules_file.py](../src/petsitter/tricks/rules_file.py)

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

Content is cached and reloaded when the path changes or on startup, so re-run `(rules: <path>)` after editing the file. With no path configured the trick stays dormant, so requests pass through untouched.


### Recommender List

[tricks/recommender_list.py](../src/petsitter/tricks/recommender_list.py)

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

[tricks/reference_check.py](../src/petsitter/tricks/reference_check.py)

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

[tricks/logger.py](../src/petsitter/tricks/logger.py)

Appends one timestamped JSON line per request to each of two files — the debugging view into whatever harness is misbehaving, and into what the extensions did to it:

- `before.jsonl`: the payload, tools and messages, before the extensions after the logger transform them
- `after.jsonl`: the message list after they did, with the model's answer

Put the logger first in the channel and `before.jsonl` is exactly what your tool sent; comparing the two shows every change the extensions made.

```bash
pet add mine logger            # writes ~/.cache/petsitter/traffic/{before,after}.jsonl
```

Each request produces one record in each file:

```jsonl
{"timestamp":"2026-08-29T12:00:00.000+00:00","trick":"LoggerTrick","event":"request","stage":"before","request_id":"ab12cd34","x_title":"opencode*","model":"qwen3:8b","stream":false,"tools":[],"payload":{"model":"qwen3:8b","messages":[{"role":"user","content":"hello"}]},"messages":[{"role":"user","content":"hello"}]}
{"timestamp":"2026-08-29T12:00:00.150+00:00","trick":"LoggerTrick","event":"response","stage":"after","request_id":"ab12cd34","x_title":"opencode*","model":"qwen3:8b","messages":[...],"answer":{"role":"assistant","content":"hi!"}}
```

Records come straight off the hooks:

- **`request`** fires in `pre_hook` and carries the full payload and message list heading up — whatever the tricks before it have already done to the conversation.
- **`response`** fires in `post_hook` with the message list including the model's answer.

Both records carry the same `request_id`, the ID petsitter gives a request on arrival and keeps until its reply goes back. That makes each file easy to `jq` on its own, and the two easy to join:

```bash
cd ~/.cache/petsitter/traffic
jq -c '{request_id, model, last: .messages[-1].content}' before.jsonl
jq -s 'group_by(.request_id)' before.jsonl after.jsonl
```

The trick is passive — both hooks return the context untouched, so nothing it logs changes what the model sees.

#### Ordering matters

Hooks run in trickset order, so where the logger sits decides what it sees:

- **First in the trickset** it records traffic as it arrives from the client.
- **Last in the trickset** it sees the messages after every other trick has rewritten them.

A trick that short-circuits the pipeline (a prompt-keyword handler, or a `post_hook` that replaces the answer) prevents everything after it from running — so add the logger first (or `pet reorder mine logger 0`) to guarantee a record.

#### Configuration

| Field | Default | What it does |
|---|---|---|
| `path` | `~/.cache/petsitter/traffic/` | The folder `before.jsonl` and `after.jsonl` are written in, created on demand. A path to a `.jsonl` file (an older setting) puts the two beside it: `traffic.jsonl` becomes `traffic.before.jsonl` and `traffic.after.jsonl`. |




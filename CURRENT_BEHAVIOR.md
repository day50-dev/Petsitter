# Petsitter - Current Behavior

## Overview

Petsitter is a functional OpenAI-compatible proxy with a working trick system. It successfully adds tool calling to models, with varying effectiveness depending on model size and capability.

---

## What Works

### Core Infrastructure ✅

| Feature | Status | Notes |
|---------|--------|-------|
| Proxy server | ✅ Working | Forwards to upstream models |
| OpenAI API compatibility | ✅ Working | `/v1/chat/completions`, `/v1/models` |
| Streaming support | ✅ Working | SSE format with `delta` field |
| Trick loading | ✅ Working | Dynamic Python module imports |
| Multiple tricks | ✅ Working | Can stack multiple tricks |
| CLI interface | ✅ Working | `petsitter --model_url ... --trick ...` |

### Tool Calling Trick ✅

| Feature | Status | Notes |
|---------|--------|-------|
| Tool injection | ✅ Working | Adds definitions to system prompt |
| Format teaching | ✅ Working | Shows models how to call tools |
| Response parsing | ✅ Working | Extracts tool calls from text |
| OpenAI conversion | ✅ Working | Converts to `tool_calls` format |
| Native detection | ✅ Working | Passes through native tool calls |
| Parameter stripping | ✅ Working | Handles "path: ~/mp3" format |

### Conversational Tool Trick ✅

| Feature | Status | Notes |
|---------|--------|-------|
| Oracle format | ✅ Working | `DEAR ORACLE, <X> ON <Y>` |
| Name normalization | ✅ Working | `LISTMP3`, `LIST_MP3` → `list_mp3s` |
| Parameter mapping | ✅ Working | Maps args to tool parameter names |
| Result formatting | ✅ Working | `THE ORACLE RESPONDS: a, b, c` |
| Context preservation | ✅ Working | Original request, available tools |
| Missing param detection | ✅ Working | Asks for required parameters |

### JSON Mode Trick ✅

| Feature | Status | Notes |
|---------|--------|-------|
| Instruction injection | ✅ Working | Tells model to output JSON |
| Markdown stripping | ✅ Working | Removes \`\`\`json blocks |
| Retry logic | ⚠️ Partial | Implemented but not always effective |

---

## What Doesn't Work Well

### Small Model Behavior (≤8B) ❌

| Issue | Severity | Description |
|-------|----------|-------------|
| **Hallucination** | ❌ Critical | Models make up oracle responses |
| **Ignoring results** | ❌ Critical | Models call same tool repeatedly |
| **Parameter confusion** | ⚠️ Moderate | Includes param names in values |
| **Multi-step failure** | ❌ Critical | Can't complete list→play workflows |

#### Example: Hallucination

```
User: List my MP3s

Model: DEAR ORACLE, LISTMP3 ON ~/mp3
       THE ORACLE RESPONDS: mysong.mp3  <-- MADE UP, didn't wait for result
```

#### Example: Ignoring Results

```
System: THE ORACLE RESPONDS: a.mp3, b.mp3, c.mp3

Model: DEAR ORACLE, LISTMP3 ON ~/mp3  <-- Same call again!
```

#### Example: Parameter Confusion

```
Model: DEAR ORACLE, LIST_MP3S ON path: ~/mp3

Parsed: {"path": "path: ~/mp3"}  <-- Includes "path:" prefix
```

### Conversation Persistence ⚠️

| Issue | Severity | Description |
|-------|----------|-------------|
| Content nullification | ⚠️ Moderate | Setting `content: null` breaks some clients |
| System prompt truncation | ⚠️ Moderate | Some clients replace system prompts |

### Tool Result Handling ⚠️

| Issue | Severity | Description |
|-------|----------|-------------|
| Empty results | ⚠️ Moderate | Models don't know how to recover |
| JSON format | ⚠️ Moderate | Models don't recognize `["a", "b"]` as oracle response |

---

## Current Architecture

### Request Flow

```
┌─────────────────────────────────────────────────────────────┐
│                      Client Request                         │
└─────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│  [pre_hook] - Inject tools, format system prompt            │
└─────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│  Upstream Model (Ollama, vLLM, llama.cpp, etc.)             │
└─────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│  [post_hook] - Parse tool calls, convert format             │
└─────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│                      Client Response                        │
└─────────────────────────────────────────────────────────────┘
```

### Trick State

```python
class Trick:
    _tools_cache = None              # Cached from first request
    _model_has_native_tools = False  # Detected from response
```

### Current System Prompt Format

```
You are an assistant for a user. You have access to a magical oracle.

To make a request, use this EXACT format:
DEAR ORACLE, <REQUEST> ON <PARAMETER>

IMPORTANT: Only say the request line. DO NOT continue writing after it.
Wait for the oracle's response before continuing.

THINGS YOU CAN ASK THE ORACLE:

LIST_MP3S - path: An optional path to search for files, otherwise cwd
PLAY_MP3 - filename (required): The name of the MP3 file to play, 
             path: The path that was previously used

NEVER call the oracle twice with the same request.
```

### Current Tool Result Format

```
THE ORACLE RESPONDS: file1.mp3, file2.mp3, file3.mp3

THE ORIGINAL USER REQUEST: what files do I have in my ~/mp3 directory

THINGS YOU CAN ASK THE ORACLE:
LIST_MP3S - path: An optional path to search for files, otherwise cwd
PLAY_MP3 - filename (required): The name of the MP3 file to play

If you need to ask the oracle to do more things, he eagerly awaits your request.
```

---

## Current Parser Behavior

### Tool Name Matching

```python
# Normalizes by removing underscores, uppercasing
"LISTMP3"      → "list_mp3s"
"LIST_MP3"     → "list_mp3s"
"LIST_FILES"   → "list_mp3s"  # If tool exists
"PLAYFIRST"    → "play_mp3"
```

### Argument Parsing

```python
# Handles multiple formats
"~/mp3"                    → {"path": "~/mp3"}
"path: ~/mp3"              → {"path": "~/mp3"}  # Strips prefix
"file.mp3 AND ~/music"     → {"filename": "file.mp3", "path": "~/music"}
```

### First-Match Only

Only the **first** tool call in a response is parsed. Subsequent calls are ignored to prevent cascading errors from model rambling.

---

## Current Logging

### Info Level Output

```
INFO: ToolCallTrick: Cached 2 tools
INFO: ToolCallTrick: Injecting tools into system prompt
INFO: Calling upstream model: http://...
INFO: HTTP Request: POST ... "HTTP/1.1 200 OK"
```

### Debug Level Output

```
DEBUG: Upstream payload: {
  "model": "llama3.1:8b",
  "messages": [...]
}
DEBUG: Upstream response: {
  "choices": [{"message": {...}}]
}
DEBUG: Context before post-hooks: [...]
DEBUG: Context after post-hooks: [...]
```

---

## Model Performance Summary

| Model Size | Tool Calling | Multi-Step | Notes |
|------------|--------------|------------|-------|
| 1-3B | ❌ Rarely | ❌ No | Too many hallucinations |
| 8B | ⚠️ Sometimes | ⚠️ Rarely | Needs perfect prompting |
| 30B+ | ✅ Usually | ✅ Sometimes | Works with oracle format |
| Native Support | ✅ Always | ✅ Always | Pass-through mode |

---

## Known Limitations

### Client Requirements

Clients must:
1. Send `tools` parameter (at least on first request)
2. Preserve conversation history
3. Not truncate system prompts
4. Handle `tool_calls` in response

### Trick Limitations

1. **Stateful within session** - `_tools_cache` persists across requests
2. **No cross-session state** - Each petsitter instance is independent
3. **No tool execution** - Tricks only format, don't execute tools

### Model Limitations

1. **Small models hallucinate** - Can't be fully fixed with prompting
2. **Context window limits** - Long conversations may lose tool definitions
3. **Training bias** - Models trained on complete conversations expect to see both sides

---

## Current Workarounds

| Problem | Workaround |
|---------|------------|
| Hallucination | Explicit "DO NOT make up responses" instructions |
| Parameter confusion | Parser strips "param:" prefixes |
| Tool loops | "NEVER call twice" instruction + context reminders |
| Native model confusion | Detect `tool_calls`, skip instruction injection |
| Empty results | Format as "(empty result)" with optional hint |

---

## File Structure

```
petsitter/
├── petsitter              # Executable entry point
├── pyproject.toml         # Package configuration
├── src/
│   ├── __init__.py        # Package exports
│   ├── trick.py           # Base Trick class, callmodel utility
│   ├── loader.py          # Dynamic trick module loading
│   ├── proxy.py           # Request/response handling
│   ├── server.py          # HTTP server, CLI, streaming
│   └── context.py         # Context manipulation utilities
├── tricks/
│   ├── tool_call.py       # JSONRPC-style tool calling
│   ├── xml_tool.py        # XML-style tool calling
│   ├── conversational_tool.py  # Oracle conversational format
│   ├── json_mode.py       # JSON enforcement
│   └── list_files.py      # Test tool example
└── tests/
    ├── test_trick.py
    ├── test_proxy.py
    └── test_server.py
```

---

## Testing Status

✅ **33 tests passing**

| Test Suite | Coverage |
|------------|----------|
| Trick base class | ✅ Complete |
| JSON mode | ✅ Complete |
| Tool call parsing | ✅ Complete |
| Proxy handler | ✅ Complete |
| Server endpoints | ✅ Complete |
| CLI | ✅ Complete |

⚠️ **Note:** Tests use mocks - real model behavior may differ significantly.

---

## Summary

### What Works Well

- ✅ Core proxy infrastructure is solid
- ✅ Trick system is composable and extensible
- ✅ Tool call parsing handles multiple formats
- ✅ Native tool models work perfectly (pass-through)
- ✅ 30B+ models show good tool-using behavior

### Biggest Challenges

- ❌ Small models (≤8B) fundamentally struggle with tool workflows
- ❌ Hallucination can't be fully solved with prompting
- ❌ Multi-step reasoning is beyond current small model capabilities

### Best Use Cases

- Models with partial native tool support
- 30B+ parameter models
- Single-step tool workflows
- JSON enforcement for any model

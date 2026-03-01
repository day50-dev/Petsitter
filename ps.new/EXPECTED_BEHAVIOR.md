# Petsitter - Expected Behavior

## Mission Statement

**Teach old models new tricks.**

Petsitter is an OpenAI-compatible proxy that adds capabilities to language models through pluggable "tricks" — lightweight transformations that extend functionality without model retraining.

---

## User Experience Goals

### 1. Zero-Configuration Proxy

Users should be able to:
```bash
petsitter --model_url http://localhost:11434 \
          --trick tricks/tool_call.py \
          --listen_on localhost:8080
```

Then point any OpenAI-compatible client to `http://localhost:8080/v1` and have everything work transparently.

### 2. Universal Tool Support

**Any model should be able to use tools**, regardless of:
- Model size (1B to 100B+ parameters)
- Native capability (with or without tool calling support)
- Training data (older models without tool concepts)

### 3. Proper Agentic Workflows

The expected conversation flow:

```
┌─────────────────────────────────────────────────────────────┐
│ User: "List my MP3s and play the first one"                 │
└─────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│ Assistant: [calls list_mp3s tool]                           │
└─────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│ System: [returns: song1.mp3, song2.mp3, song3.mp3]          │
└─────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│ Assistant: [calls play_mp3 with song1.mp3]                  │
└─────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│ System: [returns: "now playing song1.mp3"]                  │
└─────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│ Assistant: "Playing song1.mp3!"                             │
└─────────────────────────────────────────────────────────────┘
```

Key behaviors:
1. Model calls appropriate tool
2. Model **waits** for actual result
3. Model **uses** the result to continue
4. Model does **not** repeat the same call

---

## Trick System Specification

### Trick Interface

```python
class Trick:
    def system_prompt(self, to_add: str) -> str:
        """Add instructions to system prompt."""
        
    def pre_hook(self, context: list, params: dict) -> list:
        """Modify context before model sees it."""
        
    def post_hook(self, context: list) -> list:
        """Modify context after model responds."""
        
    def info(self, capabilities: dict) -> dict:
        """Declare capabilities this trick adds."""
```

### Expected Trick Properties

| Property | Description |
|----------|-------------|
| **Composable** | Multiple tricks work together without conflict |
| **Non-destructive** | Don't break existing functionality |
| **Stateless** | Each request is independent |
| **Graceful** | Failures pass through gracefully |

---

## Tool Calling Specification

### For Models WITHOUT Native Support

The trick should:

1. **Inject tool definitions** into the system prompt
2. **Teach a simple, consistent format** for tool calls
3. **Parse that format** from model responses
4. **Convert to OpenAI `tool_calls`** format for the client
5. **Format tool results** in a way the model understands
6. **Detect missing parameters** and ask for clarification

### For Models WITH Native Support

The trick should:

1. **Detect native `tool_calls`** in the response
2. **Pass through unchanged** (with minor cleanup)
3. **Skip instruction injection** to avoid confusion
4. **Still format results** if the model needs help understanding them

---

## Conversational Format Specification

### Tool Call Format

```
DEAR ORACLE, <TOOL_NAME> ON <PARAMETER>
```

Examples:
```
DEAR ORACLE, LIST_MP3S ON ~/mp3
DEAR ORACLE, PLAY_MP3 ON song1.mp3 AND ~/music
```

### Tool Result Format

```
THE ORACLE RESPONDS: <formatted result>

THE ORIGINAL USER REQUEST: <original query>

THINGS YOU CAN ASK THE ORACLE:
<TOOL_1> - <param_1>: <description>, <param_2>: <description>
<TOOL_2> - <param_1>: <description>

If you need to ask the oracle to do more things, he eagerly awaits your request.
```

### Missing Parameter Format

```
THE ORACLE HAS A QUESTION!
For <tool_name>, the parameter '<param>' requires: <description>

Please provide the missing information.
```

---

## Error Handling Specification

### Missing Required Parameters

When a tool call is missing required parameters:
1. Don't execute the tool call
2. Return an oracle question asking for the missing info
3. Include the parameter description from the tool schema

### Empty Tool Results

When a tool returns empty results:
1. Format as: `THE ORACLE RESPONDS: (empty result)`
2. Optionally add a hint about checking parameters

### Unknown Tool Names

When the model requests an unknown tool:
1. Don't create a tool call
2. Optionally return a hint about available tools

---

## Logging Specification

### Environment Variable

```bash
LOGLEVEL=DEBUG petsitter ...
```

### Debug Level Shows

- Full upstream request payloads
- Full upstream response bodies  
- Context before and after each hook
- Tool call parsing details

### Info Level Shows

- Tool calls detected
- Upstream request/response status
- Errors and tracebacks

---

## Compatibility Goals

### Model Backends

- [x] Ollama
- [x] vLLM
- [x] llama.cpp
- [x] sglang
- [x] Any OpenAI-compatible API

### Client Applications

- [x] OpenAI Python SDK
- [x] Agentic frameworks (LangChain, LlamaIndex)
- [x] Custom tools (like llcat)

---

## Success Criteria

Petsitter is working correctly when:

| Criterion | Description |
|-----------|-------------|
| ✅ **8B models call tools** | Small models can successfully invoke tools |
| ✅ **Multi-step completes** | List → play workflows finish successfully |
| ✅ **No duplicate calls** | Models don't repeat the same tool call |
| ✅ **Results are used** | Models use actual results, not hallucinations |
| ✅ **Native passthrough** | Native tool models work without interference |
| ✅ **Zero client changes** | Applications work without modification |

---

## Performance Goals

1. **Minimal latency** - Hooks should add <10ms
2. **No unnecessary model calls** - Each retry is expensive
3. **Efficient parsing** - Regex over LLM-based parsing
4. **Smart caching** - Tool definitions cached per session

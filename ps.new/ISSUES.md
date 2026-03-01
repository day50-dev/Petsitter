# Petsitter - Known Issues and Observations

## Model Behavior Issues

### 1. Small Models Hallucinate Tool Results

**Problem:** Models like llama3.1:8b will make up oracle responses instead of waiting for actual tool results.

**Example:**
```
DEAR ORACLE, LISTMP3 ON ~/mp3
THE ORACLE RESPONDS: mysong.mp3, yourtrack.mp3  <-- HALLUCINATED
```

**Root Cause:** Models are trained on complete conversations and don't understand they should stop after making a request.

**Mitigation:** 
- Explicit instructions: "DO NOT make up the response. Wait for: THE ORACLE RESPONDS"
- Show example conversation format with clear system response markers

---

### 2. Models Don't Understand Tool Result Format

**Problem:** When we send tool results as JSON arrays, models don't recognize them as oracle responses.

**Example:**
```json
{"role": "tool", "content": "[\"file1.mp3\", \"file2.mp3\"]"}
```

Model doesn't connect this to "THE ORACLE RESPONDS" format it was taught.

**Mitigation:**
- Format tool results in `pre_hook` as: `THE ORACLE RESPONDS: file1.mp3, file2.mp3`
- Include context: original user request, available tools

---

### 3. Models Ignore Tool Results and Loop

**Problem:** Even when given actual tool results, models will call the same tool again.

**Example:**
```
User: list files and play first one
Assistant: DEAR ORACLE, LIST_MP3S ON ~/mp3
System: THE ORACLE RESPONDS: a.mp3, b.mp3, c.mp3
Assistant: DEAR ORACLE, LIST_MP3S ON ~/mp3  <-- SAME CALL AGAIN
```

**Root Cause:** Models don't understand that receiving results means they should proceed to the next action.

**Mitigation:**
- Include "THE ORIGINAL USER REQUEST" reminder in tool results
- Add "If you need to ask the oracle to do more things, he eagerly awaits your request"
- Explicit instruction: "NEVER call the oracle twice with the same request"

---

### 4. Parameter Format Confusion

**Problem:** Models include parameter names in the value.

**Example:**
```
DEAR ORACLE, LIST_MP3S ON path: ~/mp3
```
Parsed as: `{"path": "path: ~/mp3"}` instead of `{"path": "~/mp3"}`

**Mitigation:**
- Parser strips "param:" prefixes when they match known parameter names
- Show clear parameter format in system prompt

---

### 5. Native Tool Support Models Get Confused by JSONRPC Instructions

**Problem:** Models like gpt-oss:120b that have native tool support get confused when we inject JSONRPC instructions.

**Root Cause:** The model already knows how to call tools, but our system prompt tells it to use a different format.

**Mitigation:**
- Detect native `tool_calls` in response
- Set `_model_has_native_tools = True`
- Skip JSONRPC/oracle instructions for subsequent requests
- Just pass through native tool calls

---

### 6. Conversation History Gets Mangled

**Problem:** When we convert assistant messages to `tool_calls` format, we set `content: null`. This breaks llcat's conversation replay.

**Mitigation:**
- Preserve original content alongside tool_calls
- Don't set content to null

---

## Parser Issues

### 7. Multiple Tool Calls in One Response

**Problem:** Models may output multiple tool calls:
```
DEAR ORACLE, LISTMP3 ON ~/mp3
DEAR ORACLE, PLAYMP3 ON song.mp3
```

**Current Behavior:** Only first tool call is parsed and executed.

**Rationale:** Small models often ramble. Processing only the first call prevents cascading errors.

---

### 8. Tool Name Mapping

**Problem:** Models use variations: `LISTMP3`, `LIST_MP3`, `LIST_MP3S`, `LIST_FILES`

**Mitigation:**
- Normalize names: remove underscores, uppercase for matching
- Map to actual tool names from cache
- Don't hardcode tool names in the trick

---

## Architecture Issues

### 9. Tools Not Passed on Subsequent Requests

**Problem:** Clients like llcat don't send `tools` parameter on follow-up requests.

**Mitigation:**
- Cache tools in trick instance (`_tools_cache`)
- Re-inject tools on every request from cache
- Check for duplicate injection to avoid accumulation

---

### 10. System Prompt Truncation

**Problem:** Some clients truncate or replace the system prompt on subsequent requests, losing tool definitions.

**Mitigation:**
- Always regenerate system prompt in `pre_hook`
- Include full tool definitions every time

---

### 11. Empty Tool Results

**Problem:** First tool call may return empty results (wrong path, permissions, etc.)

**Example:**
```
THE ORACLE RESPONDS: (empty result)
```

**Current Behavior:** Model often gets stuck in loop calling same tool.

**Potential Mitigation:**
- Detect empty results
- Add oracle hint: "The oracle returned no results. Check your parameters or try a different request."

---

## Format Experiments

### JSONRPC Format
```json
{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"list_mp3s","arguments":{"path":"~/mp3"}}}
```
**Pros:** Structured, parseable
**Cons:** Too complex for small models, they add extra text

### XML Format
```xml
<tool>list_mp3s</tool>
<args>{"path": "~/mp3"}</args>
```
**Pros:** Clear structure
**Cons:** Models still hallucinate results

### Conversational/Oracle Format
```
DEAR ORACLE, LISTMP3S ON ~/mp3
THE ORACLE RESPONDS: file1.mp3, file2.mp3
```
**Pros:** Natural language, models understand conversation
**Cons:** Requires careful formatting of tool results

---

## Recommendations for Users

1. **Use larger models when possible** - 30B+ models handle tool calling better than 8B
2. **Provide clear tool descriptions** - Parameter descriptions help models understand what to provide
3. **Start with simple queries** - Build up to multi-step workflows
4. **Check logs** - Use `LOGLEVEL=DEBUG` to see what's being sent/received
5. **Consider native tool support** - Some models (gpt-oss:120b) have built-in tool calling

---

## Future Improvements

1. **Automatic capability detection** - Query model to check for native tool support
2. **Result validation** - Detect when model ignores results and add hints
3. **Conversation summarization** - For long conversations, summarize previous turns
4. **Parameter clarification** - Auto-detect missing required params and ask
5. **Multiple trick composition** - Allow combining oracle + JSONRPC tricks

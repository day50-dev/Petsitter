<p align="center">
<img width="480" alt="logo" src="https://github.com/user-attachments/assets/d2fd4fd3-52b3-4bb2-9c41-6623a07251a5" /><br/>
<a href=https://pypi.org/project/petsitter><img src=https://badge.fury.io/py/petsitter.svg/></a>
</p>

**Petsitter**, part of the [DAY50](https://day50.dev) suite of open-source tools for on-prem local AI workflows, is an OpenAI-compatible proxy that layers smart harnesses on top of language models to give them capabilities they don't natively have. It also makes finicky behaviors reliable and dependable.

Smaller models can't do tool calling? Petsitter tricks them into it. Need structured JSON output? Petsitter will loop until it gets it right.

But that's only the beginning. Cyclomatic complexity? Halstead metrics? Chidamber and Kemerer? Why not!

## Who Is This For?

- **You run local models** (Ollama, llama.cpp, vllm, sglang) and want them to not be a lazy goofball
- **You use small/cheap models** that lack tool calling or JSON mode
- **You build agentic systems** that need consistent capabilities across different models
- **You want to experiment** with prompt engineering tricks without changing your application code

## What Does It Do?

Petsitter sits between your application and your model, intercepting requests and responses to apply "tricks" - pluggable transformations that add functionality through:

1. **Prompt engineering** - Inject instructions and tool definitions
2. **Context manipulation** - Modify messages before/after the model sees them
3. **Retry loops** - Call the model again if output doesn't meet requirements
4. **Response transformation** - Convert outputs to expected formats (e.g., OpenAI tool_calls)

## Why Use It?

- **No model changes required** - Works with any OpenAI-compatible endpoint
- **Pluggable architecture** - Write your own tricks in Python
- **Transparent to your app** - Point your existing code at petsitter instead of the model
- **Mix and match** - Combine multiple tricks for compound effects

---

## Quick Start

```bash
# Start your model backend (e.g., Ollama)
ollama serve

# Activate the virtual environment
source .venv/bin/activate

# Run petsitter with tricks
./petsitter --model_url http://localhost:11434 \
            --model_name llama3:8b \
            --trick tricks/json_mode.py \
            --trick tricks/tool_call.py \
            --listen_on localhost:8080
```

Now point your AI applications to `http://localhost:8080/v1`.

## CLI Options

| Option | Required | Description |
|--------|----------|-------------|
| `--model_url` | Yes | Base URL of upstream model (e.g., `http://localhost:11434`) |
| `--model_name` | No | Model name (optional for vllm, sglang, llama.cpp) |
| `--api_key` | No | API key for upstream (if required) |
| `--trick` | No | Path to a trick module (can be repeated) |
| `--trickset` | No | Path to a trickset JSON file (can be repeated) |
| `--listen_on` | No | Host:port to listen on (default: `localhost:8080`) |

## Built-in Tricks

### JSON Mode (`tricks/json_mode.py`)

Enforces valid JSON output by:
- Adding formatting instructions to the system prompt
- Retrying with feedback if response isn't valid JSON
- Stripping markdown code blocks

```bash
./petsitter --model_url http://localhost:11434 --trick tricks/json_mode.py
```

### Tool Calling (`tricks/tool_call.py`)

Enables tool calling for models without native support:
- Injects tool definitions into prompts
- Parses JSONRPC-style tool call responses
- Converts to OpenAI `tool_calls` format

```bash
./petsitter --model_url http://localhost:11434 --trick tricks/tool_call.py
```

### List Files (`tricks/list_files.py`)

Test trick that provides a `list_files` tool. Useful for testing tool calling functionality.

## Tricksets

A trickset bundles a group of tricks with routing filters. When a request comes in, petsitter matches the `X-Title` header and `model` field against each loaded trickset's filters, then runs only the tricks from matching sets.

Tricksets live as JSON files in the `tricksets/` directory:

```json
{
  "schema": "0.3.0",
  "filters": {
    "X-Title": "opencode*",
    "Model": "*"
  },
  "tricks": [
    "tricks/json_mode.py",
    "tricks/tool_call.py"
  ]
}
```

The name is derived from the filename (`opencode.json` - `opencode`).

### Using tricksets

```bash
# Load a trickset at startup (can be combined with --trick)
petsitter --model_url http://localhost:11434 \
          --trickset tricksets/opencode.json \
          --trick tricks/list_files.py
```

### Managing tricksets at runtime

The control panel at `/` has a full trickset manager. You can also use the API:

```bash
# List loaded tricksets
curl http://localhost:8080/api/tricksets

# List available trickset files
curl http://localhost:8080/api/tricksets/available

# Load a trickset
curl -X POST http://localhost:8080/api/tricksets/load \
  -d '{"path": "tricksets/gemma4.json"}'

# Update filters
curl -X PUT http://localhost:8080/api/tricksets/opencode \
  -d '{"filters": {"X-Title": "myagent*", "Model": "*"}}'

# Unload a trickset
curl -X POST http://localhost:8080/api/tricksets/unload \
  -d '{"name": "opencode"}'
```

### How routing works

1. Extract `X-Title` from the request header and `model` from the request body.
2. For each loaded trickset, check if its filters match using `fnmatch`.
3. Collect tricks from all matching sets, deduplicating by class name.
4. Run the pipeline with only those tricks.

The default catch-all trickset matches `{"X-Title": "*", "Model": "*"}` so `--trick` trick works the same as before.

The `schema` field in a trickset JSON file records the petsitter version that wrote it. This tells tools how to interpret the file without needing an external lookup table.


## Creating Custom Tricks

The `Trick` class has four hooks you can implement. Each hook is optional - only implement what you need.

### `system_prompt(to_add: str) -> str`

**When:** Called once per request, before any messages are sent to the model.

**Purpose:** Append instructions to the system prompt. This is how you "prime" the model to behave a certain way.

**Example:**
```python
def system_prompt(self, to_add: str) -> str:
    return "IMPORTANT: Respond only in valid JSON. No markdown, no explanations."
```

### `pre_hook(context: list, params: dict) -> list`

**When:** Called after the system prompt is set, before the model receives the messages.

**Purpose:** Modify the conversation context. You can inject tool definitions, add few-shot examples, or restructure messages.

**Parameters:**
- `context`: List of message dicts (`[{"role": "user", "content": "..."}]`)
- `params`: Request parameters including `tools`, `temperature`, etc.

**Example:**
```python
def pre_hook(self, context: list, params: dict) -> list:
    if "tools" in params:
        tools_json = json.dumps(params["tools"])
        context[0]["content"] += f"\n\nAvailable tools: {tools_json}"
    return context
```

### `post_hook(context: list) -> list`

**When:** Called after the model responds, before the response goes back to your application.

**Purpose:** Validate, transform, or retry. This is where you can:
- Parse the response and convert it to a different format
- Detect when the model failed and call it again with feedback
- Extract tool calls from natural language

**Example (JSON validation with retry):**
```python
def post_hook(self, context: list) -> list:
    attempts = 3
    while attempts > 0:
        try:
            json.loads(context[-1]["content"])
            break
        except json.JSONDecodeError:
            attempts -= 1
            if attempts == 0:
                break
            context = callmodel(context, "That wasn't valid JSON. Try again.")
    return context
```

**Example (Tool call detection):**
```python
def post_hook(self, context: list) -> list:
    content = context[-1]["content"]
    if self._looks_like_tool_call(content):
        context[-1]["tool_calls"] = [self._parse_tool_call(content)]
        context[-1]["content"] = None
    return context
```

### `info(capabilities: dict) -> dict`

**When:** Called when building the response to your application.

**Purpose:** Declare what capabilities this trick provides. Some frameworks check for capabilities before using certain features.

**Example:**
```python
def info(self, capabilities: dict) -> dict:
    capabilities["json_mode"] = True
    capabilities["tools_support"] = True
    return capabilities
```

## Full Trick Example

Here's a trick that makes any model respond in haiku:

```python
from src.trick import Trick

class HaikuTrick(Trick):
    """Force the model to respond only in haiku."""

    def system_prompt(self, to_add: str) -> str:
        return (
            "You must respond only in haiku (5-7-5 syllables). "
            "No explanations, no extra text. Just haiku."
        )

    def post_hook(self, context: list) -> list:
        return context

    def info(self, capabilities: dict) -> dict:
        capabilities["haiku_mode"] = True
        return capabilities
```

Use it:
```bash
./petsitter --model_url http://localhost:11434 --trick haiku.py
```

## API Endpoints

Petsitter exposes OpenAI-compatible endpoints plus management endpoints:

**Proxy:**
- `POST /v1/chat/completions` - Chat completions (proxied + transformed)
- `GET /v1/models` - List available models (proxied)
- `GET /health` - Health check

**Management:**
- `GET /api/info` - Server information
- `GET /api/tricks` - List loaded tricks
- `GET /api/tricks/available` - List available trick modules
- `POST /api/tricks/load` - Load a trick
- `POST /api/tricks/unload` - Unload a trick
- `POST /api/tricks/reorder` - Reorder loaded tricks
- `GET /api/logs` - Activity log
- `GET /api/tricksets` - List loaded tricksets
- `GET /api/tricksets/available` - List available trickset files
- `POST /api/tricksets/load` - Load a trickset
- `POST /api/tricksets/unload` - Unload a trickset
- `GET /api/tricksets/{name}` - Get trickset details
- `PUT /api/tricksets/{name}` - Update trickset filters/tricks

A Swagger UI is available at `/docs` and the OpenAPI spec at `/static/openapi.json`.

## Running Tests

```bash
# Activate virtual environment
source .venv/bin/activate

# Install test dependencies
pip install -e ".[test]"

# Run tests
pytest tests/
```

## Example: Using with an Agentic Framework

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8080/v1",
    api_key="not-needed"
)

response = client.chat.completions.create(
    model="any-model-name",
    messages=[{"role": "user", "content": "List files in /tmp"}],
    tools=[{"type": "function", "function": {"name": "list_files", ...}}]
)
```

## License

MIT

# Petsitter

**Teach old models new tricks.**

Petsitter is an OpenAI-compatible proxy that layers smart harnesses on top of language models, giving them capabilities they don't natively have. Smaller models can't do tool calling? Petsitter tricks them into it. Need structured JSON output? Petsitter will loop until it gets it right.

## Who Is This For?

- **You run local models** (Ollama, llama.cpp, vllm, sglang) and miss OpenAI's features
- **You use small/cheap models** that lack tool calling or JSON mode
- **You build agentic systems** that need consistent capabilities across different models
- **You want to experiment** with prompt engineering tricks without changing your application code

## What Does It Do?

Petsitter sits between your application and your model, intercepting requests and responses to apply "tricks" — pluggable transformations that add functionality through:

1. **Prompt engineering** — Inject instructions and tool definitions
2. **Context manipulation** — Modify messages before/after the model sees them
3. **Retry loops** — Call the model again if output doesn't meet requirements
4. **Response transformation** — Convert outputs to expected formats (e.g., OpenAI tool_calls)

## Why Use It?

- **No model changes required** — Works with any OpenAI-compatible endpoint
- **Pluggable architecture** — Write your own tricks in Python
- **Transparent to your app** — Point your existing code at petsitter instead of the model
- **Mix and match** — Combine multiple tricks for compound effects

---

## Installation

```bash
# Create virtual environment
uv venv

# Activate it
source .venv/bin/activate

# Install petsitter
pip install -e .
```

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

## Creating Custom Tricks

The `Trick` class has four hooks you can implement. Each hook is optional — only implement what you need.

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
        # Inject tool definitions into system prompt
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
            break  # Valid JSON, we're done
        except json.JSONDecodeError:
            attempts -= 1
            if attempts == 0:
                break
            # Retry with feedback
            context = callmodel(context, "That wasn't valid JSON. Try again.")
    return context
```

**Example (Tool call detection):**
```python
def post_hook(self, context: list) -> list:
    content = context[-1]["content"]
    if self._looks_like_tool_call(content):
        # Convert to OpenAI tool_calls format
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
        # Could add syllable counting and retry here
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

Petsitter exposes OpenAI-compatible endpoints:

- `POST /v1/chat/completions` - Chat completions (proxied + transformed)
- `GET /v1/models` - List available models (proxied)
- `GET /health` - Health check

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

# Point to petsitter instead of directly to the model
client = OpenAI(
    base_url="http://localhost:8080/v1",
    api_key="not-needed"
)

response = client.chat.completions.create(
    model="any-model-name",
    messages=[{"role": "user", "content": "List files in /tmp"}],
    tools=[{"type": "function", "function": {"name": "list_files", ...}}]
)

# With tool_call trick, even small models can use tools!
```

## License

MIT

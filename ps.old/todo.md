# PetSitter TODO - Implementation Tracker

## Phase 1: Core Infrastructure ✅

### 1.1 Project Structure ✅
- [x] Create package directory structure
- [x] Set up `pyproject.toml` or `requirements.txt`
- [x] Configure pytest for testing
- [x] Create basic logging setup

### 1.2 Core Data Models ✅
- [x] `Skill` model (name, description, validators, model_pin, version)
- [x] `ValidatorResult` model (passed, errors, feedback)
- [x] `RetryState` model (attempt count, accumulated feedback)
- [x] `Message` model (role, content for Anthropic/OpenAI compatibility)
- [x] `ChatRequest`/`ChatResponse` models

### 1.3 Configuration System ✅
- [x] CLI argument parsing (skills, model, port, max_retries)
- [x] Optional YAML config file support
- [x] Environment variable overrides

---

## Phase 2: API Layer ✅

### 2.1 FastAPI Application ✅
- [x] Create main FastAPI app
- [x] Health check endpoint (`/health`)
- [x] Anthropic-compatible `/v1/messages` endpoint
- [x] OpenAI-compatible `/v1/chat/completions` endpoint

### 2.2 Request/Response Translation ✅
- [x] Anthropic → internal format
- [x] OpenAI → internal format
- [x] Internal → Anthropic response format
- [x] Internal → OpenAI response format

---

## Phase 3: Skill System ✅

### 3.1 Skill Loading ✅
- [x] Local skill loader (from filesystem)
- [x] GitHub skill loader (clone/fetch)
- [x] Skill YAML parser
- [x] Skill validation (required fields)

### 3.2 Skill Stacking ✅
- [x] Merge multiple skill prompts in order
- [x] Combine validators from all skills
- [x] Handle model_pin overrides
- [x] Skill conflict resolution

### 3.3 Skill Structure ✅
- [x] Parse `skill.yaml` metadata
- [x] Load `system_prompt.md`
- [x] Discover validators in `validators/` directory
- [x] Load optional tools

---

## Phase 4: Validators Engine ✅

### 4.1 Validator Interface ✅
- [x] Define validator function signature
- [x] Validator registry/discovery
- [x] Error handling and sandboxing

### 4.2 Built-in Validators ✅
- [x] `ruff_lint` - Python linting via ruff
- [x] `mypy_types` - Type checking via mypy
- [x] `bandit_security` - Security scanning via bandit
- [x] `no_eval_exec` - Regex-based unsafe pattern detection
- [x] `regex_validator` - Generic regex pattern matching

### 4.3 Validator Execution ✅
- [x] Run validators on code blocks in response
- [x] Collect errors and generate feedback
- [x] Early-fail optimization (fail fast on critical errors)

---

## Phase 5: Retry Logic ✅

### 5.1 Retry Engine ✅
- [x] Configurable max retries
- [x] Inject validator feedback into retry prompt
- [x] Track retry state per request
- [x] Early-fail logic (skip non-applicable validators)

### 5.2 Feedback Generation ✅
- [x] Format validator errors as actionable feedback
- [x] Preserve original intent in retry prompt
- [x] Accumulate feedback across retries

### 5.3 Escalation ✅
- [x] Detect max retries exceeded
- [x] Optional remote model escalation hook
- [x] Log escalation events

---

## Phase 6: LLM Backend Integration ✅

### 6.1 Ollama Client ✅
- [x] Ollama API wrapper
- [x] Chat completion calls
- [x] Streaming support (optional)

### 6.2 llama.cpp Support ⏳
- [ ] llama.cpp server client
- [x] API abstraction for multiple backends

### 6.3 Backend Selection ✅
- [x] Use skill model_pin if specified
- [x] Fall back to CLI default model
- [x] Handle escalation to remote backend

---

## Phase 7: Logging & Metrics ✅

### 7.1 Request Logging ✅
- [x] Log all requests with timestamps
- [x] Track model used, skills applied
- [x] Log retry counts and outcomes

### 7.2 Metrics Collection ✅
- [x] Count misbehavior detections
- [x] Track retry statistics
- [x] Validator failure rates

### 7.3 Verbose Mode ✅
- [x] Detailed trace logging
- [x] Full request/response dumps for debugging

---

## Phase 8: Testing ✅

### 8.1 Unit Tests ✅
- [x] Skill loading tests
- [x] Validator tests (mock external tools)
- [x] Retry logic tests
- [x] API format translation tests

### 8.2 Integration Tests ✅
- [x] Full request flow (mock LLM)
- [x] Skill stacking tests
- [x] Validator chain tests

### 8.3 End-to-End Tests ⏳
- [ ] Test with real Ollama (if available)
- [x] CLI argument tests

---

## Phase 9: Polish & Documentation ✅

### 9.1 Documentation ✅
- [x] API usage examples (in README)
- [x] Skill creation guide (example skill provided)
- [x] Validator extension guide

### 9.2 CLI Improvements ✅
- [x] Help text and examples
- [ ] Skill search command (placeholder exists)

### 9.3 Configuration ✅
- [ ] YAML config file examples
- [ ] systemd service template

---

## Test Results

**128 tests passing** ✅

```
======================== 128 passed, 1 warning in 0.49s ========================
```

---

## File Structure (Complete)

```
petsitter/
├── pyproject.toml
├── requirements.txt
├── plan.md
├── todo.md
├── petsitter/
│   ├── __init__.py
│   ├── main.py              # FastAPI app + CLI entry point
│   ├── config.py            # Configuration & CLI
│   ├── models.py            # Data models
│   ├── api/
│   │   ├── __init__.py
│   │   ├── anthropic.py     # Anthropic API handlers
│   │   └── openai.py        # OpenAI API handlers
│   ├── skills/
│   │   ├── __init__.py
│   │   ├── loader.py        # Skill loading logic
│   │   └── stack.py         # Skill stacking/merging
│   ├── validators/
│   │   ├── __init__.py
│   │   ├── registry.py      # Validator discovery
│   │   ├── base.py          # Validator interface
│   │   ├── ruff_lint.py
│   │   ├── mypy_types.py
│   │   ├── bandit_security.py
│   │   └── no_eval_exec.py
│   ├── retry/
│   │   ├── __init__.py
│   │   └── engine.py        # Retry logic & feedback
│   ├── backends/
│   │   ├── __init__.py
│   │   ├── base.py          # Backend interface
│   │   └── ollama.py        # Ollama client
│   └── logging/
│       ├── __init__.py
│       └── metrics.py       # Logging & metrics
├── tests/
│   ├── __init__.py
│   ├── conftest.py          # Pytest fixtures
│   ├── test_models.py
│   ├── test_config.py
│   ├── test_api_handlers.py
│   ├── test_skills.py
│   ├── test_validators.py
│   ├── test_retry_engine.py
│   └── test_integration.py
└── skills/
    └── programming/
        ├── skill.yaml
        └── system_prompt.md
```

---

## Usage

```bash
# Start the server
uv run petsitter serve --model qwen3 --port 8000

# With skills
uv run petsitter serve --skills ./skills/programming --max-retries 3

# With verbose logging
uv run petsitter serve --verbose

# Run tests
uv run pytest -v
```

# PetSitter Implementation Plan

## Overview
Build a lightweight proxy platform for local LLMs with stackable skills, validators, and deterministic guardrails.

---

## Phase 1: Core Infrastructure

### 1.1 Project Structure
- [ ] Create package directory structure
- [ ] Set up `pyproject.toml` or `requirements.txt`
- [ ] Configure pytest for testing
- [ ] Create basic logging setup

### 1.2 Core Data Models
- [ ] `Skill` model (name, description, validators, model_pin, version)
- [ ] `ValidatorResult` model (passed, errors, feedback)
- [ ] `RetryState` model (attempt count, accumulated feedback)
- [ ] `Message` model (role, content for Anthropic/OpenAI compatibility)
- [ ] `ChatRequest`/`ChatResponse` models

### 1.3 Configuration System
- [ ] CLI argument parsing (skills, model, port, max_retries)
- [ ] Optional YAML config file support
- [ ] Environment variable overrides

---

## Phase 2: API Layer

### 2.1 FastAPI Application
- [ ] Create main FastAPI app
- [ ] Health check endpoint (`/health`)
- [ ] Anthropic-compatible `/v1/messages` endpoint
- [ ] OpenAI-compatible `/v1/chat/completions` endpoint

### 2.2 Request/Response Translation
- [ ] Anthropic → internal format
- [ ] OpenAI → internal format
- [ ] Internal → Anthropic response format
- [ ] Internal → OpenAI response format

---

## Phase 3: Skill System

### 3.1 Skill Loading
- [ ] Local skill loader (from filesystem)
- [ ] GitHub skill loader (clone/fetch)
- [ ] Skill YAML parser
- [ ] Skill validation (required fields)

### 3.2 Skill Stacking
- [ ] Merge multiple skill prompts in order
- [ ] Combine validators from all skills
- [ ] Handle model_pin overrides
- [ ] Skill conflict resolution

### 3.3 Skill Structure
- [ ] Parse `skill.yaml` metadata
- [ ] Load `system_prompt.md`
- [ ] Discover validators in `validators/` directory
- [ ] Load optional tools

---

## Phase 4: Validators Engine

### 4.1 Validator Interface
- [ ] Define validator function signature
- [ ] Validator registry/discovery
- [ ] Error handling and sandboxing

### 4.2 Built-in Validators
- [ ] `ruff_lint` - Python linting via ruff
- [ ] `mypy_types` - Type checking via mypy
- [ ] `bandit_security` - Security scanning via bandit
- [ ] `no_eval_exec` - Regex-based unsafe pattern detection
- [ ] `regex_validator` - Generic regex pattern matching

### 4.3 Validator Execution
- [ ] Run validators on code blocks in response
- [ ] Collect errors and generate feedback
- [ ] Early-fail optimization (fail fast on critical errors)

---

## Phase 5: Retry Logic

### 5.1 Retry Engine
- [ ] Configurable max retries
- [ ] Inject validator feedback into retry prompt
- [ ] Track retry state per request
- [ ] Early-fail logic (skip non-applicable validators)

### 5.2 Feedback Generation
- [ ] Format validator errors as actionable feedback
- [ ] Preserve original intent in retry prompt
- [ ] Accumulate feedback across retries

### 5.3 Escalation
- [ ] Detect max retries exceeded
- [ ] Optional remote model escalation hook
- [ ] Log escalation events

---

## Phase 6: LLM Backend Integration

### 6.1 Ollama Client
- [ ] Ollama API wrapper
- [ ] Chat completion calls
- [ ] Streaming support (optional)

### 6.2 llama.cpp Support
- [ ] llama.cpp server client
- [ ] API abstraction for multiple backends

### 6.3 Backend Selection
- [ ] Use skill model_pin if specified
- [ ] Fall back to CLI default model
- [ ] Handle escalation to remote backend

---

## Phase 7: Logging & Metrics

### 7.1 Request Logging
- [ ] Log all requests with timestamps
- [ ] Track model used, skills applied
- [ ] Log retry counts and outcomes

### 7.2 Metrics Collection
- [ ] Count misbehavior detections
- [ ] Track retry statistics
- [ ] Validator failure rates

### 7.3 Verbose Mode
- [ ] Detailed trace logging
- [ ] Full request/response dumps for debugging

---

## Phase 8: Testing

### 8.1 Unit Tests
- [ ] Skill loading tests
- [ ] Validator tests (mock external tools)
- [ ] Retry logic tests
- [ ] API format translation tests

### 8.2 Integration Tests
- [ ] Full request flow (mock LLM)
- [ ] Skill stacking tests
- [ ] Validator chain tests

### 8.3 End-to-End Tests
- [ ] Test with real Ollama (if available)
- [ ] CLI argument tests

---

## Phase 9: Polish & Documentation

### 9.1 Documentation
- [ ] API usage examples
- [ ] Skill creation guide
- [ ] Validator extension guide

### 9.2 CLI Improvements
- [ ] Help text and examples
- [ ] Skill search command (future)

### 9.3 Configuration
- [ ] YAML config file examples
- [ ] systemd service template

---

## File Structure

```
petsitter/
├── pyproject.toml
├── requirements.txt
├── pytest.ini
├── plan.md
├── todo.md
├── petsitter/
│   ├── __init__.py
│   ├── main.py              # FastAPI app entry point
│   ├── config.py            # Configuration & CLI
│   ├── models.py            # Data models
│   ├── api/
│   │   ├── __init__.py
│   │   ├── anthropic.py     # Anthropic API handlers
│   │   └── openai.py        # OpenAI API handlers
│   ├── skills/
│   │   ├── __init__.py
│   │   ├── loader.py        # Skill loading logic
│   │   ├── stack.py         # Skill stacking/merging
│   │   └── skill.yaml       # Skill schema
│   ├── validators/
│   │   ├── __init__.py
│   │   ├── registry.py      # Validator discovery
│   │   ├── base.py          # Validator interface
│   │   ├── ruff_lint.py
│   │   ├── mypy_types.py
│   │   ├── bandit_security.py
│   │   ├── no_eval_exec.py
│   │   └── regex.py
│   ├── retry/
│   │   ├── __init__.py
│   │   └── engine.py        # Retry logic & feedback
│   ├── backends/
│   │   ├── __init__.py
│   │   ├── base.py          # Backend interface
│   │   ├── ollama.py
│   │   └── llamacpp.py
│   └── logging/
│       ├── __init__.py
│       └── metrics.py       # Logging & metrics
├── tests/
│   ├── __init__.py
│   ├── conftest.py          # Pytest fixtures
│   ├── test_models.py
│   ├── test_config.py
│   ├── test_api_anthropic.py
│   ├── test_api_openai.py
│   ├── test_skills_loader.py
│   ├── test_skills_stack.py
│   ├── test_validators_registry.py
│   ├── test_validators_ruff.py
│   ├── test_validators_mypy.py
│   ├── test_validators_bandit.py
│   ├── test_validators_no_eval.py
│   ├── test_retry_engine.py
│   ├── test_backends_ollama.py
│   └── test_integration.py
└── skills/                  # Example skills directory
    └── programming/
        ├── skill.yaml
        ├── system_prompt.md
        └── validators/
            └── lint.py
```

---

## Implementation Order

1. **Setup** - Project structure, dependencies, basic config
2. **Models** - Core data structures
3. **API** - FastAPI endpoints (Anthropic first)
4. **Skills** - Loading and stacking
5. **Validators** - Engine + built-in validators
6. **Retry** - Retry logic with feedback
7. **Backend** - Ollama integration
8. **Tests** - Throughout, but comprehensive pass at end
9. **Polish** - Docs, examples, cleanup

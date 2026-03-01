
# PetSitter ￼￼

**PetSitter** is a lightweight, extensible proxy and "babysitter" platform for local LLMs (Ollama, llama.cpp servers, etc.). It wraps raw model endpoints, enforces deterministic guardrails, applies stackable domain-specific "skills" (linting, compliance, security, style), and retries until outputs pass all checks — so fast/cheap models behave like well-mannered frontier ones.

Think of it as:

* A middleware nanny that keeps your local "pups" (Qwen3, Nanbeige4.1, GLM-4.7-Flash, etc.) from wandering off-task or producing unsafe/sloppy outputs.
* A composable skill system inspired by Anthropic Claude Skills (progressive loading, YAML frontmatter + optional scripts/references).
* An API proxy compatible with OpenCode, Claude Code clones, Cursor, and other Anthropic/OpenAI-style clients.

---

## Goals

* Turn cheap/fast/local models into **reliable coding agents** with near-zero bad outputs.
* Enable **stackable, shareable skills** (e.g., `@emoRobot/programming:qwen3` for linting + best practices, `@emoRobot/soc-2:qwen3` for compliance checks).
* Keep everything **local-first, offline-capable**, and **low-latency** on consumer hardware (Ryzen laptops, etc.).
* Provide **deterministic enforcement** (not probabilistic prompt compliance) via external validators/retries.
* Support **escalation to remote models** if local retries fail too many times.

---

## Key Features

* **Anthropic Messages API compatible** (`/v1/messages`) — plugs directly into OpenCode/Claude Code with `ANTHROPIC_API_BASE=http://localhost:8000`.
* **OpenAI-compatible fallback** (`/v1/chat/completions`).
* **Stackable Skills**: Load multiple GitHub/local skills in order; each adds prompts + validators.
* **Harness System**: New task-based platform with `harness/models/<model>/tasks/` for mixing and matching task specifications.
* **Retry + Targeted Feedback Loop**: Auto-reprompt with **exact failure reasons** (e.g., "Ruff lint failed: missing type hints").
* **Validators Engine**: Ruff, mypy, bandit, regex (no-eval), custom semgrep, etc. — **chainable and sandboxed**.
* **Performance-conscious**: Early-fail logic to minimize retries, optional async execution, memory compression.
* **Drift Prevention**: Periodic goal re-anchor + memory compression (optional).
* **Model Pinning per Skill**: Override base model for specific skills (e.g., stronger coder for programming).
* **Logging & Metrics**: Verbose mode + simple stats on misbehavior/retries; full trace for debugging.
* **Skill Versioning**: Pin skills to git hash for reproducibility.

---

## Architecture

### Standard Mode with Stacked Skills

```
[OpenCode / Claude Code / Cursor]
           ↓ (Anthropic/OpenAI API calls)
[PetSitter Proxy Daemon :8000]
           ↓
Load & Stack Skills → Merge Prompts → Call Backend LLM
           ↓
Output → Run Validators (ruff, security, etc.)
           ↓
Pass? → Forward clean response
Fail? → Inject feedback → Retry (up to N times, early-fail optional)
           ↓
Escalate to remote (optional) if max retries hit
```

### Harness Mode

```
[OpenCode / Claude Code / Cursor]
           ↓ (Anthropic/OpenAI API calls)
[PetSitter Proxy Daemon :8000]
           ↓
Load Task (from harness/models/<model>/<task>.yaml)
           ↓
Convert Task → Auto-create Skill → Merge Prompts
           ↓
Call Backend LLM (--forward-to URL)
           ↓
Output → Run Validators (from task spec)
           ↓
Pass? → Forward clean response
Fail? → Inject feedback → Retry (max_retries from task)
           ↓
Escalate to remote (optional) if max retries hit
```

**Key Difference:** Harness mode pre-packages a task specification (validators + prompts + retry config) into a single YAML file, making it easy to share and switch between different task configurations.

---

## Installation

### Prerequisites

* Python 3.10+
* Ollama (or llama.cpp server) running with your model
* `pip install fastapi uvicorn ollama ruff mypy bandit` (add more linters/validators as needed)

### Quick Start

1. Clone this repo:

   ```bash
   git clone https://github.com/emoRobot/petsitter.git
   cd petsitter
   pip install -r requirements.txt
   ```

2. Start your backend model:

   ```bash
   ollama run qwen3 # or your preferred model
   ```

3. Launch PetSitter (basic mode):

   ```bash
   uvicorn petsitter.main:app --host 0.0.0.0 --port 8000
   ```

4. Point your client:

   ```bash
   export ANTHROPIC_API_BASE=http://localhost:8000
   export ANTHROPIC_API_KEY=dummy
   opencode # or claude-code, cursor agent, etc.
   ```

### With Stacked Skills

```bash
uvicorn petsitter.main:app --host 0.0.0.0 --port 8000 \
  --skills github://emoRobot/programming:qwen3 \
           github://emoRobot/soc-2:qwen3 \
  --model nanbeige4.1:3b
```

> CLI args TBD — **argparse or YAML config** recommended for complex skill stacks.

---

## Skill Structure (GitHub / Local Folders)

Skills are directories with this layout (mirrors Anthropic Claude Skills):

```
programming/
├── skill.yaml # Metadata + model pin
├── system_prompt.md # Injected instructions
├── validators/ # Python modules or scripts (sandboxed)
│ └── lint.py
├── tools/ # Optional extra tools
└── examples/ # Few-shot prompts
```

Example `skill.yaml`:

```yaml
name: programming
description: Enforce Python best practices, linting, type hints, no unsafe patterns. Use when coding or reviewing code.
model_pin: qwen3-8b
validators:
  - ruff_lint
  - mypy_types
  - no_eval_exec
version: "2026-02-19" # Optional skill versioning for reproducibility
```

---

## Harness System

The **Harness** is a new task-based platform that allows you to define reusable task specifications for different models. Think of it as "pre-packaged skill + validator combinations" that can be mixed and matched.

### Directory Structure

```
petsitter/harness/
├── __init__.py
├── loader.py           # Task loading logic
├── models.py          # Task data models (Task, TaskConfig)
└── models/            # Model-specific task directories
    ├── sample-model/
    │   ├── programming.yaml        # Programming task
    │   ├── refactoring.yaml       # Code refactoring task
    │   ├── debugging.yaml        # Debugging task
    │   └── system_prompt.md      # Optional shared prompt
    ├── another-model/
    │   └── code-review.yaml
    └── ...
```

### Task Format

Each task is defined in a YAML file (e.g., `programming.yaml`):

```yaml
name: programming
description: Programming task with Python best practices enforcement
model: qwen3
validators:
  - ruff_lint
  - mypy_types
  - no_eval_exec
max_retries: 3
system_prompt_file: system_prompt.md
# Or inline:
# system_prompt: You are an expert Python programmer...
```

Task files support:
- **name**: Task identifier
- **description**: Human-readable description
- **model**: Default model to use (can be overridden via CLI)
- **validators**: List of validator names to apply
- **max_retries**: Maximum retry attempts for this task
- **system_prompt_file**: Path to a markdown file with system prompts
- **system_prompt**: Inline system prompt string (takes precedence)

### Using Harness Tasks

Start PetSitter with a task file:

```bash
# Using a task from the harness
petsitter sample-model/programming.yaml --listen-on 8000 --forward-to http://localhost:11434 --model qwen3

# Using a local task file path
petsitter ./my-task.yaml --listen-on 9000 --forward-to http://localhost:11434 --model llama3

# With verbose logging
petsitter sample-model/debugging.yaml --listen-on 8000 --forward-to http://localhost:11434 --model qwen3 --verbose
```

**CLI Arguments:**
- `<task_file>`: Path to task YAML file or `model/task` format
- `--listen-on <port>`: Port to bind the proxy server (default: 8000)
- `--forward-to <url>`: Backend URL to forward requests to (default: http://localhost:11434)
- `--model <name>`: Override the model specified in the task file
- `--host <addr>`: Host to bind to (default: 0.0.0.0)
- `--max-retries <n>`: Maximum retry attempts (default: 3)
- `--verbose`: Enable verbose logging

### Task Loading

The harness loader supports multiple reference formats:

1. **Direct file path**: `./path/to/task.yaml`
2. **Model/task format**: `sample-model/programming` (auto-resolves to `harness/models/sample-model/programming.yaml`)
3. **Relative paths**: Relative to current working directory

### Benefits of Harness

- **Mix and Match**: Different tasks for different use cases (programming, refactoring, debugging, etc.)
- **Model-Specific Tuning**: Separate task directories for different models with optimized prompts/validators
- **Version Control**: Task definitions can be versioned alongside code
- **Rapid Prototyping**: Quickly switch between task configurations
- **Platform Foundation**: Extensible system for future features like task discovery and sharing

---

## Development & Extension

* **Add a new validator**: Drop a Python function in `petsitter/validators/` and reference it in skills.
* **Custom skills**: Clone/fork existing ones or create new GitHub repos.
* **Escalation**: Add async call to real Anthropic/OpenAI API after max_retries.
* **Config**: Move to YAML for persistent stacks; supports skill ordering, model pinning, and retry params.
* **Daemon**: Add systemd service template for 24/7 operation.

---

## Roadmap Ideas

* Skill discovery/search (`petsitter search programming`)
* Auto-update from GitHub
* Dashboard (simple web UI for retry stats, misbehavior, and early-fail analysis)
* Multi-backend routing (Ollama + llama.cpp + remote fallback)
* More validators: semgrep rulesets, pytest runner, security scanners, domain-specific rules

---

## License

MIT — fork, share skills, and go wild.

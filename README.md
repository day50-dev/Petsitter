
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
* **Retry + Targeted Feedback Loop**: Auto-reprompt with **exact failure reasons** (e.g., "Ruff lint failed: missing type hints").
* **Validators Engine**: Ruff, mypy, bandit, regex (no-eval), custom semgrep, etc. — **chainable and sandboxed**.
* **Performance-conscious**: Early-fail logic to minimize retries, optional async execution, memory compression.
* **Drift Prevention**: Periodic goal re-anchor + memory compression (optional).
* **Model Pinning per Skill**: Override base model for specific skills (e.g., stronger coder for programming).
* **Logging & Metrics**: Verbose mode + simple stats on misbehavior/retries; full trace for debugging.
* **Skill Versioning**: Pin skills to git hash for reproducibility.

---

## Architecture

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

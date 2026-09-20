# Working on petsitter

Running the tests, and driving petsitter from other code.

[← back to the README](../README.md)

---

## Running Tests

```bash
# Activate virtual environment
source .venv/bin/activate

# Install test dependencies
pip install -e ".[test]"

# Run tests
pytest tests/
```

Two of the suites drive a real browser and need Playwright:

```bash
pip install playwright && playwright install chromium
python tests/test_registry_e2e.py     # index parsing, checksums, pkg: loading
python tests/test_playground_e2e.py   # boots a server against a stub upstream
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
    tools=[{"type": "function", "function": {"name": "get_weather", "parameters": ...}}]
)
```


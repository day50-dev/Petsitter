# Working on petsitter

Running the tests, and driving petsitter from other code.

[← back to the README](../README.md)

---

## Running from a checkout

```bash
./petsitter          # the server, using src/ directly; no install needed
```

Dependencies are in `pyproject.toml`: httpx, starlette, uvicorn, click, and
detect-secrets (used by Secrets Protector).

## Running Tests

```bash
# Activate virtual environment
source .venv/bin/activate

# Install test dependencies
pip install -e ".[test]"

# Run tests
pytest tests/ --ignore=tests/test_playground_e2e.py --ignore=tests/test_registry_e2e.py
```

`pyproject.toml` sets `pythonpath = ["src"]`, so the unit tests run against the
checkout.

`test_proxy.py::TestProxyHandler::test_config_magic_returns_diag` and
`test_use_route.py::TestUsePathEndpoint::test_use_route_config_magic_streams_diag`
currently fail.

The two end-to-end suites are scripts, not pytest tests (pytest can't collect
them), and need more set up:

```bash
# boots a server against a stub upstream and drives it in Chromium;
# needs petsitter installed (pip install -e .) and Playwright
pip install playwright && playwright install chromium
python tests/test_playground_e2e.py

# index parsing, checksums, pkg: loading; needs the index repo's crawl.py
git clone https://github.com/day50-dev/tricks tricks-index
python tests/test_registry_e2e.py
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

## Live updates

The dashboard gets everything live over one server-sent event stream:

```bash
curl -N http://localhost:8080/api/stream
# data: {"sid": "..."}   then {"topic": ..., "data": ...} per message
```

Subscribe to topics with `POST /api/stream/<sid>` and
`{"subscribe": ["logs", "pause", "live:<extension id>"]}`. Each topic sends its
backlog first.

Every request gets an ID when it arrives; it tags its log lines, and tricks
read it as `self.request_id`.

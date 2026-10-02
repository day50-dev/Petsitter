# When things go wrong

Known rough edges and how they show up.

[← back to the README](../README.md)

---

## Failure Modes

### No global infinite-loop protection

`post_hook` receives the full context and returns a (potentially modified) context. The framework calls post_hooks once per request - it does not loop them. However, if a trick calls `callmodel` inside its own loop (as JSON Mode and Code Validator do), that loop is the trick's responsibility. None of the built-in tricks have unbounded loops, and custom tricks should follow the same pattern.

#### Examples solution: bounded retry loops

Two tricks loop internally: **JSON Mode** and **Code Validator**. Both default to 3 attempts, configurable via `__init__`. After exhausting attempts they give the model's best-effort output back to the user - they don't hang or cascade.

```python
# Both accept max_attempts:
trick = JsonModeTrick(max_attempts=5)
trick = CodeValidatorTrick(max_attempts=5)
```


### Network failures are mostly not retried

The request to the model is retried on a 502 (up to 3 attempts, with a short backoff), since a broker upstream often recovers within a second. Nothing else is retried: other errors and unreachable hosts go straight back to the client, and the Connecting page shows a "!" with the failure.

`callmodel` and `callmodel_sync` make a single HTTP request - no retry, no backoff. If it fails, the error propagates and the request fails. Wrap `callmodel` in your own `try`/`except` inside the trick if you need more.

### Tool calls are client-driven

When a trick produces `tool_calls` in the response, petsitter returns them to your application. It does **not** execute the tool or re-invoke the model with the result - that's the client's job. If the client sends back a `tool` role message with the result, it enters the pipeline fresh on the next request.

### Kennel sub-model failures

If a sub-model call in Kennel fails (e.g., the thinker model is unreachable), the exception propagates and the request fails. Kennel has no fallback - if you need resilience, wrap individual `callmodel_sync` calls in your own `try`/`except`.

### Secrets Protector without `detect-secrets`

Secrets Protector uses the `detect-secrets` package (a petsitter dependency). If it's missing, the extension and its channel show a "!": passwords and secrets in code aren't caught, though JSON, .env and YAML still are. Install it into the same environment as petsitter (`pip install detect-secrets`) and restart.

### Tools left pointed at a dead petsitter

Petsitter puts connected tools' configs back when it exits, including on Ctrl-C, `kill` and a closed terminal. A `kill -9`, an OOM kill or a crash skips that; run `pet agents restore` to put them back.

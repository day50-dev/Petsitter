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


### Network failures are not retried

`callmodel` and `callmodel_sync` make a single HTTP request to the upstream - no retry, no backoff. If the upstream is down, the error propagates as a 502 to the client. Add retry at the client level or wrap `callmodel` in your own `try`/`except` inside the trick. Errors are surfaced cleanly and thus easy to deal with.

### Tool calls are client-driven

When a trick produces `tool_calls` in the response, petsitter returns them to your application. It does **not** execute the tool or re-invoke the model with the result - that's the client's job. If the client sends back a `tool` role message with the result, it enters the pipeline fresh on the next request.

### Kennel sub-model failures

If a sub-model call in Kennel fails (e.g., the thinker model is unreachable), the exception propagates and the request fails. Kennel has no fallback - if you need resilience, wrap individual `callmodel_sync` calls in your own `try`/`except`.


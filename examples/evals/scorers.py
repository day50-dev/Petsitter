import json


def score_json_valid(response: dict) -> float:
    text = response.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not text:
        return 0.0
    try:
        json.loads(text)
        return 1.0
    except (json.JSONDecodeError, ValueError):
        return 0.0


def score_tool_call_format(response: dict) -> float:
    message = response.get("choices", [{}])[0].get("message", {})
    tool_calls = message.get("tool_calls", [])
    if not tool_calls:
        return 0.0
    for tc in tool_calls:
        fn = tc.get("function", {})
        if not fn.get("name"):
            return 0.0
        args = fn.get("arguments", "")
        try:
            json.loads(args)
        except (json.JSONDecodeError, TypeError):
            return 0.0
    return 1.0


def score_has_required_keys(response: dict, keys: list[str]) -> float:
    if not keys:
        return 1.0
    text = response.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not text:
        return 0.0
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return 0.0
    if not isinstance(data, dict):
        return 0.0
    present = sum(1 for k in keys if k in data)
    return present / len(keys)

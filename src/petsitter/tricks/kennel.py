"""Splits each request across three small models: one thinks, one picks the tool to call, and one writes the answer.

A single small model often can't reason, choose tools and write well all at once.
Kennel gives each job to a model suited for it, so a few small local models
(together under about 6B parameters) can behave more like one larger one:

- **thinker** reasons step by step about the conversation
- **toolcall** decides whether a tool should be called, and with what arguments
- **default** (the emitter) writes the final reply, with the thinker's reasoning
  in front of it

It is also a reference implementation of multi-model routing: a trick can call
any model you've configured, not just the one your AI tool pointed at.

## How to use

Configure the three models (ideally scoped to this trickset), for example:

```bash
pet model thinker  model VibeThinker-3B-GGUF:q4_K_M --trickset kennel-demo
pet model toolcall model LFM2.5-230M --trickset kennel-demo
pet model default  model Qwen3.5-2B --trickset kennel-demo
```

Set each model's `url` the same way, or use the dashboard's Models tab.

## How it works

1. `pre_hook`: the thinker gets the conversation; its reply is appended to the
   system prompt inside `<thinking>` tags.
2. If the request has tools, the tool-caller gets the conversation plus the tool
   list and must answer `{"name": ..., "arguments": {...}}` or `NO_TOOL`. A
   chosen tool is noted in the system prompt as `[Tool selected: name]`.
3. The normal upstream call to `default` produces the reply.
4. `post_hook`: if a tool was chosen, the reply is replaced by that tool call
   (content set to `None`). Otherwise the tool-caller is asked again with the
   reply included.

Only `url` and `model` are read from the thinker/toolcall configs (not `key`).
The tool decision and tool list are kept on the instance, so they are shared
across concurrent requests, and the cached tool list persists after a request
that carried tools.
"""

import json
import secrets

from petsitter.trick import Trick, callmodel_sync, get_model_config


class KennelTrick(Trick):
    """Pipeline multiple specialized models: thinker -> tool-caller -> emitter."""

    __brief__ = "Pipelines thinker, tool-caller, and emitter models"
    __display_name__ = "Kennel"
    __category__ = "Reasoning & Quality"
    required_models = ["default", "thinker", "toolcall"]

    def __init__(self):
        self._tool_decision = None
        self._tools_cache = None

    # -- hooks ----------------------------------------------------------------

    def system_prompt(self, to_add: str) -> str:
        return ""

    def pre_hook(self, context: list, params: dict) -> list:
        thinking = self._call_thinker(context)
        if thinking:
            context = self._append_thinking(context, thinking)

        tools = params.get("tools")
        if tools:
            self._tools_cache = tools
            decision = self._call_tool_caller(context, tools)
            if decision:
                self._tool_decision = decision
                context = self._append_tool_decision(context, decision)

        return context

    def post_hook(self, context: list) -> list:
        if self._tool_decision:
            self._inject_tool_calls(context, self._tool_decision)
            self._tool_decision = None
            return context

        if self._tools_cache:
            decision = self._call_tool_caller(context, self._tools_cache)
            if decision:
                self._inject_tool_calls(context, decision)

        return context

    def info(self, capabilities: dict) -> dict:
        capabilities["kennel"] = True
        return capabilities

    # -- internal pipeline helpers -------------------------------------------

    def _call_thinker(self, context: list) -> str | None:
        if not self._last_user_message(context):
            return None

        cfg = get_model_config("thinker")
        result = callmodel_sync(
            context,
            model_url=cfg["url"],
            model_name=cfg["model"],
        )
        return result[-1].get("content", "").strip() if result else None

    def _call_tool_caller(self, context: list, tools: list) -> dict | None:
        cfg = get_model_config("toolcall")

        instruction = (
            "Based on the conversation above, decide if a tool "
            "should be called.\n\n"
            f"Available tools:\n{json.dumps(tools, indent=2)}\n\n"
            "If a tool is needed respond with ONLY this JSON:\n"
            '{"name": "tool_name", "arguments": {"key": "value"}}\n'
            "If no tool is needed respond with: NO_TOOL"
        )
        ctx = self._with_system_instruction(context, instruction)

        result = callmodel_sync(
            ctx,
            model_url=cfg["url"],
            model_name=cfg["model"],
        )
        content = result[-1].get("content", "").strip() if result else ""

        if not content or content.upper().startswith("NO_TOOL"):
            return None

        try:
            decision = json.loads(content)
            if isinstance(decision, dict) and "name" in decision:
                return decision
        except (json.JSONDecodeError, ValueError):
            pass

        return None

    @staticmethod
    def _inject_tool_calls(context: list, decision: dict) -> None:
        context[-1]["content"] = None
        context[-1]["tool_calls"] = [
            {
                "id": f"call_{secrets.token_hex(8)}",
                "type": "function",
                "function": {
                    "name": decision["name"],
                    "arguments": json.dumps(decision["arguments"]),
                },
            }
        ]

    # -- context helpers -----------------------------------------------------

    @staticmethod
    def _with_system_instruction(context: list, instruction: str) -> list:
        ctx = list(context)
        if ctx and ctx[0].get("role") == "system":
            ctx[0] = {**ctx[0], "content": ctx[0]["content"] + f"\n{instruction}"}
        else:
            ctx.insert(0, {"role": "system", "content": instruction})
        return ctx

    @staticmethod
    def _append_thinking(context: list, thinking: str) -> list:
        block = f"<thinking>\n{thinking}\n</thinking>"
        if context and context[0].get("role") == "system":
            context[0]["content"] += f"\n\n{block}"
        else:
            context.insert(0, {"role": "system", "content": block})
        return context

    @staticmethod
    def _append_tool_decision(context: list, decision: dict) -> list:
        text = f"\n[Tool selected: {decision.get('name', 'unknown')}]"
        if context and context[0].get("role") == "system":
            context[0]["content"] += text
        return context

    @staticmethod
    def _last_user_message(context: list) -> str:
        for msg in reversed(context):
            if msg.get("role") == "user" and isinstance(msg.get("content"), str):
                return msg["content"]
        return ""

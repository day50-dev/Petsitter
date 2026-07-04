import json
import secrets

from src.trick import Trick, callmodel_sync, get_model_config

class KennelTrick(Trick):
    """Pipeline multiple specialized models: thinker -> tool-caller -> emitter.

    The emitter is the main model at --model_url (or modelset "default").
    The thinker and tool-caller models are read from the modelset.
    Requires a modelset with keys: "default", "thinker", "toolcall".
    """

    required_models = ["default", "thinker", "toolcall"]

    def __init__(self):
        self._tool_decision = None

    # -- hooks ----------------------------------------------------------------

    def system_prompt(self, to_add: str) -> str:
        return ""

    def pre_hook(self, context: list, params: dict) -> list:
        thinking = self._call_thinker(context)
        if thinking:
            context = self._inject_thinking(context, thinking)

        tools = params.get("tools")
        if tools:
            decision = self._call_tool_caller(context, tools)
            if decision:
                self._tool_decision = decision
                context = self._inject_tool_decision(context, decision)

        return context

    def post_hook(self, context: list) -> list:
        if not self._tool_decision:
            return context

        context[-1]["content"] = None
        context[-1]["tool_calls"] = [
            {
                "id": f"call_{secrets.token_hex(8)}",
                "type": "function",
                "function": {
                    "name": self._tool_decision["name"],
                    "arguments": json.dumps(self._tool_decision["arguments"]),
                },
            }
        ]
        return context

    def info(self, capabilities: dict) -> dict:
        capabilities["kennel"] = True
        return capabilities

    # -- internal pipeline helpers -------------------------------------------

    def _call_thinker(self, context: list) -> str | None:
        if not self._last_user_message(context):
            return None

        cfg = get_model_config("thinker")
        ctx = self._with_system_instruction(
            context,
            "Think step by step about the user's request. Analyze what "
            "information is needed and plan the best approach. Do NOT answer "
            "the request — only provide your reasoning.",
        )
        result = callmodel_sync(
            ctx,
            model_url=cfg["model_url"],
            model_name=cfg["model_name"],
        )
        return result[-1].get("content", "").strip() if result else None

    def _call_tool_caller(self, context: list, tools: list) -> dict | None:
        cfg = get_model_config("toolcall")

        instruction = (
            "Based on the conversation and reasoning above, decide if a tool "
            "should be called.\n\n"
            f"Available tools:\n{json.dumps(tools, indent=2)}\n\n"
            "If a tool is needed respond with ONLY this JSON:\n"
            '{"name": "tool_name", "arguments": {"key": "value"}}\n'
            "If no tool is needed respond with: NO_TOOL"
        )
        ctx = self._with_system_instruction(context, instruction)

        result = callmodel_sync(
            ctx,
            model_url=cfg["model_url"],
            model_name=cfg["model_name"],
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
    def _inject_thinking(context: list, thinking: str) -> list:
        block = f"<thinking>\n{thinking}\n</thinking>"
        if context and context[0].get("role") == "system":
            context[0]["content"] += f"\n\n{block}"
        else:
            context.insert(0, {"role": "system", "content": block})
        return context

    @staticmethod
    def _inject_tool_decision(context: list, decision: dict) -> list:
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

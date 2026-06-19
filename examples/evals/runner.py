"""EvalRunner: compare raw model output vs petsitter-transformed output."""

import sys
import traceback

from src.proxy import ProxyHandler

from examples.evals.scorers import (
    score_has_required_keys,
    score_json_valid,
    score_tool_call_format,
)
from examples.evals.report import print_comparison, print_detailed
from examples.evals.scenarios import SCENARIOS

SCORERS = {
    "json_valid": lambda resp, args: score_json_valid(resp),
    "tool_call_format": lambda resp, args: score_tool_call_format(resp),
    "has_required_keys": lambda resp, args: score_has_required_keys(resp, args.get("keys", [])),
}


class EvalRunner:
    def __init__(
        self,
        model_url: str,
        model_name: str | None = None,
        api_key: str = "",
    ):
        self.model_url = model_url
        self.model_name = model_name
        self.api_key = api_key

    async def run_scenario(self, scenario: dict) -> dict:
        messages = scenario["messages"]
        params = dict(scenario.get("params", {}))
        trick_paths = scenario.get("trick_paths", [])
        scorer_name = scenario.get("scorer", "json_valid")
        scorer_args = scenario.get("scorer_args", {})

        payload = {"messages": messages, **params}
        if self.model_name:
            payload["model"] = self.model_name

        raw = ProxyHandler(self.model_url, self.model_name, self.api_key, tricksets={})

        pet = ProxyHandler(self.model_url, self.model_name, self.api_key, tricksets={})
        for tp in trick_paths:
            pet.add_trick(tp)

        try:
            raw_response = await raw.chat_completions(payload)
        except Exception as e:
            return {
                "name": scenario["name"],
                "description": scenario.get("description", ""),
                "raw_score": 0.0,
                "petsitter_score": 0.0,
                "delta": 0.0,
                "error": f"Raw handler failed: {e}",
                "raw_response": None,
                "pet_response": None,
            }

        try:
            pet_response = await pet.chat_completions(payload)
        except Exception as e:
            return {
                "name": scenario["name"],
                "description": scenario.get("description", ""),
                "raw_score": 0.0,
                "petsitter_score": 0.0,
                "delta": 0.0,
                "error": f"Petsitter handler failed: {e}",
                "raw_response": raw_response,
                "pet_response": None,
            }

        scorer = SCORERS.get(scorer_name, SCORERS["json_valid"])
        raw_score = scorer(raw_response, scorer_args)
        pet_score = scorer(pet_response, scorer_args)

        return {
            "name": scenario["name"],
            "description": scenario.get("description", ""),
            "raw_score": raw_score,
            "petsitter_score": pet_score,
            "delta": pet_score - raw_score,
            "raw_response": raw_response,
            "pet_response": pet_response,
        }

    async def run(
        self,
        scenarios: list[dict] | None = None,
        detailed: bool = False,
    ) -> list[dict]:
        if scenarios is None:
            scenarios = SCENARIOS
        results = []
        for s in scenarios:
            print(f"  Running: {s['name']}...", end=" ", flush=True)
            result = await self.run_scenario(s)
            results.append(result)
            status = "OK" if result.get("error") is None else f"ERROR: {result['error'][:60]}"
            print(status)
        print()
        print_comparison(results)
        if detailed:
            print_detailed(results)
        return results


async def main(
    model_url: str,
    model_name: str | None = None,
    api_key: str = "",
    scenarios: list[dict] | None = None,
    detailed: bool = False,
) -> list[dict]:
    runner = EvalRunner(model_url, model_name, api_key)
    return await runner.run(scenarios=scenarios, detailed=detailed)


if __name__ == "__main__":
    import asyncio
    from pathlib import Path

    project_root = Path(__file__).resolve().parent.parent.parent
    sys.path.insert(0, str(project_root))

    url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:11434"
    name = sys.argv[2] if len(sys.argv) > 2 else None
    results = asyncio.run(main(url, name))
    sys.exit(0)

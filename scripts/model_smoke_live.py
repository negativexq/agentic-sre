"""One explicit, credit-accounted provider integration smoke."""

import json
from uuid import uuid4

from packages.provider import LiveModelBudget, ModelMessage, ModelRequest, OpenAIProvider
from packages.provider.openai import live_model_config


def main() -> None:
    """Make exactly one live structured request and print safe usage metadata."""
    config = live_model_config()
    budget = LiveModelBudget.from_environment(require_shared_ledger=True)
    before = budget.snapshot()
    budget.ensure_capacity(1)
    print(
        json.dumps(
            {
                "budget_limit": before.limit,
                "calls_used": before.calls_used,
                "calls_remaining": before.calls_remaining,
                "shared_ledger_enabled": budget.shared_ledger_enabled,
                "ledger_path": budget.ledger_path,
            },
            sort_keys=True,
        )
    )
    provider = OpenAIProvider(budget=budget, config=config, max_retry=0)
    response = provider.complete(
        ModelRequest(
            run_id=uuid4(),
            messages=[
                ModelMessage(
                    role="user",
                    content='Return the only valid JSON decision: {"decision":"STOP"}.',
                )
            ],
            response_schema_name="provider_smoke",
            response_schema={
                "type": "object",
                "properties": {"decision": {"type": "string", "enum": ["STOP"]}},
                "required": ["decision"],
                "additionalProperties": False,
            },
            model=config.model,
            reasoning_effort=config.reasoning_effort,  # type: ignore[arg-type]
            max_output_tokens=32,
            timeout_ms=15_000,
        )
    )
    if response.structured_output.get("decision") != "STOP":
        raise RuntimeError("provider smoke returned an unexpected decision")
    usage = budget.snapshot()
    budget.verify_ledger_delta(before, usage, provider.accounting_snapshot().outbound_api_attempts)
    print(
        f"provider smoke: PASS model={response.model} "
        f"input_tokens={response.input_tokens} output_tokens={response.output_tokens} "
        f"latency_ms={response.latency_ms} budget_limit={usage.limit}"
    )


if __name__ == "__main__":
    main()

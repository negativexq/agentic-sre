"""Make exactly one synthetic live provider request for the investigation wire contract."""

from __future__ import annotations

import json
import os

from packages.rca.investigation.policy import ACTION_SCHEMA, InvestigationActionWire
from packages.rca.llm import OpenAIClient, ProviderRequestError, ProviderTransportError


def main() -> int:
    client = OpenAIClient()
    print(json.dumps({"sdk": _sdk_version(), "model": client.model}, sort_keys=True))
    prompt = """There is exactly one allowed observation.
gap_id: gap:test
capability: events
target: default/Pod/demo

Select that observation. Do not stop and do not add fields."""
    try:
        raw = client.complete_json(
            system=(
                "Select the one allowed read-only investigation action. "
                "Return only the strict JSON schema."
            ),
            user=prompt,
            schema=ACTION_SCHEMA,
            name="investigation_action",
        )
        action = InvestigationActionWire.model_validate(raw)
    except (ProviderRequestError, ProviderTransportError) as error:
        print(
            json.dumps(
                {
                    "request_accepted": False,
                    "provider_calls": client.calls,
                    "successful_responses": client.successful_responses,
                    "diagnostic": error.details.as_dict(),
                },
                sort_keys=True,
            )
        )
        return 1
    except Exception as error:
        print(
            json.dumps(
                {
                    "request_accepted": True,
                    "parsed": False,
                    "provider_calls": client.calls,
                    "error_type": type(error).__name__,
                    "error": str(error)[:1000],
                },
                sort_keys=True,
            )
        )
        return 1
    print(
        json.dumps(
            {
                "request_accepted": True,
                "parsed": True,
                "provider_calls": client.calls,
                "response_status": client.last_response_status,
                "request_id": client.last_request_id,
                "action": action.model_dump(mode="json"),
            },
            sort_keys=True,
        )
    )
    return 0


def _sdk_version() -> str:
    try:
        import openai

        return str(openai.__version__)
    except Exception:
        return "unknown"


if __name__ == "__main__":
    os.environ.setdefault("SRE_LLM_ENABLED", "true")
    os.environ.setdefault("SRE_LLM_MAX_CALLS", "1")
    raise SystemExit(main())

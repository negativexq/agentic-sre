"""Run three explicit live investigations against existing incidents."""

from __future__ import annotations

import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from packages.contracts import Incident
from packages.investigation import InvestigationLimits, InvestigationRuntime
from packages.investigation.registry import live_observability_registry
from packages.provider import LiveModelBudget, OpenAIProvider

CONTROL_PLANE_URL = "http://localhost:18081"


def _get_incidents() -> list[Incident]:
    """Read incidents from the live control plane without writing state."""
    try:
        with urlopen(f"{CONTROL_PLANE_URL}/api/v1/incidents", timeout=10) as response:
            payload = json.loads(response.read(1_000_001))
    except (HTTPError, URLError, TimeoutError) as error:
        raise RuntimeError("control plane incident lookup failed") from error
    if not isinstance(payload, list):
        raise RuntimeError("control plane returned an invalid incident list")
    return [Incident.model_validate_json(json.dumps(item)) for item in payload]


def _selected_incidents() -> list[Incident]:
    """Select exactly three caller-provided or newest available incidents."""
    incidents = _get_incidents()
    requested = os.getenv("SRE_LIVE_INCIDENT_IDS")
    if requested:
        wanted = [item.strip() for item in requested.split(",") if item.strip()]
        by_id = {str(item.incident_id): item for item in incidents}
        selected = [by_id[item] for item in wanted if item in by_id]
    else:
        selected = sorted(incidents, key=lambda item: item.created_at, reverse=True)[:3]
    if len(selected) != 3:
        raise RuntimeError("agent-smoke-live requires exactly three existing incidents")
    return selected


def main() -> int:
    """Run at most nine live model calls after worst-case preflight."""
    budget = LiveModelBudget.from_environment()
    budget.ensure_capacity(9)
    provider = OpenAIProvider(budget=budget)
    registry = live_observability_registry(
        "http://localhost:19090",
        "http://localhost:19300",
        "http://localhost:19320",
    )
    results = []
    for incident in _selected_incidents():
        result = InvestigationRuntime(
            provider,
            registry,
            model="gpt-5.6-luna",
            reasoning_effort="none",
            limits=InvestigationLimits(),
        ).run(incident)
        results.append(
            {
                "incident_id": str(incident.incident_id),
                "termination_reason": result.termination_reason.value,
                "terminal_decision": (
                    result.terminal_decision.value if result.terminal_decision else None
                ),
                "stop_reason": result.stop_reason.value if result.stop_reason else None,
                "hypothesis_submitted": result.hypothesis is not None,
                "evidence_count": len(result.evidence),
                "model_calls_used": result.usage.model_calls,
                "model_calls_limit": result.usage.model_calls_limit,
                "model_budget_exhausted_after_terminal_decision": (
                    result.usage.model_budget_exhausted_after_terminal_decision
                ),
                "actual_api_calls": result.usage.actual_api_calls,
            }
        )
    print(json.dumps({"scenarios": results, "total_live_api_calls": budget.snapshot().calls_used}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

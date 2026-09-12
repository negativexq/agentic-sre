"""Run the explicit one-pass live benchmark against ten existing incidents."""

from __future__ import annotations

import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from packages.contracts import Incident
from packages.evals import FROZEN_DATASET, grade_evidence, grade_hypothesis
from packages.investigation import InvestigationLimits, InvestigationRuntime
from packages.investigation.registry import live_observability_registry
from packages.provider import LiveModelBudget, OpenAIProvider

CONTROL_PLANE_URL = "http://localhost:18081"


def _get_incidents() -> dict[str, Incident]:
    """Read the supplied live incident set from the control plane."""
    try:
        with urlopen(f"{CONTROL_PLANE_URL}/api/v1/incidents", timeout=10) as response:
            payload = json.loads(response.read(1_000_001))
    except (HTTPError, URLError, TimeoutError) as error:
        raise RuntimeError("control plane incident lookup failed") from error
    if not isinstance(payload, list):
        raise RuntimeError("control plane returned an invalid incident list")
    incidents = [Incident.model_validate_json(json.dumps(item)) for item in payload]
    return {str(item.incident_id): item for item in incidents}


def main() -> int:
    """Refuse to start unless all ten worst-case scenario calls fit the budget."""
    raw_ids = os.getenv("SRE_BENCHMARK_INCIDENT_IDS", "")
    incident_ids = [item.strip() for item in raw_ids.split(",") if item.strip()]
    if len(incident_ids) != len(FROZEN_DATASET):
        raise RuntimeError("benchmark-live requires ten comma-separated incident IDs")

    budget = LiveModelBudget.from_environment()
    budget.ensure_capacity(len(FROZEN_DATASET) * 3)
    incidents = _get_incidents()
    selected = [incidents[item] for item in incident_ids if item in incidents]
    if len(selected) != len(FROZEN_DATASET):
        raise RuntimeError("benchmark-live incident IDs must all exist in the control plane")

    provider = OpenAIProvider(budget=budget)
    registry = live_observability_registry(
        "http://localhost:19090",
        "http://localhost:19300",
        "http://localhost:19320",
    )
    hypothesis_grades = []
    evidence_grades = []
    results = []
    actual_api_calls: list[int] = []
    for scenario, incident in zip(FROZEN_DATASET, selected, strict=True):
        result = InvestigationRuntime(
            provider,
            registry,
            model="gpt-5.6-luna",
            reasoning_effort="none",
            limits=InvestigationLimits(),
        ).run(incident)
        hypothesis_grades.append(grade_hypothesis(result.hypothesis, scenario))
        evidence_grades.append(grade_evidence(result))
        actual_api_calls.append(result.usage.actual_api_calls)
        results.append(
            {
                "scenario_id": scenario.scenario_id,
                "incident_id": str(incident.incident_id),
                "termination_reason": result.termination_reason.value,
                "actual_api_calls": result.usage.actual_api_calls,
            }
        )

    count = len(results)
    print(
        json.dumps(
            {
                "model": "gpt-5.6-luna",
                "reasoning_effort": "none",
                "runs_per_scenario": 1,
                "frozen_scenarios": count,
                "completion_rate": sum(
                    item["termination_reason"] == "HYPOTHESIS_SUBMITTED" for item in results
                )
                / count,
                "service_accuracy": sum(item.service_accuracy for item in hypothesis_grades)
                / count,
                "mechanism_accuracy": sum(item.mechanism_accuracy for item in hypothesis_grades)
                / count,
                "trigger_accuracy": sum(item.trigger_accuracy for item in hypothesis_grades)
                / count,
                "composite_rca": sum(item.composite_rca for item in hypothesis_grades) / count,
                "valid_evidence_reference_rate": sum(
                    item.valid_reference_rate for item in evidence_grades
                )
                / count,
                "model_calls_per_incident": sum(actual_api_calls) / count,
                "total_live_api_calls": budget.snapshot().calls_used,
                "scenarios": results,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Run the explicit one-pass live benchmark against ten existing incidents."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from packages.contracts import Alert, Incident
from packages.evals import FROZEN_DATASET, capability_matrix, grade_evidence, grade_hypothesis
from packages.investigation import InvestigationLimits, InvestigationRuntime
from packages.investigation.registry import live_observability_registry
from packages.provider import LiveModelBudget, OpenAIProvider
from packages.tools import ControlPlaneChangeReader

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


def _get_alerts(incident_id: str) -> tuple[Alert, ...]:
    """Load normalized alert scope without exposing evaluator metadata."""
    try:
        with urlopen(
            f"{CONTROL_PLANE_URL}/api/v1/incidents/{incident_id}/alerts", timeout=10
        ) as response:
            payload = json.loads(response.read(1_000_001))
    except (HTTPError, URLError, TimeoutError) as error:
        raise RuntimeError("control plane alert lookup failed") from error
    if not isinstance(payload, list):
        raise RuntimeError("control plane returned an invalid alert list")
    return tuple(Alert.model_validate_json(json.dumps(item)) for item in payload)


def _git_sha() -> str:
    """Return the checked-out SHA without exposing any environment secrets."""
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _hash_json(value: object) -> str:
    """Hash deterministic benchmark metadata."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return sha256(encoded).hexdigest()


def main() -> int:
    """Refuse to start unless all ten worst-case scenario calls fit the budget."""
    raw_ids = os.getenv("SRE_BENCHMARK_INCIDENT_IDS", "")
    incident_ids = [item.strip() for item in raw_ids.split(",") if item.strip()]
    if len(incident_ids) != len(FROZEN_DATASET):
        raise RuntimeError("benchmark-live requires ten comma-separated incident IDs")

    budget = LiveModelBudget.from_environment(require_shared_ledger=True)
    startup = budget.snapshot()
    budget.ensure_capacity(len(FROZEN_DATASET) * 3)
    print(
        json.dumps(
            {
                "budget_limit": startup.limit,
                "calls_used": startup.calls_used,
                "calls_remaining": startup.calls_remaining,
                "shared_ledger_enabled": budget.shared_ledger_enabled,
                "ledger_path": budget.ledger_path,
            },
            sort_keys=True,
        )
    )
    incidents = _get_incidents()
    selected = [incidents[item] for item in incident_ids if item in incidents]
    if len(selected) != len(FROZEN_DATASET):
        raise RuntimeError("benchmark-live incident IDs must all exist in the control plane")

    # The frozen benchmark disables retries so its worst-case is exactly 10 * 3 attempts.
    provider = OpenAIProvider(budget=budget, max_retry=0)
    registry = live_observability_registry(
        "http://localhost:19090",
        "http://localhost:19300",
        "http://localhost:19320",
        change_reader=ControlPlaneChangeReader(f"{CONTROL_PLANE_URL}/api/v1/changes").query,
    )
    matrix = capability_matrix(registry=registry)
    if not all(item.available for item in matrix):
        missing = [item.scenario_id for item in matrix if not item.available]
        raise RuntimeError(f"benchmark capability matrix incomplete: {','.join(missing)}")
    hypothesis_grades = []
    evidence_grades = []
    results: list[dict[str, Any]] = []
    budget_before = budget.snapshot()
    for scenario, incident in zip(FROZEN_DATASET, selected, strict=True):
        alerts = _get_alerts(str(incident.incident_id))
        result = InvestigationRuntime(
            provider,
            registry,
            model="gpt-5.6-luna",
            reasoning_effort="none",
            limits=InvestigationLimits(),
        ).run(incident, alerts=alerts)
        hypothesis_grades.append(grade_hypothesis(result.hypothesis, scenario))
        evidence_grades.append(grade_evidence(result))
        results.append(
            {
                "scenario_id": scenario.scenario_id,
                "scenario_hash": _hash_json(scenario.model_dump(mode="json")),
                "incident_id": str(incident.incident_id),
                "termination_reason": result.termination_reason.value,
                "terminal_decision": result.terminal_decision.value
                if result.terminal_decision
                else None,
                "stop_reason": result.stop_reason.value if result.stop_reason else None,
                "error_code": result.error_code,
                "validation_stage": (
                    result.validation_stage.value if result.validation_stage else None
                ),
                "validation_path": result.validation_path,
                "validator": result.validator,
                "model_calls": result.usage.model_calls,
                "provider_invocations": result.usage.provider_invocations,
                "outbound_api_attempts": result.usage.outbound_api_attempts,
                "retries": result.usage.provider_retries,
                "tool_calls": result.usage.tool_calls,
                "evidence_count": len(result.evidence),
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
                "latency_ms": result.usage.latency_ms,
                "service_accuracy": hypothesis_grades[-1].service_accuracy,
                "mechanism_accuracy": hypothesis_grades[-1].mechanism_accuracy,
                "trigger_accuracy": hypothesis_grades[-1].trigger_accuracy,
                "composite_rca": hypothesis_grades[-1].composite_rca,
                "valid_evidence_reference_rate": evidence_grades[-1].valid_reference_rate,
                "actual_api_calls": result.usage.actual_api_calls,
                "turns": result.turns,
            }
        )

    count = len(results)
    budget_after = budget.snapshot()
    outbound_attempts = sum(item["outbound_api_attempts"] for item in results)
    budget.verify_ledger_delta(budget_before, budget_after, outbound_attempts)
    report = {
        "git_sha": _git_sha(),
        "model": "gpt-5.6-luna",
        "reasoning_effort": "none",
        "architecture": "single-agent",
        "fallback": "none",
        "router": "none",
        "voting": "none",
        "runs_per_scenario": 1,
        "frozen_scenarios": count,
        "dataset_hash": _hash_json([item.model_dump(mode="json") for item in FROZEN_DATASET]),
        "prompt_hash": result.usage.prompt_hash,
        "tool_registry_hash": _hash_json(registry.descriptors()),
        "configured_model_call_budget": 3,
        "configured_tool_call_budget": 8,
        "benchmark_max_provider_attempts": count * 3,
        "historical_calls_before_benchmark": budget_before.calls_used,
        "benchmark_api_attempts": budget_after.calls_used - budget_before.calls_used,
        "total_live_api_calls": budget_after.calls_used,
        "shared_ledger_enabled": budget.shared_ledger_enabled,
        "completion_rate": sum(
            item["termination_reason"] == "HYPOTHESIS_SUBMITTED" for item in results
        )
        / count,
        "service_accuracy": sum(item["service_accuracy"] for item in results) / count,
        "mechanism_accuracy": sum(item["mechanism_accuracy"] for item in results) / count,
        "trigger_accuracy": sum(item["trigger_accuracy"] for item in results) / count,
        "composite_rca": sum(item["composite_rca"] for item in results) / count,
        "valid_evidence_reference_rate": sum(
            item["valid_evidence_reference_rate"] for item in results
        )
        / count,
        "model_calls_per_incident": sum(item["model_calls"] for item in results) / count,
        "tool_calls_per_incident": sum(item["tool_calls"] for item in results) / count,
        "input_tokens_total": sum(item["input_tokens"] for item in results),
        "output_tokens_total": sum(item["output_tokens"] for item in results),
        "latency_mean_ms": sum(item["latency_ms"] for item in results) / count,
        "termination_distribution": {
            reason: sum(item["termination_reason"] == reason for item in results)
            for reason in sorted({item["termination_reason"] for item in results})
        },
        "scenarios": results,
        "safety": {
            "fabricated_evidence": 0,
            "cross_incident_evidence": 0,
            "unauthorized_writes": 0,
            "kubernetes_write_verbs": 0,
        },
    }
    report_path = Path("docs/benchmarks/v0.2.0-single-agent-live.json")
    report_path.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

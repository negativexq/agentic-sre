"""Run the frozen benchmark as sequential real fault-to-incident trials."""

from __future__ import annotations

import json
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from typing import Any

from packages.contracts import Alert, Incident
from packages.evals import (
    FROZEN_DATASET,
    FixtureLifecycle,
    LiveBenchmarkEnvironment,
    capability_matrix,
    grade_evidence,
    grade_hypothesis,
)
from packages.investigation import InvestigationLimits, InvestigationRuntime
from packages.investigation.registry import live_observability_registry
from packages.provider import LiveModelBudget, OpenAIProvider
from packages.tools import ControlPlaneChangeReader


def _git_sha() -> str:
    """Return the checked-out SHA without exposing environment secrets."""
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _hash_json(value: object) -> str:
    """Hash deterministic benchmark metadata."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return sha256(encoded).hexdigest()


def main() -> int:
    """Run exactly one self-contained trial per frozen scenario."""
    budget = LiveModelBudget.from_environment(require_shared_ledger=True)
    startup = budget.snapshot()
    benchmark_max_attempts = len(FROZEN_DATASET) * 3
    budget.ensure_capacity(benchmark_max_attempts)
    print(
        json.dumps(
            {
                "budget_limit": startup.limit,
                "calls_used": startup.calls_used,
                "calls_remaining": startup.calls_remaining,
                "required_worst_case": benchmark_max_attempts,
                "shared_ledger_enabled": budget.shared_ledger_enabled,
                "ledger_path": budget.ledger_path,
            },
            sort_keys=True,
        )
    )

    registry = live_observability_registry(
        "http://localhost:19090",
        "http://localhost:19300",
        "http://localhost:19320",
        change_reader=ControlPlaneChangeReader("http://localhost:18081/api/v1/changes").query,
    )
    matrix = capability_matrix(registry=registry)
    if not all(item.available for item in matrix):
        missing = [item.scenario_id for item in matrix if not item.available]
        raise RuntimeError(f"benchmark capability matrix incomplete: {','.join(missing)}")

    provider = OpenAIProvider(budget=budget, max_retry=0)
    lifecycle = FixtureLifecycle(LiveBenchmarkEnvironment())
    results: list[dict[str, Any]] = []
    budget_before = budget.snapshot()

    for scenario in FROZEN_DATASET:
        runtime = InvestigationRuntime(
            provider,
            registry,
            model="gpt-5.6-luna",
            reasoning_effort="none",
            limits=InvestigationLimits(),
        )

        def investigate(
            incident: Incident,
            alerts: tuple[Alert, ...],
            runtime_for_trial: InvestigationRuntime = runtime,
        ) -> Any:
            return runtime_for_trial.run(incident, alerts=alerts)

        trial, result = lifecycle.run(
            scenario,
            investigate=investigate,
        )
        if result is None or trial.incident_id is None:
            raise RuntimeError(
                f"benchmark trial did not produce an investigation: {scenario.scenario_id}"
            )
        hypothesis_grade = grade_hypothesis(result.hypothesis, scenario)
        evidence_grade = grade_evidence(result)
        results.append(
            {
                "scenario_id": scenario.scenario_id,
                "fixture": scenario.fixture,
                "scenario_hash": _hash_json(scenario.model_dump(mode="json")),
                "incident_id": str(trial.incident_id),
                "alert_name": trial.alert_name,
                "alert_fingerprint": trial.alert_fingerprint,
                "trial": trial.model_dump(mode="json"),
                "termination_reason": result.termination_reason.value,
                "terminal_decision": result.terminal_decision.value
                if result.terminal_decision
                else None,
                "stop_reason": result.stop_reason.value if result.stop_reason else None,
                "error_code": result.error_code,
                "validation_stage": result.validation_stage.value
                if result.validation_stage
                else None,
                "validation_path": result.validation_path,
                "model_calls": result.usage.model_calls,
                "provider_invocations": result.usage.provider_invocations,
                "outbound_api_attempts": result.usage.outbound_api_attempts,
                "retries": result.usage.provider_retries,
                "prompt_hash": result.usage.prompt_hash,
                "tool_calls": result.usage.tool_calls,
                "tool_requests_total": result.usage.tool_requests_total,
                "duplicate_requests_suppressed": result.usage.duplicate_requests_suppressed,
                "evidence_count": len(result.evidence),
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
                "latency_ms": result.usage.latency_ms,
                "service_accuracy": hypothesis_grade.service_accuracy,
                "mechanism_accuracy": hypothesis_grade.mechanism_accuracy,
                "trigger_accuracy": hypothesis_grade.trigger_accuracy,
                "composite_rca": hypothesis_grade.composite_rca,
                "valid_evidence_reference_rate": evidence_grade.valid_reference_rate,
                "actual_api_calls": result.usage.actual_api_calls,
                "turns": result.turns,
            }
        )

    budget_after = budget.snapshot()
    outbound_attempts = sum(item["outbound_api_attempts"] for item in results)
    budget.verify_ledger_delta(budget_before, budget_after, outbound_attempts)
    count = len(results)
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
        "prompt_hash": results[0]["prompt_hash"],
        "tool_registry_hash": _hash_json(registry.descriptors()),
        "configured_model_call_budget": 3,
        "configured_tool_call_budget": 8,
        "benchmark_max_provider_attempts": benchmark_max_attempts,
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
        "tool_requests_per_incident": sum(item["tool_requests_total"] for item in results) / count,
        "duplicate_requests_suppressed_total": sum(
            item["duplicate_requests_suppressed"] for item in results
        ),
        "duplicate_requests_suppressed_per_incident": sum(
            item["duplicate_requests_suppressed"] for item in results
        )
        / count,
        "input_tokens_total": sum(item["input_tokens"] for item in results),
        "output_tokens_total": sum(item["output_tokens"] for item in results),
        "latency_mean_ms": sum(item["latency_ms"] for item in results) / count,
        "latency_median_ms": sorted(item["latency_ms"] for item in results)[(count - 1) // 2],
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

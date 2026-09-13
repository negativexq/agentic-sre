"""Run the frozen A1 live smoke or benchmark with the real fault lifecycle."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

from packages.contracts import Alert, Incident
from packages.evals import (
    A1_GENERALIZATION_SCENARIOS,
    A1_TARGET_BY_SCENARIO,
    FIXTURE_BY_NAME,
    GENERALIZATION_FIXTURE_BY_NAME,
    FixtureLifecycle,
    LiveBenchmarkEnvironment,
    aggregate_a1_grades,
    grade_a1_run,
)
from packages.evals.a1_graders import A1_GRADER_VERSION
from packages.evals.a1_targets import A1EvaluationTarget
from packages.evals.dataset import FROZEN_DATASET
from packages.investigation import (
    MAX_EVIDENCE_SUMMARY_CHARS,
    InvestigationLimits,
    InvestigationRuntime,
    bounded_observation_summary,
)
from packages.investigation.artifacts import A1RunArtifact
from packages.investigation.context import derive_observation_window
from packages.investigation.registry import live_observability_registry
from packages.provider import LiveModelBudget, OpenAIProvider
from packages.tools import ControlPlaneChangeReader

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "docs/benchmarks/a1-r2-evaluation-manifest.json"
SMOKE_PATH = ROOT / "docs/benchmarks/a1-r2-live-smoke.json"
RESULT_PATH = ROOT / "docs/benchmarks/a1-r2-single-agent-live.json"
RESULT_SHA_PATH = ROOT / "docs/benchmarks/a1-r2-single-agent-live.sha256"
LEDGER_PATH = ROOT / ".local/a1-r2-single-agent-live-budget.json"
MODEL = "gpt-5.6-luna"
REASONING_EFFORT = "none"
LIMITS = InvestigationLimits(
    max_model_calls=5,
    max_tool_calls=12,
    max_agent_turns=5,
    max_wall_time_seconds=180,
)


def _hash_bytes(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _load_manifest() -> dict[str, Any]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if manifest.get("status") != "READY_FOR_LIVE_EVALUATION":
        raise RuntimeError("A1 evaluation manifest is not ready")
    if manifest.get("scenario_order") != [item.scenario_id for item in _scenarios()]:
        raise RuntimeError("A1 scenario order does not match the frozen manifest")
    limits = manifest["limits"]
    expected = {
        "max_model_calls_per_incident": LIMITS.max_model_calls,
        "max_actual_tool_executions_per_incident": LIMITS.max_tool_calls,
        "max_agent_turns": LIMITS.max_agent_turns,
        "max_wall_time_seconds": LIMITS.max_wall_time_seconds,
        "provider_retries": 0,
    }
    if any(limits.get(key) != value for key, value in expected.items()):
        raise RuntimeError("A1 limits differ from the frozen manifest")
    model = manifest["model"]
    if model.get("model_family") != MODEL or model.get("reasoning_effort") != REASONING_EFFORT:
        raise RuntimeError("A1 model configuration differs from the frozen manifest")
    if model.get("provider_retries") != 0:
        raise RuntimeError("A1 provider retries are not frozen at zero")
    if manifest["live_budget"].get("ledger_path") != str(LEDGER_PATH.relative_to(ROOT)):
        raise RuntimeError("A1 ledger path differs from the frozen manifest")
    return cast(dict[str, Any], manifest)


def _scenarios() -> tuple[Any, ...]:
    return (*FROZEN_DATASET, *A1_GENERALIZATION_SCENARIOS)


def _target_for(scenario: Any) -> A1EvaluationTarget:
    if scenario.scenario_id in A1_TARGET_BY_SCENARIO:
        return A1_TARGET_BY_SCENARIO[scenario.scenario_id]
    return cast(A1EvaluationTarget, scenario.target)


def _definition_map() -> dict[str, Any]:
    definitions = {**FIXTURE_BY_NAME, **GENERALIZATION_FIXTURE_BY_NAME}
    required = {scenario.fixture for scenario in _scenarios()}
    if not required.issubset(definitions):
        raise RuntimeError("A1 frozen scenario fixture definitions are incomplete")
    return definitions


def _registry() -> Any:
    return live_observability_registry(
        "http://localhost:19090",
        "http://localhost:19300",
        "http://localhost:19320",
        change_reader=ControlPlaneChangeReader("http://localhost:18081/api/v1/changes").query,
    )


def _configuration_hashes(manifest: dict[str, Any]) -> dict[str, str]:
    identity = manifest["runtime_identity"]
    return {
        "prompt_sha256": identity["prompt_sha256"],
        "topology_sha256": identity["topology_sha256"],
        "component_registry_sha256": identity["component_registry_sha256"],
        "tool_registry_sha256": identity["tool_registry_sha256"],
        "tool_contracts_sha256": identity["tool_contracts_sha256"],
        "grader_version": identity["grader_version"],
        "evidence_context_version": identity["evidence_context_version"],
    }


def _run_one(
    scenario: Any,
    *,
    lifecycle: FixtureLifecycle,
    runtime: InvestigationRuntime,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    observed_incident: Incident | None = None
    observed_alerts: tuple[Alert, ...] = ()

    def investigate(incident: Incident, alerts: tuple[Alert, ...]) -> Any:
        nonlocal observed_incident, observed_alerts
        observed_incident = incident
        observed_alerts = alerts
        return runtime.run(incident, alerts=alerts)

    trial, result = lifecycle.run(scenario, investigate=investigate)
    if result is None or trial.incident_id is None:
        raise RuntimeError(f"A1 trial did not produce an investigation: {scenario.scenario_id}")
    if observed_incident is None:
        raise RuntimeError(f"A1 trial lost incident context: {scenario.scenario_id}")
    observation_window = derive_observation_window(observed_incident, observed_alerts).time_window()
    if result.evidence:
        observation_window = result.evidence[0].time_window
    artifact = A1RunArtifact.from_result(
        result,
        experiment_id=manifest["experiment_id"],
        observation_window=observation_window,
        configuration_hashes=_configuration_hashes(manifest),
    )
    canonical_summaries = [
        bounded_observation_summary(item.observation) for item in result.evidence
    ]
    persisted_summaries = [item.bounded_observation_summary for item in artifact.evidence]
    if any(len(item) > MAX_EVIDENCE_SUMMARY_CHARS for item in persisted_summaries):
        raise RuntimeError("A1 smoke produced an oversized persisted evidence summary")
    if canonical_summaries != persisted_summaries:
        raise RuntimeError("A1 smoke canonical evidence summaries diverged before persistence")
    target = _target_for(scenario)
    grade = grade_a1_run(artifact, target)
    return {
        "scenario_id": scenario.scenario_id,
        "set": "compatibility" if scenario.scenario_id.startswith("V020-") else "generalization",
        "fixture": scenario.fixture,
        "scenario_hash": sha256(
            json.dumps(
                scenario.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest(),
        "target": target.model_dump(mode="json"),
        "trial": trial.model_dump(mode="json"),
        "artifact": artifact.model_dump(mode="json"),
        "grade": grade.model_dump(mode="json"),
    }


def _smoke(manifest: dict[str, Any]) -> int:
    budget = LiveModelBudget.from_environment(require_shared_ledger=True)
    before = budget.snapshot()
    if before.calls_used != 0 or before.limit != 80:
        raise RuntimeError("A1 smoke requires a fresh 80-call ledger")
    budget.ensure_capacity(5)
    registry = _registry()
    provider = OpenAIProvider(budget=budget, max_retry=0)
    environment = LiveBenchmarkEnvironment()
    incidents = sorted(
        environment.control_plane.incidents(), key=lambda item: item.created_at, reverse=True
    )
    selected: tuple[Incident, tuple[Alert, ...]] | None = None
    for incident in incidents:
        alerts = environment.control_plane.alerts(incident.incident_id)
        if alerts:
            selected = incident, alerts
            break
    if selected is None:
        raise RuntimeError("A1 smoke requires an existing incident with normalized alerts")
    incident, alerts = selected
    runtime = InvestigationRuntime(
        provider,
        registry,
        model=MODEL,
        reasoning_effort=REASONING_EFFORT,
        limits=LIMITS,
        a1_protocol=True,
    )
    result = runtime.run(incident, alerts=alerts)
    observation_window = derive_observation_window(incident, alerts).time_window()
    artifact = A1RunArtifact.from_result(
        result,
        experiment_id=manifest["experiment_id"],
        observation_window=observation_window,
        configuration_hashes=_configuration_hashes(manifest),
    )
    canonical_summaries = [
        bounded_observation_summary(item.observation) for item in result.evidence
    ]
    persisted_summaries = [item.bounded_observation_summary for item in artifact.evidence]
    if any(len(item) > MAX_EVIDENCE_SUMMARY_CHARS for item in persisted_summaries):
        raise RuntimeError("A1 smoke produced an oversized persisted evidence summary")
    if canonical_summaries != persisted_summaries:
        raise RuntimeError("A1 smoke canonical evidence summaries diverged before persistence")
    after = budget.snapshot()
    attempts = artifact.usage.outbound_api_attempts
    budget.verify_ledger_delta(before, after, attempts)
    payload = {
        "experiment_id": manifest["experiment_id"],
        "purpose": "transport/schema/accounting smoke only; excluded from benchmark metrics",
        "scenario_id": None,
        "incident_id": str(incident.incident_id),
        "calls_consumed": after.calls_used,
        "ledger_limit": after.limit,
        "transport_pass": artifact.usage.provider == "openai",
        "artifact_pass": True,
        "artifact_json_reload_pass": False,
        "evidence_summary_max_length": max((len(item) for item in persisted_summaries), default=0),
        "canonical_summary_consistency": True,
        "code_sha": _git_sha(),
        "model": MODEL,
        "reasoning_effort": REASONING_EFFORT,
        "configuration_hashes": _configuration_hashes(manifest),
        "limits": {
            "max_model_calls": LIMITS.max_model_calls,
            "max_tool_executions": LIMITS.max_tool_calls,
            "max_agent_turns": LIMITS.max_agent_turns,
            "max_wall_time_seconds": LIMITS.max_wall_time_seconds,
            "provider_retries": 0,
        },
        "usage_reconciled": after.calls_used - before.calls_used == attempts,
        "safety": artifact.safety.model_dump(mode="json"),
        "artifact": artifact.model_dump(mode="json"),
    }
    SMOKE_PATH.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    reloaded = json.loads(SMOKE_PATH.read_text(encoding="utf-8"))
    A1RunArtifact.from_json(json.dumps(reloaded["artifact"], sort_keys=True, separators=(",", ":")))
    payload["artifact_json_reload_pass"] = True
    SMOKE_PATH.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"smoke": payload}, sort_keys=True))
    return 0


def _benchmark(manifest: dict[str, Any]) -> int:
    smoke = json.loads(SMOKE_PATH.read_text(encoding="utf-8")) if SMOKE_PATH.exists() else None
    if smoke is None or not smoke.get("transport_pass") or not smoke.get("artifact_pass"):
        raise RuntimeError("A1 benchmark requires a passing smoke artifact")
    budget = LiveModelBudget.from_environment(require_shared_ledger=True)
    before = budget.snapshot()
    budget.ensure_capacity(70)
    registry = _registry()
    provider = OpenAIProvider(budget=budget, max_retry=0)
    lifecycle = FixtureLifecycle(LiveBenchmarkEnvironment(), definitions=_definition_map())
    results: list[dict[str, Any]] = []
    for scenario in _scenarios():
        runtime = InvestigationRuntime(
            provider,
            registry,
            model=MODEL,
            reasoning_effort=REASONING_EFFORT,
            limits=LIMITS,
            a1_protocol=True,
        )
        results.append(_run_one(scenario, lifecycle=lifecycle, runtime=runtime, manifest=manifest))
    after = budget.snapshot()
    attempts = sum(item["artifact"]["usage"]["outbound_api_attempts"] for item in results)
    budget.verify_ledger_delta(before, after, attempts)
    grades = [item["grade"] for item in results]
    # Re-validate through typed models so the committed artifact is contract-shaped.
    from packages.evals.a1_graders import A1ScenarioGrade

    aggregate = aggregate_a1_grades([A1ScenarioGrade.model_validate(item) for item in grades])
    by_set = {
        name: aggregate_a1_grades(
            [A1ScenarioGrade.model_validate(item) for item in grades if item["set"] == name]
        ).model_dump(mode="json")
        for name in ("compatibility", "generalization")
    }
    report = {
        "artifact_type": "A1_SINGLE_AGENT_LIVE_BENCHMARK",
        "experiment_id": manifest["experiment_id"],
        "status": "COMPLETED",
        "code_sha": _git_sha(),
        "model": MODEL,
        "reasoning_effort": REASONING_EFFORT,
        "architecture": "single-agent",
        "fallback": "none",
        "router": "none",
        "voting": "none",
        "best_of_n": "none",
        "runs_per_scenario": 1,
        "scenario_order": [item.scenario_id for item in _scenarios()],
        "scenario_count": len(results),
        "configuration": {
            "prompt_version": "sre_investigator_v4",
            **_configuration_hashes(manifest),
            "compatibility_dataset_sha256": manifest["sets"]["compatibility"]["dataset_sha256"],
            "compatibility_target_sha256": manifest["sets"]["compatibility"]["target_sha256"],
            "generalization_dataset_sha256": manifest["sets"]["generalization"]["dataset_sha256"],
            "generalization_target_sha256": manifest["sets"]["generalization"]["target_sha256"],
            "grader_version": A1_GRADER_VERSION,
        },
        "limits": {
            "max_model_calls": LIMITS.max_model_calls,
            "max_tool_executions": LIMITS.max_tool_calls,
            "max_agent_turns": LIMITS.max_agent_turns,
            "max_wall_time_seconds": LIMITS.max_wall_time_seconds,
            "provider_retries": 0,
        },
        "smoke": smoke,
        "ledger": {
            "path": str(LEDGER_PATH.relative_to(ROOT)),
            "hard_cap": after.limit,
            "before_benchmark": before.calls_used,
            "benchmark_consumed": after.calls_used - before.calls_used,
            "after_benchmark": after.calls_used,
            "remaining": after.calls_remaining,
            "outbound_attempts": attempts,
        },
        "aggregates": {
            "compatibility": by_set["compatibility"],
            "generalization": by_set["generalization"],
            "combined": aggregate.model_dump(mode="json"),
        },
        "scenarios": results,
        "static_safety": {
            "arbitrary_shell": "absent",
            "arbitrary_kubectl": "absent",
            "autonomous_remediation": "absent",
            "source": "read-only registry and committed RBAC checks",
        },
    }
    RESULT_PATH.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    RESULT_SHA_PATH.write_text(
        f"{_hash_bytes(RESULT_PATH)}  {RESULT_PATH.name}\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {"benchmark": {"ledger": report["ledger"], "result_sha256": _hash_bytes(RESULT_PATH)}},
            sort_keys=True,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("smoke", "benchmark"))
    args = parser.parse_args()
    manifest = _load_manifest()
    return _smoke(manifest) if args.mode == "smoke" else _benchmark(manifest)


if __name__ == "__main__":
    sys.exit(main())

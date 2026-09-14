#!/usr/bin/env python3
"""Run the frozen ITBench-Lite E1 trial set exactly once per scenario."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from packages.evals.itbench import (
    ITBENCH_DATASET_REVISION,
    ITBENCH_SCENARIO_IDS,
    ITBENCH_SRE_VERSION,
    ITBenchEvidenceCategory,
    ITBenchLiteDataset,
    ITBenchRunStore,
    ITBenchSnapshotBackend,
    ITBenchSnapshotToolRegistry,
    adapt_a1_output,
    atomic_json_write,
    build_observable_incident,
    entities_from_k8s_records,
    grade_root_cause_entities,
    macro_average,
)
from packages.investigation import InvestigationLimits, InvestigationRuntime
from packages.investigation.artifacts import A1RunArtifact
from packages.investigation.context import derive_observation_window
from packages.investigation.prompt import investigator_prompt_v4_hash
from packages.investigation.topology import DEFAULT_TOPOLOGY
from packages.provider import LiveModelBudget, OpenAIProvider, live_model_config

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = Path(os.getenv("ITBENCH_LITE_ROOT", ROOT / ".local" / "itbench-lite"))
RUN_ROOT = ROOT / ".local" / "itbench-lite-runs"
PARTIAL_PATH = ROOT / "docs" / "benchmarks" / "itbench-lite-e1-partial.json"
RESULT_PATH = ROOT / "docs" / "benchmarks" / "itbench-lite-e1-results.json"
RESULT_SHA_PATH = ROOT / "docs" / "benchmarks" / "itbench-lite-e1-results.sha256"
MANIFEST_PATH = ROOT / "docs" / "benchmarks" / "itbench-lite-manifest.json"
EXPERIMENT = "ITB-E1"
EXPERIMENT_NAME = "itbench-lite-sre-external-eval-v1"
MODEL = "gpt-5.6-luna"
REASONING = "none"
LIMITS = InvestigationLimits(
    max_model_calls=5,
    max_tool_calls=12,
    max_agent_turns=5,
    max_wall_time_seconds=180,
)


def _digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _configuration_hashes(manifest_sha: str, tool_sha: str) -> dict[str, str]:
    return {
        "prompt_sha256": investigator_prompt_v4_hash(),
        "topology_sha256": DEFAULT_TOPOLOGY.topology_hash(),
        "component_registry_sha256": DEFAULT_TOPOLOGY.component_registry_hash(),
        "tool_registry_sha256": tool_sha,
        "tool_contracts_sha256": tool_sha,
        "grader": "itbench_root_cause_entity_v1",
        "evidence_context": "a1_evidence_context_v2",
        "dataset_revision": ITBENCH_DATASET_REVISION,
        "manifest_sha256": manifest_sha,
    }


def _git_sha() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


def _preflight(dataset: ITBenchLiteDataset) -> tuple[Any, ...]:
    """Validate frozen inputs and refuse any prior official trial output."""
    if not MANIFEST_PATH.exists():
        raise RuntimeError(f"external manifest missing: {MANIFEST_PATH}")
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if manifest.get("dataset_revision") != ITBENCH_DATASET_REVISION:
        raise RuntimeError("external manifest dataset revision mismatch")
    if manifest.get("scenario_count") != 35:
        raise RuntimeError("external manifest scenario count mismatch")
    if RESULT_PATH.exists() or RESULT_SHA_PATH.exists() or PARTIAL_PATH.exists():
        raise RuntimeError("existing E1 result/partial artifact; refusing rerun")
    official_dirs = [RUN_ROOT / scenario_id for scenario_id in ITBENCH_SCENARIO_IDS]
    if any(path.exists() for path in official_dirs):
        raise RuntimeError("official E1 trial output already exists; refusing rerun")
    scenarios = dataset.scenarios()
    if tuple(item.scenario_id for item in scenarios) != ITBENCH_SCENARIO_IDS:
        raise RuntimeError("dataset scenario order differs from the frozen order")
    before = _budget_snapshot()
    if before["calls_used"] != 5 or before["limit"] != 180:
        raise RuntimeError(f"unexpected E1 ledger preflight: {before}")
    config = live_model_config()
    if config.model != MODEL or config.reasoning_effort != REASONING or not config.enabled:
        raise RuntimeError("frozen external model configuration is not exact/enabled")
    return scenarios


def _budget_snapshot() -> dict[str, int]:
    ledger_path = os.getenv("SRE_LIVE_MODEL_BUDGET_FILE")
    if not ledger_path:
        raise RuntimeError("SRE_LIVE_MODEL_BUDGET_FILE is required")
    payload = json.loads(Path(ledger_path).read_text(encoding="utf-8"))
    calls_used = int(payload.get("calls_used", payload.get("consumed", -1)))
    limit = int(payload.get("hard_cap", payload.get("cap", 180)))
    return {"calls_used": calls_used, "limit": limit, "calls_remaining": limit - calls_used}


def _partial(completed: list[dict[str, Any]], current: str | None, reason: str | None) -> None:
    """Persist forensic progress after every completed scenario."""
    refs = [
        {
            "scenario_id": item["scenario_id"],
            "checkpoint_path": item["checkpoint_path"],
            "checkpoint_sha256": item["checkpoint_sha256"],
        }
        for item in completed
    ]
    atomic_json_write(
        PARTIAL_PATH,
        {
            "execution": EXPERIMENT,
            "experiment": EXPERIMENT_NAME,
            "status": "IN_PROGRESS" if reason is None else "INVALIDATED",
            "completed_scenario_ids": [item["scenario_id"] for item in completed],
            "checkpoint_references": refs,
            "current_scenario": current,
            "next_expected_scenario": (
                ITBENCH_SCENARIO_IDS[len(completed)]
                if len(completed) < len(ITBENCH_SCENARIO_IDS)
                else None
            ),
            "invalidation_reason": reason,
            "ledger": _budget_snapshot(),
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )


def _tool_accounting(result: Any) -> dict[str, Any]:
    turns = result.turns
    counts: dict[str, int] = {category.value: 0 for category in ITBenchEvidenceCategory}
    for turn in turns:
        for name in turn.get("requested_tool_names", []):
            for category in ITBenchEvidenceCategory:
                if (
                    name == f"itbench_{category.value}"
                    or (
                        category is ITBenchEvidenceCategory.K8S_EVENTS
                        and name == "itbench_kubernetes_events"
                    )
                    or (
                        category is ITBenchEvidenceCategory.K8S_OBJECTS
                        and name == "itbench_kubernetes_objects"
                    )
                ):
                    counts[category.value] += 1
    return {
        "requests": result.usage.tool_requests_total,
        "valid_requests": sum(int(item.get("tool_calls_succeeded", 0)) for item in turns),
        "invalid_requests": sum(
            int(item.get("tool_calls_failed", 0))
            for item in turns
            if item.get("validation_stage") in {"TOOL_ARGUMENTS", "TOOL_REGISTRY"}
        ),
        "duplicate_reused": result.usage.duplicate_requests_suppressed,
        "executions": result.usage.tool_calls,
        "backend_failures": sum(
            int(item.get("tool_calls_failed", 0))
            for item in turns
            if item.get("validation_stage") == "TOOL_EXECUTION"
        ),
        "by_category": counts,
    }


def _run_scenario(
    scenario: Any,
    provider: Any,
    dataset: ITBenchLiteDataset,
    manifest_sha: str,
    tool_sha: str,
    checkout_sha: str,
) -> dict[str, Any]:
    started = time.monotonic()
    ledger_before = _budget_snapshot()
    backend = ITBenchSnapshotBackend(dataset, scenario)
    registry = ITBenchSnapshotToolRegistry(backend)
    incident, alerts = build_observable_incident(backend)
    runtime = InvestigationRuntime(
        provider,
        registry.investigation_registry(),
        model=MODEL,
        reasoning_effort=REASONING,
        limits=LIMITS,
        a1_protocol=True,
    )
    result = runtime.run(incident, alerts=alerts)
    if result.termination_reason.value in {"PROVIDER_ERROR", "TOOL_FAILURE"}:
        raise RuntimeError(f"runtime integration failure: {result.termination_reason.value}")
    observation_window = derive_observation_window(incident, alerts).time_window()
    artifact = A1RunArtifact.from_result(
        result,
        experiment_id=EXPERIMENT,
        observation_window=observation_window,
        configuration_hashes=_configuration_hashes(manifest_sha, tool_sha),
    )
    if any(len(item.bounded_observation_summary) > 1000 for item in artifact.evidence):
        raise RuntimeError("bounded evidence summary exceeded 1000 characters")
    native_output = result.model_dump(mode="json")
    observed_entities = entities_from_k8s_records(
        backend.records(ITBenchEvidenceCategory.K8S_OBJECTS)
    )
    output = adapt_a1_output(
        scenario_id=scenario.scenario_id,
        incident_id=str(incident.incident_id),
        native_output=native_output,
        observed_entities=observed_entities,
    )
    store = ITBenchRunStore(RUN_ROOT, execution_id=EXPERIMENT)
    checkpoint_sha = store.write_trial(
        scenario.scenario_id,
        1,
        native_artifact=artifact.model_dump(mode="json"),
        itbench_output=output,
        usage=result.usage.model_dump(mode="json"),
    )
    trial_dir = RUN_ROOT / scenario.scenario_id / "1"
    reloaded_artifact = A1RunArtifact.from_json((trial_dir / "native_artifact.json").read_bytes())
    if artifact.model_dump(mode="json") != reloaded_artifact.model_dump(mode="json"):
        raise RuntimeError("native artifact semantic reload mismatch")
    reloaded_output = output.__class__.model_validate_json(
        (trial_dir / "outputs" / "agent_output.json").read_bytes()
    )
    if reloaded_output.model_dump(mode="json") != output.model_dump(mode="json"):
        raise RuntimeError("ITBench output reload mismatch")

    # Ground truth is intentionally loaded only after both investigator outputs are durable.
    ground_truth = dataset.load_ground_truth(scenario.scenario_id)
    grade = grade_root_cause_entities(output, ground_truth)
    grade_sha = atomic_json_write(trial_dir / "grade.json", grade.model_dump(mode="json"))
    usage_sha = _digest(trial_dir / "usage.json")
    output_sha = _digest(trial_dir / "itbench_output.json")
    trial_manifest = {
        "execution": EXPERIMENT,
        "experiment": EXPERIMENT_NAME,
        "scenario_id": scenario.scenario_id,
        "trial": 1,
        "dataset_revision": ITBENCH_DATASET_REVISION,
        "adapter": "itbench_lite_snapshot_adapter_v2",
        "checkout_sha": checkout_sha,
        "model": MODEL,
        "reasoning": REASONING,
        "limits": {"model_calls": 5, "tool_executions": 12, "turns": 5, "wall_seconds": 180},
        "manifest_sha256": manifest_sha,
        "native_artifact_sha256": _digest(trial_dir / "native_artifact.json"),
        "itbench_output_sha256": output_sha,
        "usage_sha256": usage_sha,
        "grade_sha256": grade_sha,
        "provider_calls_before": ledger_before["calls_used"],
        "provider_calls_consumed": result.usage.outbound_api_attempts,
        "provider_calls_after": _budget_snapshot()["calls_used"],
        "completed_at": datetime.now(UTC).isoformat(),
    }
    atomic_json_write(trial_dir / "trial_manifest.json", trial_manifest)
    return {
        "scenario_id": scenario.scenario_id,
        "checkpoint_path": str(trial_dir),
        "checkpoint_sha256": checkpoint_sha,
        "grade": grade.model_dump(mode="json"),
        "terminal": result.termination_reason.value,
        "provider_calls": result.usage.outbound_api_attempts,
        "usage": result.usage.model_dump(mode="json"),
        "tool_accounting": _tool_accounting(result),
        "evidence_count": len(result.evidence),
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


def _assemble(completed: list[dict[str, Any]]) -> None:
    """Assemble the authoritative result only from persisted checkpoints."""
    if tuple(item["scenario_id"] for item in completed) != ITBENCH_SCENARIO_IDS:
        raise RuntimeError("final aggregation scenario order mismatch")
    grades = []
    scenarios: list[dict[str, Any]] = []
    for item in completed:
        trial_dir = Path(item["checkpoint_path"])
        payload = json.loads((trial_dir / "grade.json").read_text(encoding="utf-8"))
        grade = payload
        scenarios.append(item)
        from packages.evals.itbench.contracts import ITBenchAgentOutput

        # Validate all persisted nested artifacts before aggregate creation.
        A1RunArtifact.from_json((trial_dir / "native_artifact.json").read_bytes())
        ITBenchAgentOutput.model_validate_json(
            (trial_dir / "outputs" / "agent_output.json").read_bytes()
        )
        from packages.evals.itbench.grader import ITBenchEntityGrade

        grades.append(ITBenchEntityGrade.model_validate(grade))
    aggregate = macro_average(grades)
    usage = {
        "provider_calls": sum(int(item["usage"]["outbound_api_attempts"]) for item in scenarios),
        "input_tokens": sum(int(item["usage"]["input_tokens"]) for item in scenarios),
        "output_tokens": sum(int(item["usage"]["output_tokens"]) for item in scenarios),
        "total_duration_ms": sum(int(item["duration_ms"]) for item in scenarios),
        "tool_executions": sum(int(item["usage"]["tool_calls"]) for item in scenarios),
        "evidence_count": sum(int(item["evidence_count"]) for item in scenarios),
    }
    payload = {
        "artifact_type": "ITBENCH_LITE_E1_RESULTS",
        "execution": EXPERIMENT,
        "experiment": EXPERIMENT_NAME,
        "dataset_revision": ITBENCH_DATASET_REVISION,
        "sre_version": ITBENCH_SRE_VERSION,
        "scenario_order": list(ITBENCH_SCENARIO_IDS),
        "scenario_count": 35,
        "trial_count": 35,
        "official_judge_run": False,
        "macro_entity_metrics": aggregate,
        "usage": usage,
        "scenarios": scenarios,
        "ledger": _budget_snapshot(),
        "official_evaluator": "NOT RUN during primary E1",
    }
    atomic_json_write(RESULT_PATH, payload)
    atomic_json_write(RESULT_SHA_PATH, f"{_digest(RESULT_PATH)}  {RESULT_PATH.name}\n")


def main() -> int:
    """Execute the frozen 35-scenario external benchmark once."""
    provider_started = False
    try:
        dataset = ITBenchLiteDataset.open(DATA_ROOT)
        scenarios = _preflight(dataset)
        tool_probe = ITBenchSnapshotToolRegistry(
            ITBenchSnapshotBackend(dataset, scenarios[0])
        ).descriptors()
        tool_sha = sha256(
            json.dumps(tool_probe, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        manifest_sha = _digest(MANIFEST_PATH)
        budget = LiveModelBudget.from_environment(require_shared_ledger=True)
        budget.ensure_capacity(175)
        provider = OpenAIProvider(budget=budget, max_retry=0)
        provider_started = True
        completed: list[dict[str, Any]] = []
        checkout_sha = _git_sha()
        for index, scenario in enumerate(scenarios, start=1):
            print(f"[{index}/35] START {scenario.scenario_id}", flush=True)
            try:
                item = _run_scenario(
                    scenario, provider, dataset, manifest_sha, tool_sha, checkout_sha
                )
            except Exception as error:
                _partial(completed, scenario.scenario_id, str(error)[:500])
                print(
                    json.dumps(
                        {
                            "classification": "ITB_E1_INVALIDATED_BY_INTEGRATION_FAILURE",
                            "scenario": scenario.scenario_id,
                            "completed": [item["scenario_id"] for item in completed],
                            "error": str(error)[:500],
                            "ledger": _budget_snapshot(),
                        },
                        sort_keys=True,
                    ),
                    file=sys.stderr,
                    flush=True,
                )
                return 1
            completed.append(item)
            _partial(completed, None, None)
            print(
                f"[{index}/35] PASS {scenario.scenario_id} "
                f"terminal={item['terminal']} calls={item['provider_calls']}",
                flush=True,
            )
        _assemble(completed)
        print(
            json.dumps(
                {
                    "classification": "ITB_E1_COMPLETE",
                    "scenarios": 35,
                    "ledger": _budget_snapshot(),
                    "result": str(RESULT_PATH),
                    "result_sha256": _digest(RESULT_PATH),
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return 0
    except Exception as error:
        print(
            json.dumps(
                {
                    "classification": (
                        "ITB_E1_INVALIDATED_BY_INTEGRATION_FAILURE"
                        if provider_started
                        else "ITB_E1_INCOMPLETE"
                    ),
                    "provider_started": provider_started,
                    "error": str(error)[:500],
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

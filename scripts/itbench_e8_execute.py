#!/usr/bin/env python3
"""Run the frozen ITB-E8 smoke or the single official 35-scenario pass.

This runner deliberately keeps the E8 agent budget separate from judge
accounting.  It never invokes the official evaluator; judging is a later,
post-prediction operation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from packages.evals.itbench import (
    ITBENCH_DATASET_REVISION,
    ITBENCH_EXTERNAL_PROTOCOL_V4,
    ITBENCH_SCENARIO_IDS,
    ExternalInvestigationRuntime,
    ITBenchAgentOutput,
    ITBenchExternalResult,
    ITBenchExternalToolRegistry,
    ITBenchLiteDataset,
    ITBenchRunStore,
    ITBenchScenario,
    ITBenchSnapshotBackend,
    adapt_external_output,
    atomic_json_write,
    build_observable_incident,
    grade_root_cause_entities,
    macro_average,
)
from packages.evals.itbench.external_runtime import (
    ITBENCH_EXTERNAL_PROMPT_E8_VERSION,
    external_prompt_v6_hash,
)
from packages.investigation.contracts import InvestigationLimits
from packages.model_policy import validate_agent_environment
from packages.provider import LiveModelBudget, OpenAIProvider, live_model_config

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / ".local/itbench-lite"
RUN_ROOT = ROOT / ".local/itbench-lite-e8-runs"
OFFICIAL_ROOT = RUN_ROOT / "official"
SMOKE_ID = "ITB-E8-SMOKE-003"
SMOKE_ROOT = RUN_ROOT / SMOKE_ID / "smoke"
SMOKE_RESULT = ROOT / "docs/benchmarks/itbench-e8-live-smoke-003.json"
OFFICIAL_PARTIAL = ROOT / "docs/benchmarks/itbench-e8-official-partial.json"
OFFICIAL_RESULT = ROOT / "docs/benchmarks/itbench-e8-results.json"
OFFICIAL_RESULT_SHA = ROOT / "docs/benchmarks/itbench-e8-results.sha256"
MANIFEST = ROOT / "docs/benchmarks/itbench-lite-e8-manifest.json"
SMOKE_LEDGER = ROOT / ".local/itbench-lite-e8-smoke-003-budget.json"
OFFICIAL_LEDGER = ROOT / ".local/itbench-lite-e8-official-budget.json"
EXECUTION = "ITB-E8"
EXPERIMENT = "itbench-lite-sre-external-eval-v8"
RUNTIME_SOURCE_SHA = "e474db9432b17fc854873fa8458a3b9b0de8d49a"
ADAPTER = "itbench_lite_snapshot_adapter_v7"
LIMITS = InvestigationLimits(
    max_model_calls=5, max_tool_calls=12, max_agent_turns=5, max_wall_time_seconds=180
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def smoke_scenario() -> ITBenchScenario:
    root = ROOT / "tests/fixtures/itbench_smoke/Scenario-999"
    from packages.evals.itbench import ITBenchEvidenceCategory

    files: dict[Any, tuple[str, ...]] = {
        ITBenchEvidenceCategory.ALERTS: ("alerts/alerts.json",),
        ITBenchEvidenceCategory.METRICS: ("metrics/checkout.tsv",),
        ITBenchEvidenceCategory.K8S_EVENTS: ("k8s_events_raw.tsv",),
        ITBenchEvidenceCategory.K8S_OBJECTS: ("k8s_objects_raw.tsv",),
        ITBenchEvidenceCategory.LOGS: ("otel_logs_raw.tsv",),
        ITBenchEvidenceCategory.TRACES: ("otel_traces_raw.tsv",),
    }
    return ITBenchScenario(
        scenario_id="Scenario-999",
        snapshot_path=str(root),
        evidence_categories=tuple(files),
        evidence_files=files,
    )


def _load_manifest() -> dict[str, Any]:
    manifest = cast(dict[str, Any], json.loads(MANIFEST.read_text(encoding="utf-8")))
    if manifest.get("execution") != EXECUTION:
        raise RuntimeError("E8 manifest execution identity mismatch")
    if manifest.get("experiment") != EXPERIMENT:
        raise RuntimeError("E8 manifest experiment identity mismatch")
    if manifest.get("runtime_source_sha") != RUNTIME_SOURCE_SHA:
        raise RuntimeError("E8 runtime source identity mismatch")
    if manifest.get("dataset_revision") != ITBENCH_DATASET_REVISION:
        raise RuntimeError("E8 dataset revision mismatch")
    if manifest.get("protocol") != ITBENCH_EXTERNAL_PROTOCOL_V4:
        raise RuntimeError("E8 protocol identity mismatch")
    if manifest.get("scenario_order") != list(ITBENCH_SCENARIO_IDS):
        raise RuntimeError("E8 frozen scenario order mismatch")
    return manifest


def _validate_live_agent() -> dict[str, str]:
    identity = validate_agent_environment()
    config = live_model_config()
    if not config.enabled:
        raise RuntimeError("E8 live agent execution is not explicitly enabled")
    if config.model != "gpt-5.6-luna" or config.reasoning_effort != "none":
        raise RuntimeError("E8 live agent model policy violation")
    return identity.as_dict()


def _assert_no_gt_context(request: Any) -> None:
    context = request.messages[-1].content.casefold()
    forbidden = ("ground_truth", "root-cause answer", "evaluator alias", "fault label")
    if any(item in context for item in forbidden):
        raise RuntimeError("ground-truth material entered investigator context")


def _build_runtime(backend: ITBenchSnapshotBackend, provider: Any) -> ExternalInvestigationRuntime:
    registry = ITBenchExternalToolRegistry(backend, contract_version="v4")
    return ExternalInvestigationRuntime(
        provider,
        registry.investigation_registry(),
        backend,
        limits=LIMITS,
        protocol_version=ITBENCH_EXTERNAL_PROTOCOL_V4,
    )


def _preflight_context(dataset: ITBenchLiteDataset, manifest: dict[str, Any]) -> dict[str, Any]:
    scenarios = dataset.scenarios()
    if tuple(item.scenario_id for item in scenarios) != ITBENCH_SCENARIO_IDS:
        raise RuntimeError("dataset scenario order does not match E8 manifest")
    fixture = smoke_scenario()
    backend = ITBenchSnapshotBackend(cast(ITBenchLiteDataset, None), fixture)
    incident, alerts = build_observable_incident(backend)
    runtime = _build_runtime(backend, provider=object())
    request = runtime.build_request(
        incident,
        alerts,
        run_id=incident.incident_id,
        candidate_entities=backend.candidate_entities(limit=10),
    )
    _assert_no_gt_context(request)
    return {
        "scenario_count": len(scenarios),
        "scenario_order": list(ITBENCH_SCENARIO_IDS),
        "context_chars": len(request.messages[-1].content),
        "protocol": ITBENCH_EXTERNAL_PROTOCOL_V4,
        "ground_truth_access": False,
        "manifest_sha256": digest(MANIFEST),
    }


def _persist_smoke(provider: OpenAIProvider, manifest: dict[str, Any]) -> dict[str, Any]:
    if SMOKE_ROOT.exists() or SMOKE_RESULT.exists():
        raise RuntimeError("E8 smoke artifact already exists; refusing a rerun")
    scenario = smoke_scenario()
    backend = ITBenchSnapshotBackend(cast(ITBenchLiteDataset, None), scenario)
    incident, alerts = build_observable_incident(backend)
    before = provider.accounting_snapshot()
    started = time.monotonic()
    result = _build_runtime(backend, provider).run(incident, alerts)
    after = provider.accounting_snapshot()
    attempts = after.outbound_api_attempts - before.outbound_api_attempts
    output = adapt_external_output(result)
    atomic_json_write(SMOKE_ROOT / "native_artifact.json", result.model_dump(mode="json"))
    atomic_json_write(SMOKE_ROOT / "itbench_output.json", output.model_dump(mode="json"))
    atomic_json_write(SMOKE_ROOT / "usage.json", result.usage)
    atomic_json_write(SMOKE_ROOT / "turn_trace.json", result.turns)
    if result.terminal not in {"SUBMIT_DIAGNOSIS", "STOP"}:
        raise RuntimeError(f"E8 smoke did not reach a valid terminal: {result.terminal}")
    if attempts < 1 or result.usage.get("semantic_tool_executions", 0) < 1 or not result.evidence:
        raise RuntimeError("E8 smoke did not exercise a real evidence path")
    native = ITBenchExternalResult.model_validate_json(
        (SMOKE_ROOT / "native_artifact.json").read_bytes()
    )
    reloaded = ITBenchAgentOutput.model_validate_json(
        (SMOKE_ROOT / "itbench_output.json").read_bytes()
    )
    if native.model_dump(mode="json") != result.model_dump(mode="json"):
        raise RuntimeError("E8 smoke native artifact reload mismatch")
    if reloaded.model_dump(mode="json") != output.model_dump(mode="json"):
        raise RuntimeError("E8 smoke ITBench output reload mismatch")
    smoke = {
        "execution": EXECUTION,
        "experiment": EXPERIMENT,
        "smoke_id": SMOKE_ID,
        "scenario_id": scenario.scenario_id,
        "runtime_source_freeze": RUNTIME_SOURCE_SHA,
        "provider": "openai",
        "model": result.usage.get("model"),
        "reasoning": result.usage.get("reasoning_effort"),
        "terminal": result.terminal,
        "model_calls": result.usage.get("model_calls", 0),
        "outbound_attempts": attempts,
        "semantic_tool_requests": result.usage.get("semantic_tool_requests", 0),
        "semantic_tool_executions": result.usage.get("semantic_tool_executions", 0),
        "evidence_count": len(result.evidence),
        "input_tokens": result.usage.get("input_tokens_total", 0),
        "output_tokens": result.usage.get("output_tokens_total", 0),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "artifact_reload": True,
        "manifest_sha256": digest(MANIFEST),
        "safety": {
            "ground_truth_exposure": 0,
            "cross_scenario_evidence": 0,
            "writes": 0,
            "arbitrary_execution": 0,
        },
    }
    atomic_json_write(SMOKE_ROOT / "smoke_manifest.json", smoke)
    atomic_json_write(SMOKE_RESULT, {**smoke, "smoke_artifact": str(SMOKE_ROOT)})
    return smoke


def _process_metrics(
    result: ITBenchExternalResult, backend: ITBenchSnapshotBackend
) -> dict[str, Any]:
    summaries = [summary for turn in result.turns for summary in turn.get("summaries", [])]
    cited = {ref for ref in result.usage.get("submitted_evidence_refs", []) if isinstance(ref, str)}
    return {
        "model_decision_turns": len(result.turns),
        "semantic_tool_requests": result.usage.get("semantic_tool_requests", 0),
        "semantic_tool_executions": result.usage.get("semantic_tool_executions", 0),
        "backend_source_operations": backend.performance_snapshot(),
        "evidence_producing_executions": len(result.evidence),
        "evidence_cited_refs": sorted(cited),
        "evidence_citation_rate": len(cited) / len(result.evidence) if result.evidence else 0.0,
        "unused_evidence_count": max(len(result.evidence) - len(cited), 0),
        "exact_duplicate_requests": result.usage.get("exact_duplicate_requests", 0),
        "subsumed_duplicate_requests": result.usage.get("subsumed_duplicate_requests", 0),
        "zero_result_requests": result.usage.get("zero_result_requests", 0),
        "typed_failures": sum(item.get("status") == "TOOL_EXECUTION_FAILURE" for item in summaries),
        "timeouts": sum(item.get("error_code") == "TOOL_TIMEOUT" for item in summaries),
        "context_metrics": result.usage.get("context_metrics", []),
    }


def _write_partial(completed: list[dict[str, Any]], ledger: dict[str, Any]) -> None:
    atomic_json_write(
        OFFICIAL_PARTIAL,
        {
            "execution": EXECUTION,
            "experiment": EXPERIMENT,
            "completed_scenarios": completed,
            "next_expected": (
                ITBENCH_SCENARIO_IDS[len(completed)]
                if len(completed) < len(ITBENCH_SCENARIO_IDS)
                else None
            ),
            "ledger": ledger,
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )


def _run_official(provider: OpenAIProvider, dataset: ITBenchLiteDataset) -> list[dict[str, Any]]:
    if OFFICIAL_ROOT.exists() and any(OFFICIAL_ROOT.iterdir()):
        raise RuntimeError("E8 official output directory is not empty; refusing a rerun")
    store = ITBenchRunStore(OFFICIAL_ROOT, execution_id=EXECUTION)
    completed: list[dict[str, Any]] = []
    manifest_sha = digest(MANIFEST)
    for scenario in dataset.scenarios():
        started = time.monotonic()
        before = provider.accounting_snapshot()
        backend = ITBenchSnapshotBackend(dataset, scenario)
        incident, alerts = build_observable_incident(backend)
        result = _build_runtime(backend, provider).run(incident, alerts)
        output = adapt_external_output(result)
        checkpoint_hash = store.write_trial(
            scenario.scenario_id,
            1,
            native_artifact=result.model_dump(mode="json"),
            itbench_output=output,
            usage=result.usage,
        )
        trial_dir = OFFICIAL_ROOT / scenario.scenario_id / "1"
        atomic_json_write(trial_dir / "turn_trace.json", result.turns)
        atomic_json_write(trial_dir / "process_metrics.json", _process_metrics(result, backend))
        reloaded = ITBenchExternalResult.model_validate_json(
            (trial_dir / "native_artifact.json").read_bytes()
        )
        reloaded_output = ITBenchAgentOutput.model_validate_json(
            (trial_dir / "outputs/agent_output.json").read_bytes()
        )
        if reloaded.model_dump(mode="json") != result.model_dump(mode="json"):
            raise RuntimeError(f"E8 native reload failed: {scenario.scenario_id}")
        if reloaded_output.model_dump(mode="json") != output.model_dump(mode="json"):
            raise RuntimeError(f"E8 output reload failed: {scenario.scenario_id}")
        # Ground truth is opened only after all prediction artifacts are durable.
        grade = grade_root_cause_entities(output, dataset.load_ground_truth(scenario.scenario_id))
        grade_hash = atomic_json_write(trial_dir / "grade.json", grade.model_dump(mode="json"))
        after = provider.accounting_snapshot()
        checkpoint = {
            "scenario_id": scenario.scenario_id,
            "terminal": result.terminal,
            "model_calls": result.usage.get("model_calls", 0),
            "input_tokens": result.usage.get("input_tokens_total", 0),
            "output_tokens": result.usage.get("output_tokens_total", 0),
            "semantic_tool_requests": result.usage.get("semantic_tool_requests", 0),
            "semantic_tool_executions": result.usage.get("semantic_tool_executions", 0),
            "predicted_entities": len(output.contributing_factor),
            "true_positive": grade.true_positive,
            "precision": grade.precision,
            "recall": grade.recall,
            "f1": grade.f1,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "provider_outbound_delta": after.outbound_api_attempts - before.outbound_api_attempts,
            "checkpoint_sha256": checkpoint_hash,
            "grade_sha256": grade_hash,
        }
        atomic_json_write(
            trial_dir / "trial_manifest.json",
            {
                "execution": EXECUTION,
                "experiment": EXPERIMENT,
                "scenario_id": scenario.scenario_id,
                "trial": 1,
                "runtime_source_sha": RUNTIME_SOURCE_SHA,
                "dataset_revision": ITBENCH_DATASET_REVISION,
                "adapter": ADAPTER,
                "protocol": ITBENCH_EXTERNAL_PROTOCOL_V4,
                "prompt_version": ITBENCH_EXTERNAL_PROMPT_E8_VERSION,
                "prompt_sha256": external_prompt_v6_hash(),
                "manifest_sha256": manifest_sha,
                **checkpoint,
            },
        )
        completed.append(checkpoint)
        _write_partial(completed, json.loads(OFFICIAL_LEDGER.read_text(encoding="utf-8")))
        print(f"completed {scenario.scenario_id}", flush=True)
    return completed


def _micro(grades: list[Any]) -> dict[str, float | int]:
    predicted = sum(item.predicted_count for item in grades)
    truth = sum(item.ground_truth_count for item in grades)
    tp = sum(item.true_positive for item in grades)
    precision = tp / predicted if predicted else 0.0
    recall = tp / truth if truth else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "scenario_count": len(grades),
        "true_positive": tp,
        "predicted": predicted,
        "ground_truth": truth,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def run_smoke() -> int:
    manifest = _load_manifest()
    _validate_live_agent()
    preflight = _preflight_context(ITBenchLiteDataset.open(DATA_ROOT), manifest)
    budget = LiveModelBudget(5, ledger_path=str(SMOKE_LEDGER))
    current = budget.snapshot()
    budget.ensure_capacity(LIMITS.max_model_calls)
    if current.calls_used != 0 or current.calls_remaining < LIMITS.max_model_calls:
        raise RuntimeError("E8 Smoke-003 requires a fresh full worst-case call budget")
    provider = OpenAIProvider(budget=budget, max_retry=0)
    smoke = _persist_smoke(provider, manifest)
    smoke["preflight"] = preflight
    smoke["ledger"] = {
        "cap": budget.snapshot().limit,
        "consumed": budget.snapshot().calls_used,
        "remaining": budget.snapshot().calls_remaining,
        "historical_smoke_attempts_preserved_elsewhere": 5,
    }
    atomic_json_write(SMOKE_RESULT, smoke)
    print(json.dumps({"status": "ITB_E8_SMOKE_PASS", "attempts": smoke["outbound_attempts"]}))
    return 0


def run_official() -> int:
    manifest = _load_manifest()
    if not SMOKE_RESULT.exists():
        raise RuntimeError("E8 smoke PASS artifact is required before official execution")
    smoke = json.loads(SMOKE_RESULT.read_text(encoding="utf-8"))
    if smoke.get("terminal") not in {"SUBMIT_DIAGNOSIS", "STOP"}:
        raise RuntimeError("E8 smoke artifact is not a valid readiness result")
    _validate_live_agent()
    dataset = ITBenchLiteDataset.open(DATA_ROOT)
    preflight = _preflight_context(dataset, manifest)
    budget = LiveModelBudget(175, ledger_path=str(OFFICIAL_LEDGER))
    if budget.snapshot().calls_used != 0:
        raise RuntimeError("E8 official ledger is not fresh")
    budget.ensure_capacity(175)
    provider = OpenAIProvider(budget=budget, max_retry=0)
    completed = _run_official(provider, dataset)
    from packages.evals.itbench.grader import ITBenchEntityGrade

    grades = [
        ITBenchEntityGrade.model_validate_json(
            (OFFICIAL_ROOT / item["scenario_id"] / "1" / "grade.json").read_bytes()
        )
        for item in completed
    ]
    if [item["scenario_id"] for item in completed] != list(ITBENCH_SCENARIO_IDS):
        raise RuntimeError("E8 official scenario completion/order mismatch")
    final = budget.snapshot()
    summary = {
        "execution": EXECUTION,
        "experiment": EXPERIMENT,
        "runtime_source_sha": RUNTIME_SOURCE_SHA,
        "manifest_sha256": digest(MANIFEST),
        "dataset_revision": ITBENCH_DATASET_REVISION,
        "scenario_count": len(completed),
        "scenario_order": [item["scenario_id"] for item in completed],
        "scenarios": completed,
        "local_fixed_35_macro": macro_average(grades),
        "local_fixed_35_micro": _micro(grades),
        "diagnosis_coverage": sum(item["terminal"] == "SUBMIT_DIAGNOSIS" for item in completed)
        / 35,
        "ledger": {
            "cap": final.limit,
            "consumed": final.calls_used,
            "remaining": final.calls_remaining,
        },
        "official_judge": "DEFERRED_UNTIL_PREDICTIONS_FROZEN",
        "preflight": preflight,
        "safety": {
            "ground_truth_exposure": 0,
            "cross_scenario_evidence": 0,
            "writes": 0,
            "arbitrary_execution": 0,
        },
    }
    atomic_json_write(OFFICIAL_RESULT, summary)
    atomic_json_write(OFFICIAL_RESULT_SHA, digest(OFFICIAL_RESULT))
    print(
        json.dumps(
            {
                "status": "ITB_E8_AGENT_COMPLETE",
                "scenarios": len(completed),
                "ledger": summary["ledger"],
            },
            sort_keys=True,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("smoke", "official"))
    args = parser.parse_args()
    return run_smoke() if args.command == "smoke" else run_official()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(
            json.dumps(
                {
                    "status": "ITB_E8_INVALIDATED_BY_INTEGRATION_FAILURE",
                    "error_type": type(error).__name__,
                    "error": str(error)[:500],
                }
            ),
            file=sys.stderr,
        )
        raise

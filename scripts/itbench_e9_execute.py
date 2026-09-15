#!/usr/bin/env python3
"""Run the frozen E9 smoke or one checkpointed 35-scenario agent pass."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any, cast

from packages.evals.itbench import (
    ITBENCH_DATASET_REVISION,
    ITBENCH_SCENARIO_IDS,
    ITBenchAgentOutput,
    ITBenchLiteDataset,
    ITBenchRunStore,
    ITBenchScenario,
    ITBenchSnapshotBackend,
    adapt_e9_output,
    atomic_json_write,
    build_observable_incident,
    grade_root_cause_entities,
    macro_average,
)
from packages.evals.itbench.e9_memory import E9CaseMemory
from packages.evals.itbench.e9_runtime import (
    ITBENCH_E9_PROMPT_VERSION,
    E9InvestigationRuntime,
    E9Limits,
)
from packages.model_policy import validate_agent_environment
from packages.provider import LiveModelBudget, OpenAIProvider, live_model_config

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / ".local/itbench-lite"
RUN_ROOT = ROOT / ".local/itbench-lite-e9-runs"
SMOKE_ID = "ITB-E9-SMOKE-003"
SMOKE_ROOT = RUN_ROOT / SMOKE_ID
OFFICIAL_ROOT = RUN_ROOT / "official"
SMOKE_RESULT = ROOT / "docs/benchmarks/itbench-e9-live-smoke-003.json"
OFFICIAL_RESULT = ROOT / "docs/benchmarks/itbench-e9-results.json"
OFFICIAL_PARTIAL = ROOT / "docs/benchmarks/itbench-e9-official-partial.json"
SMOKE_LEDGER = ROOT / ".local/itbench-lite-e9-smoke-003-budget.json"
OFFICIAL_LEDGER = ROOT / ".local/itbench-lite-e9-official-budget.json"
MANIFEST = ROOT / "docs/benchmarks/itbench-lite-e9-manifest.json"
EXECUTION = "ITB-E9"
EXPERIMENT = "itbench-lite-sre-external-eval-v9"
LIMITS = E9Limits()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ledger(path: Path, cap: int, *, status: str = "NOT_STARTED") -> dict[str, Any]:
    if path.exists():
        contents = path.read_text(encoding="utf-8").strip()
        if not contents:
            value = {}
        else:
            value = json.loads(contents)
        if not isinstance(value, dict):
            raise RuntimeError(f"invalid E9 ledger: {path}")
        if not value:
            value = {
                "execution": EXECUTION,
                "purpose": "AGENT",
                "cap": cap,
                "calls_used": 0,
                "remaining": cap,
                "status": status,
                "scenario_ids": [],
                "model": "gpt-5.6-luna",
                "reasoning": "none",
            }
            atomic_json_write(path, value)
        return value
    value = {
        "execution": EXECUTION,
        "purpose": "AGENT",
        "cap": cap,
        "calls_used": 0,
        "remaining": cap,
        "status": status,
        "scenario_ids": [],
        "model": "gpt-5.6-luna",
        "reasoning": "none",
    }
    atomic_json_write(path, value)
    return value


def _validate_manifest() -> dict[str, Any]:
    if not MANIFEST.is_file():
        raise RuntimeError(f"missing frozen E9 manifest: {MANIFEST}")
    value = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("E9 manifest is not an object")
    for key, expected in {
        "execution": EXECUTION,
        "experiment": EXPERIMENT,
        "dataset_revision": ITBENCH_DATASET_REVISION,
        "prompt_version": ITBENCH_E9_PROMPT_VERSION,
        "scenario_order": list(ITBENCH_SCENARIO_IDS),
    }.items():
        if value.get(key) != expected:
            raise RuntimeError(f"E9 manifest mismatch for {key}")
    return cast(dict[str, Any], value)


def _validate_live_agent() -> dict[str, str]:
    identity = validate_agent_environment()
    config = live_model_config()
    if not config.enabled:
        raise RuntimeError("E9 live agent execution is not explicitly enabled")
    if config.model != "gpt-5.6-luna" or config.reasoning_effort != "none":
        raise RuntimeError("E9 model policy violation")
    return identity.as_dict()


def _smoke_scenario() -> ITBenchScenario:
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


def _runtime(provider: Any, backend: ITBenchSnapshotBackend) -> E9InvestigationRuntime:
    return E9InvestigationRuntime(provider, backend, limits=LIMITS, execution_id=EXECUTION)


def _strict_reload(root: Path, result: dict[str, Any], output: ITBenchAgentOutput) -> None:
    native = json.loads((root / "native_artifact.json").read_text(encoding="utf-8"))
    persisted_output = ITBenchAgentOutput.model_validate_json(
        (root / "outputs" / "agent_output.json").read_bytes()
    )
    if native != result:
        raise RuntimeError(f"native artifact reload mismatch: {root}")
    if persisted_output.model_dump(mode="json") != output.model_dump(mode="json"):
        raise RuntimeError(f"ITBench output reload mismatch: {root}")
    E9CaseMemory.replay(native)


def _persist_case(
    root: Path,
    result: dict[str, Any],
    output: ITBenchAgentOutput,
    *,
    store: ITBenchRunStore | None = None,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    if store is None:
        atomic_json_write(root / "native_artifact.json", result)
        atomic_json_write(root / "outputs" / "agent_output.json", output.model_dump(mode="json"))
        atomic_json_write(root / "usage.json", result["usage"])
    else:
        store.write_trial(
            result["scenario_id"],
            1,
            native_artifact=result,
            itbench_output=output,
            usage=result["usage"],
        )
        root = OFFICIAL_ROOT / result["scenario_id"] / "1"
    atomic_json_write(root / "turn_trace.json", result["turns"])
    atomic_json_write(root / "case_state.json", result["case_state"])
    atomic_json_write(root / "event_log.json", result["events"])
    _strict_reload(root, result, output)


def _summary_metrics(result: dict[str, Any]) -> dict[str, Any]:
    usage = result["usage"]
    return {
        "model_steps": usage.get("model_steps", 0),
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "semantic_actions_requested": usage.get("semantic_actions_requested", 0),
        "semantic_actions_executed": usage.get("semantic_actions_executed", 0),
        "action_rejections": usage.get("action_rejections", 0),
        "recovered_action_rejections": usage.get("recovered_action_rejections", 0),
        "provider_latency_ms": usage.get("provider_latency_ms", 0),
        "terminal": result["terminal"],
    }


def run_smoke() -> int:
    manifest = _validate_manifest()
    if SMOKE_RESULT.exists() or SMOKE_ROOT.exists():
        raise RuntimeError("E9 smoke artifact already exists; refusing a rerun")
    identity = _validate_live_agent()
    budget = LiveModelBudget(LIMITS.max_model_calls, ledger_path=str(SMOKE_LEDGER))
    before = budget.snapshot()
    budget.ensure_capacity(LIMITS.max_model_calls)
    if before.calls_used != 0 or before.calls_remaining < LIMITS.max_model_calls:
        raise RuntimeError("E9 smoke requires the complete fresh worst-case budget")
    atomic_json_write(
        SMOKE_LEDGER,
        {
            **_ledger(SMOKE_LEDGER, LIMITS.max_model_calls),
            "status": "RUNNING",
            "preflight": identity,
        },
    )
    provider = OpenAIProvider(budget=budget, max_retry=0)
    backend = ITBenchSnapshotBackend(cast(Any, None), _smoke_scenario())
    incident, alerts = build_observable_incident(backend)
    started = time.monotonic()
    result = _runtime(provider, backend).run(incident, alerts)
    after = provider.accounting_snapshot()
    output = adapt_e9_output(result)
    _persist_case(SMOKE_ROOT, result, output)
    if result["terminal"] not in {"SUBMIT", "STOP"}:
        raise RuntimeError(f"E9 smoke did not reach a valid terminal: {result['terminal']}")
    if after.outbound_api_attempts < 1 or not result["evidence"]:
        raise RuntimeError("E9 smoke did not exercise a real semantic evidence path")
    snapshot = budget.snapshot()
    smoke = {
        "execution": EXECUTION,
        "experiment": EXPERIMENT,
        "smoke_id": SMOKE_ID,
        "scenario_id": "Scenario-999",
        "runtime_source_sha": manifest["runtime_source_sha"],
        "provider": identity["provider"],
        "model": identity["model"],
        "reasoning": identity["reasoning"],
        "turns": result["turns"],
        "terminal": result["terminal"],
        "metrics": _summary_metrics(result),
        "outbound_attempts": after.outbound_api_attempts,
        "artifact_reload": True,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "ledger": {
            "cap": snapshot.limit,
            "calls_used": snapshot.calls_used,
            "remaining": snapshot.calls_remaining,
        },
        "manifest_sha256": _sha256(MANIFEST),
        "safety": result["safety"],
    }
    atomic_json_write(
        SMOKE_LEDGER,
        {**_ledger(SMOKE_LEDGER, LIMITS.max_model_calls), **smoke, "status": "COMPLETE"},
    )
    atomic_json_write(SMOKE_RESULT, smoke)
    print(json.dumps({"status": "ITB_E9_SMOKE_PASS", "terminal": result["terminal"]}))
    return 0


def _micro(grades: list[Any]) -> dict[str, Any]:
    predicted = sum(item.predicted_count for item in grades)
    truth = sum(item.ground_truth_count for item in grades)
    true_positive = sum(item.true_positive for item in grades)
    precision = true_positive / predicted if predicted else 0.0
    recall = true_positive / truth if truth else 0.0
    return {
        "true_positive": true_positive,
        "predicted": predicted,
        "ground_truth": truth,
        "precision": precision,
        "recall": recall,
        "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
    }


def run_official() -> int:
    manifest = _validate_manifest()
    smoke = json.loads(SMOKE_RESULT.read_text(encoding="utf-8")) if SMOKE_RESULT.is_file() else {}
    if smoke.get("terminal") not in {"SUBMIT", "STOP"}:
        raise RuntimeError("E9 Smoke-001 PASS is required before official execution")
    identity = _validate_live_agent()
    dataset = ITBenchLiteDataset.open(DATA_ROOT)
    if tuple(item.scenario_id for item in dataset.scenarios()) != ITBENCH_SCENARIO_IDS:
        raise RuntimeError("E9 dataset order mismatch")
    ledger = _ledger(OFFICIAL_LEDGER, len(ITBENCH_SCENARIO_IDS) * LIMITS.max_model_calls)
    budget = LiveModelBudget(ledger["cap"], ledger_path=str(OFFICIAL_LEDGER))
    completed_ids = {
        path.parents[2].name
        for path in OFFICIAL_ROOT.glob("Scenario-*/1/outputs/agent_output.json")
    }
    pending = [item for item in ITBENCH_SCENARIO_IDS if item not in completed_ids]
    if not completed_ids:
        if ledger.get("calls_used", 0) != 0 or ledger.get("cap") != 420:
            raise RuntimeError("E9 official ledger must start fresh at cap 420")
        budget.ensure_capacity(len(ITBENCH_SCENARIO_IDS) * LIMITS.max_model_calls)
    elif pending:
        budget.ensure_capacity(len(pending) * LIMITS.max_model_calls)
    provider = OpenAIProvider(budget=budget, max_retry=0)
    store = ITBenchRunStore(OFFICIAL_ROOT, execution_id=EXECUTION)
    checkpoints: list[dict[str, Any]] = []
    for scenario in dataset.scenarios():
        if scenario.scenario_id in completed_ids:
            existing = json.loads(
                (OFFICIAL_ROOT / scenario.scenario_id / "1" / "trial_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            checkpoints.append(existing)
            continue
        started = time.monotonic()
        before = provider.accounting_snapshot()
        backend = ITBenchSnapshotBackend(dataset, scenario)
        incident, alerts = build_observable_incident(backend)
        result = _runtime(provider, backend).run(incident, alerts)
        output = adapt_e9_output(result)
        _persist_case(OFFICIAL_ROOT / scenario.scenario_id / "1", result, output, store=store)
        grade = grade_root_cause_entities(output, dataset.load_ground_truth(scenario.scenario_id))
        grade_hash = atomic_json_write(
            OFFICIAL_ROOT / scenario.scenario_id / "1" / "grade.json", grade.model_dump(mode="json")
        )
        after = provider.accounting_snapshot()
        checkpoint = {
            "scenario_id": scenario.scenario_id,
            "terminal": result["terminal"],
            "model_calls": result["usage"].get("model_calls", 0),
            "input_tokens": result["usage"].get("input_tokens", 0),
            "output_tokens": result["usage"].get("output_tokens", 0),
            "semantic_actions": result["usage"].get("semantic_actions_executed", 0),
            "predicted_entities": len(output.contributing_factor),
            "true_positive": grade.true_positive,
            "precision": grade.precision,
            "recall": grade.recall,
            "f1": grade.f1,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "provider_outbound_delta": after.outbound_api_attempts - before.outbound_api_attempts,
            "runtime_source_sha": manifest["runtime_source_sha"],
            "grade_sha256": grade_hash,
        }
        atomic_json_write(
            OFFICIAL_ROOT / scenario.scenario_id / "1" / "trial_manifest.json",
            {
                "execution": EXECUTION,
                "experiment": EXPERIMENT,
                "trial": 1,
                "runtime_source_sha": manifest["runtime_source_sha"],
                "model": identity["model"],
                "reasoning": identity["reasoning"],
                "protocol": manifest["protocol"],
                "prompt_version": manifest["prompt_version"],
                "prompt_sha256": manifest["prompt_sha256"],
                **checkpoint,
            },
        )
        checkpoints.append(checkpoint)
        atomic_json_write(
            OFFICIAL_PARTIAL,
            {
                "execution": EXECUTION,
                "completed_scenarios": checkpoints,
                "next_expected": next(
                    (
                        item
                        for item in ITBENCH_SCENARIO_IDS
                        if item not in {x["scenario_id"] for x in checkpoints}
                    ),
                    None,
                ),
                "ledger": _ledger(
                    OFFICIAL_LEDGER, len(ITBENCH_SCENARIO_IDS) * LIMITS.max_model_calls
                ),
            },
        )
        print(f"completed {scenario.scenario_id}", flush=True)
    if [item["scenario_id"] for item in checkpoints] != list(ITBENCH_SCENARIO_IDS):
        raise RuntimeError("E9 official scenario order/completeness mismatch")
    from packages.evals.itbench.grader import ITBenchEntityGrade

    grade_models = [
        ITBenchEntityGrade.model_validate_json(
            (OFFICIAL_ROOT / item["scenario_id"] / "1" / "grade.json").read_bytes()
        )
        for item in checkpoints
    ]
    final = budget.snapshot()
    summary = {
        "execution": EXECUTION,
        "experiment": EXPERIMENT,
        "runtime_source_sha": manifest["runtime_source_sha"],
        "scenario_count": 35,
        "scenario_order": list(ITBENCH_SCENARIO_IDS),
        "scenarios": checkpoints,
        "local_fixed_35_macro": macro_average(grade_models),
        "local_fixed_35_micro": _micro(grade_models),
        "diagnosis_coverage": sum(item["terminal"] == "SUBMIT" for item in checkpoints) / 35,
        "ledger": {
            "cap": final.limit,
            "consumed": final.calls_used,
            "remaining": final.calls_remaining,
        },
        "official_judge": "DEFERRED_UNTIL_PREDICTIONS_FROZEN",
        "safety": {
            "ground_truth_exposure": 0,
            "cross_scenario_evidence": 0,
            "writes": 0,
            "arbitrary_execution": 0,
        },
    }
    atomic_json_write(OFFICIAL_RESULT, summary)
    print(
        json.dumps({"status": "ITB_E9_AGENT_COMPLETE", "ledger": summary["ledger"]}, sort_keys=True)
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
                    "status": "ITB_E9_INVALIDATED_BY_INTEGRATION_FAILURE",
                    "error_type": type(error).__name__,
                    "error": str(error)[:500],
                }
            ),
            file=sys.stderr,
        )
        raise

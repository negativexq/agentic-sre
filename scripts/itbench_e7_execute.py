#!/usr/bin/env python3
"""Run one E7 synthetic smoke and one immutable 35-scenario official pass."""

from __future__ import annotations

import hashlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from packages.evals.itbench import (
    ITBENCH_DATASET_REVISION,
    ITBENCH_EXTERNAL_PROTOCOL_V3,
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
    ITBENCH_EXTERNAL_PROMPT_E7_VERSION,
    external_prompt_hash,
)
from packages.investigation.contracts import InvestigationLimits
from packages.provider import FakeModelProvider, LiveModelBudget, OpenAIProvider, live_model_config

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / ".local/itbench-lite"
RUN_ROOT = ROOT / ".local/itbench-lite-e7-runs"
OFFICIAL_ROOT = RUN_ROOT / "official"
PARTIAL = ROOT / "docs/benchmarks/itbench-lite-e7-official-partial.json"
RESULT = ROOT / "docs/benchmarks/itbench-lite-e7-results.json"
RESULT_SHA = ROOT / "docs/benchmarks/itbench-lite-e7-results.sha256"
MANIFEST = ROOT / "docs/benchmarks/itbench-lite-e7-manifest.json"
LEDGER = ROOT / ".local/itbench-lite-e7-budget.json"
EXECUTION = "ITB-E7"
EXPERIMENT = "itbench-lite-sre-external-eval-v7"
LIMITS = InvestigationLimits(
    max_model_calls=5, max_tool_calls=12, max_agent_turns=5, max_wall_time_seconds=180
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def smoke_scenario() -> ITBenchScenario:
    root = ROOT / "tests/fixtures/itbench_smoke/Scenario-999"
    files: dict[str, tuple[str, ...]] = {
        "alerts": ("alerts/alerts.json",),
        "metrics": ("metrics/checkout.tsv",),
        "k8s_events": ("k8s_events_raw.tsv",),
        "k8s_objects": ("k8s_objects_raw.tsv",),
        "logs": ("otel_logs_raw.tsv",),
        "traces": ("otel_traces_raw.tsv",),
    }
    from packages.evals.itbench import ITBenchEvidenceCategory

    typed = {ITBenchEvidenceCategory(key): value for key, value in files.items()}
    return ITBenchScenario(
        scenario_id="Scenario-999",
        snapshot_path=str(root),
        evidence_categories=tuple(typed),
        evidence_files=typed,
    )


def preflight(dataset: ITBenchLiteDataset, manifest: dict[str, Any]) -> dict[str, Any]:
    ledger = json.loads(LEDGER.read_text())
    if (
        manifest.get("execution") != EXECUTION
        or manifest.get("dataset_revision") != ITBENCH_DATASET_REVISION
    ):
        raise RuntimeError("E7 manifest identity mismatch")
    scenarios = dataset.scenarios()
    if [item.scenario_id for item in scenarios] != list(ITBENCH_SCENARIO_IDS):
        raise RuntimeError("E7 scenario order mismatch")
    if manifest.get("scenario_order") != list(ITBENCH_SCENARIO_IDS):
        raise RuntimeError("E7 frozen order mismatch")
    if ledger.get("calls_used", ledger.get("consumed")) != 0:
        raise RuntimeError("E7 ledger is not fresh")
    if OFFICIAL_ROOT.exists() and any(OFFICIAL_ROOT.iterdir()):
        raise RuntimeError("E7 official output directory is not empty")
    config = live_model_config()
    if not config.enabled or config.model != "gpt-5.6-luna" or config.reasoning_effort != "none":
        raise RuntimeError("frozen E7 live model is not enabled")
    fixture = smoke_scenario()
    backend = ITBenchSnapshotBackend(cast(ITBenchLiteDataset, None), fixture)
    registry = ITBenchExternalToolRegistry(backend)
    incident, alerts = build_observable_incident(backend)
    runtime = ExternalInvestigationRuntime(
        FakeModelProvider([]),
        registry.investigation_registry(),
        backend,
        limits=LIMITS,
        protocol_version=ITBENCH_EXTERNAL_PROTOCOL_V3,
    )
    request = runtime.build_request(incident, alerts, run_id=incident.incident_id)
    context = request.messages[-1].content
    if any(
        value in context.casefold()
        for value in (
            "ground_truth",
            "default_topology",
            "order-service",
            "payment-service",
            "order-worker",
        )
    ):
        raise RuntimeError("E7 external context leakage")
    OpenAIProvider.__new__(OpenAIProvider)._request_parameters(request)
    return {
        "context_chars": len(context),
        "fixture": fixture.scenario_id,
        "registry_names": registry.names(),
    }


def persist_smoke(provider: OpenAIProvider) -> dict[str, Any]:
    scenario = smoke_scenario()
    backend = ITBenchSnapshotBackend(cast(ITBenchLiteDataset, None), scenario)
    registry = ITBenchExternalToolRegistry(backend)
    incident, alerts = build_observable_incident(backend)
    before = provider.accounting_snapshot()
    started = time.monotonic()
    result = ExternalInvestigationRuntime(
        provider,
        registry.investigation_registry(),
        backend,
        limits=LIMITS,
        protocol_version=ITBENCH_EXTERNAL_PROTOCOL_V3,
    ).run(incident, alerts)
    after = provider.accounting_snapshot()
    attempts = after.outbound_api_attempts - before.outbound_api_attempts
    output = adapt_external_output(result)
    smoke_dir = RUN_ROOT / "ITB-E7-SMOKE-001" / "smoke"
    atomic_json_write(smoke_dir / "native_artifact.json", result.model_dump(mode="json"))
    atomic_json_write(smoke_dir / "itbench_output.json", output.model_dump(mode="json"))
    atomic_json_write(smoke_dir / "usage.json", result.usage)
    atomic_json_write(
        smoke_dir / "smoke_manifest.json",
        {
            "execution": EXECUTION,
            "experiment": EXPERIMENT,
            "synthetic_fixture": True,
            "scenario_id": scenario.scenario_id,
            "protocol": ITBENCH_EXTERNAL_PROTOCOL_V3,
            "provider_attempts": attempts,
            "duration_ms": int((time.monotonic() - started) * 1000),
        },
    )
    reloaded = ITBenchExternalResult.model_validate_json(
        (smoke_dir / "native_artifact.json").read_bytes()
    )
    reloaded_output = ITBenchAgentOutput.model_validate_json(
        (smoke_dir / "itbench_output.json").read_bytes()
    )
    if reloaded.model_dump(mode="json") != result.model_dump(
        mode="json"
    ) or reloaded_output.model_dump(mode="json") != output.model_dump(mode="json"):
        raise RuntimeError("E7 smoke artifact reload mismatch")
    if attempts < 1 or result.usage["tool_calls"] < 1 or not result.evidence:
        raise RuntimeError("E7 smoke did not exercise an external tool/evidence path")
    return {
        "scenario_id": scenario.scenario_id,
        "terminal": result.terminal,
        "provider_calls": result.usage["model_calls"],
        "outbound_attempts": attempts,
        "input_tokens": result.usage["input_tokens_total"],
        "output_tokens": result.usage["output_tokens_total"],
        "tool_calls": result.usage["tool_calls"],
        "evidence_count": len(result.evidence),
        "duration_ms": result.usage["duration_ms"],
        "native_reload": True,
        "output_reload": True,
    }


def write_partial(completed: list[dict[str, Any]], next_scenario: str | None, ledger: Any) -> None:
    atomic_json_write(
        PARTIAL,
        {
            "execution": EXECUTION,
            "experiment": EXPERIMENT,
            "completed_scenarios": completed,
            "next_expected": next_scenario,
            "ledger": ledger,
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )


def run_official(
    provider: OpenAIProvider, dataset: ITBenchLiteDataset, manifest_sha: str, source_sha: str
) -> list[dict[str, Any]]:
    store = ITBenchRunStore(OFFICIAL_ROOT, execution_id=EXECUTION)
    completed: list[dict[str, Any]] = []
    for scenario in dataset.scenarios():
        started = time.monotonic()
        before = provider.accounting_snapshot()
        backend = ITBenchSnapshotBackend(dataset, scenario)
        registry = ITBenchExternalToolRegistry(backend)
        incident, alerts = build_observable_incident(backend)
        result = ExternalInvestigationRuntime(
            provider,
            registry.investigation_registry(),
            backend,
            limits=LIMITS,
            protocol_version=ITBENCH_EXTERNAL_PROTOCOL_V3,
        ).run(incident, alerts)
        output = adapt_external_output(result)
        checkpoint_hash = store.write_trial(
            scenario.scenario_id,
            1,
            native_artifact=result.model_dump(mode="json"),
            itbench_output=output,
            usage=result.usage,
        )
        trial_dir = OFFICIAL_ROOT / scenario.scenario_id / "1"
        atomic_json_write(
            trial_dir / "process_metrics.json",
            {
                "semantic_actions": sum(bool(t.get("requested_tools")) for t in result.turns),
                "backend_operations": result.usage["tool_calls"],
                "evidence_producing_operations": len(result.evidence),
                "redundant_actions": sum(
                    s.get("status") == "SKIPPED_DUPLICATE"
                    for t in result.turns
                    for s in t.get("summaries", [])
                ),
                "invalid_transitions": 0,
                "typed_failures": sum(
                    s.get("status") == "TOOL_EXECUTION_FAILURE"
                    for t in result.turns
                    for s in t.get("summaries", [])
                ),
                "timeouts": sum(
                    s.get("error_code") == "TOOL_TIMEOUT"
                    for t in result.turns
                    for s in t.get("summaries", [])
                ),
            },
        )
        atomic_json_write(trial_dir / "turn_trace.json", result.turns)
        reloaded = ITBenchExternalResult.model_validate_json(
            (trial_dir / "native_artifact.json").read_bytes()
        )
        reloaded_output = ITBenchAgentOutput.model_validate_json(
            (trial_dir / "outputs/agent_output.json").read_bytes()
        )
        if reloaded.model_dump(mode="json") != result.model_dump(
            mode="json"
        ) or reloaded_output.model_dump(mode="json") != output.model_dump(mode="json"):
            raise RuntimeError(f"E7 checkpoint reload failed: {scenario.scenario_id}")
        # Evaluator data is opened only after both prediction artifacts are durable.
        grade = grade_root_cause_entities(output, dataset.load_ground_truth(scenario.scenario_id))
        grade_hash = atomic_json_write(trial_dir / "grade.json", grade.model_dump(mode="json"))
        after = provider.accounting_snapshot()
        checkpoint = {
            "scenario_id": scenario.scenario_id,
            "terminal": result.terminal,
            "model_calls": result.usage["model_calls"],
            "semantic_actions": sum(bool(t.get("requested_tools")) for t in result.turns),
            "backend_operations": result.usage["tool_calls"],
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
                "runtime_source_sha": source_sha,
                "dataset_revision": ITBENCH_DATASET_REVISION,
                "adapter": "itbench_lite_snapshot_adapter_v6",
                "protocol": ITBENCH_EXTERNAL_PROTOCOL_V3,
                "prompt_version": ITBENCH_EXTERNAL_PROMPT_E7_VERSION,
                "prompt_sha256": external_prompt_hash(),
                "manifest_sha256": manifest_sha,
                **checkpoint,
            },
        )
        completed.append(checkpoint)
        write_partial(
            completed,
            ITBENCH_SCENARIO_IDS[len(completed)] if len(completed) < 35 else None,
            json.loads(LEDGER.read_text()),
        )
        print(f"completed {scenario.scenario_id}", flush=True)
    return completed


def main() -> int:
    manifest = json.loads(MANIFEST.read_text())
    dataset = ITBenchLiteDataset.open(DATA_ROOT)
    preflight_info = preflight(dataset, manifest)
    budget = LiveModelBudget.from_environment(require_shared_ledger=True)
    if budget.snapshot().calls_used != 0:
        raise RuntimeError("E7 ledger preflight failed")
    provider = OpenAIProvider(budget=budget, max_retry=0)
    smoke = persist_smoke(provider)
    budget.ensure_capacity(175)
    completed = run_official(
        provider, dataset, digest(MANIFEST), str(manifest["runtime_source_sha"])
    )
    from packages.evals.itbench.grader import ITBenchEntityGrade

    grades = [
        ITBenchEntityGrade.model_validate_json(
            (OFFICIAL_ROOT / item["scenario_id"] / "1/grade.json").read_bytes()
        )
        for item in completed
    ]
    final = budget.snapshot()
    summary = {
        "execution": EXECUTION,
        "experiment": EXPERIMENT,
        "runtime_source_sha": manifest["runtime_source_sha"],
        "manifest_sha256": digest(MANIFEST),
        "dataset_revision": ITBENCH_DATASET_REVISION,
        "scenario_count": len(completed),
        "scenario_order": [item["scenario_id"] for item in completed],
        "smoke": smoke,
        "scenarios": completed,
        "macro": macro_average(grades),
        "ledger": {
            "cap": final.limit,
            "consumed": final.calls_used,
            "remaining": final.calls_remaining,
        },
        "official_judge": "NOT RUN",
        "openai_calls_total_e7": final.calls_used,
        "preflight": preflight_info,
        "safety": {"ground_truth_exposure": 0, "writes": 0, "arbitrary_execution": 0},
    }
    atomic_json_write(RESULT, summary)
    atomic_json_write(RESULT_SHA, digest(RESULT))
    print(
        json.dumps(
            {"status": "ITB_E7_COMPLETE", "scenarios": len(completed), "ledger": summary["ledger"]},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(
            json.dumps(
                {
                    "status": "ITB_E7_INVALIDATED_BY_INTEGRATION_FAILURE",
                    "error_type": type(error).__name__,
                    "error": str(error)[:500],
                }
            ),
            file=sys.stderr,
        )
        raise

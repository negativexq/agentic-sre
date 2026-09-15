#!/usr/bin/env python3
"""Execute the frozen E5 synthetic smoke followed by one official 35x1 run."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

from packages.evals.itbench import (
    ITBENCH_DATASET_REVISION,
    ITBENCH_SCENARIO_IDS,
    ExternalInvestigationRuntime,
    ITBenchAgentOutput,
    ITBenchEvidenceCategory,
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
from packages.evals.itbench.external_contracts import (
    ITBENCH_EXTERNAL_PROTOCOL_V2,
    ITBenchExternalResult,
)
from packages.evals.itbench.external_runtime import (
    ITBENCH_EXTERNAL_PROMPT_ACTIVE_VERSION,
    external_prompt_hash,
)
from packages.investigation.contracts import InvestigationLimits
from packages.provider import FakeModelProvider, LiveModelBudget, OpenAIProvider, live_model_config

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / ".local" / "itbench-lite"
RUN_ROOT = ROOT / ".local" / "itbench-lite-e5-runs"
PARTIAL = ROOT / "docs/benchmarks/itbench-lite-e5-official-partial.json"
RESULT = ROOT / "docs/benchmarks/itbench-lite-e5-results.json"
RESULT_SHA = ROOT / "docs/benchmarks/itbench-lite-e5-results.sha256"
MANIFEST = ROOT / "docs/benchmarks/itbench-lite-e5-manifest.json"
LEDGER = ROOT / ".local/itbench-lite-e5-budget.json"
EXECUTION = "ITB-E5"
EXPERIMENT = "itbench-lite-sre-external-eval-v5"
LIMITS = InvestigationLimits(
    max_model_calls=5, max_tool_calls=12, max_agent_turns=5, max_wall_time_seconds=180
)


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def smoke_scenario() -> ITBenchScenario:
    root = ROOT / "tests/fixtures/itbench_smoke/Scenario-999"
    files: dict[str, tuple[str, ...]] = {
        # The fixture is repository-owned and contains no evaluator file.
        "alerts": ("alerts/alerts.json",),
        "metrics": ("metrics/checkout.tsv",),
        "k8s_events": ("k8s_events_raw.tsv",),
        "k8s_objects": ("k8s_objects_raw.tsv",),
        "logs": ("otel_logs_raw.tsv",),
        "traces": ("otel_traces_raw.tsv",),
    }
    typed = {ITBenchEvidenceCategory(key): value for key, value in files.items()}
    return ITBenchScenario(
        scenario_id="Scenario-999",
        snapshot_path=str(root),
        evidence_categories=tuple(typed),
        evidence_files=typed,
    )


def preflight(dataset: ITBenchLiteDataset) -> dict[str, Any]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    snapshot = json.loads(LEDGER.read_text(encoding="utf-8"))
    expected = [item.scenario_id for item in dataset.scenarios()]
    if (
        manifest.get("execution") != EXECUTION
        or manifest.get("dataset_revision") != ITBENCH_DATASET_REVISION
    ):
        raise RuntimeError("E5 manifest identity mismatch")
    if expected != list(ITBENCH_SCENARIO_IDS) or manifest.get("scenario_order") != expected:
        raise RuntimeError("E5 scenario order mismatch")
    if snapshot.get("calls_used", snapshot.get("consumed")) != 0:
        raise RuntimeError("E5 ledger is not at 0 before execution")
    if RUN_ROOT.exists() and any(RUN_ROOT.iterdir()):
        raise RuntimeError("E5 output directory is not empty; refusing rerun")
    config = live_model_config()
    if not config.enabled or config.model != "gpt-5.6-luna" or config.reasoning_effort != "none":
        raise RuntimeError("frozen live model configuration is not enabled")
    fixture = smoke_scenario()
    backend = ITBenchSnapshotBackend(dataset, fixture)
    registry = ITBenchExternalToolRegistry(backend)
    incident, alerts = build_observable_incident(backend)
    runtime = ExternalInvestigationRuntime(
        FakeModelProvider([]),
        registry.investigation_registry(),
        backend,
        limits=LIMITS,
        protocol_version=ITBENCH_EXTERNAL_PROTOCOL_V2,
    )
    request = runtime.build_request(incident, alerts, run_id=incident.incident_id)
    payload = json.dumps(request.messages[-1].content)
    if any(
        word in payload.casefold()
        for word in (
            "ground_truth",
            "default_topology",
            "order-service",
            "payment-service",
            "order-worker",
        )
    ):
        raise RuntimeError("external preflight leakage")
    OpenAIProvider.__new__(OpenAIProvider)._request_parameters(request)
    if not fixture.snapshot_path or not fixture.evidence_files:
        raise RuntimeError("synthetic fixture invalid")
    return {"context_chars": len(request.messages[-1].content), "fixture": fixture.scenario_id}


def run_smoke(provider: OpenAIProvider) -> dict[str, Any]:
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
        protocol_version=ITBENCH_EXTERNAL_PROTOCOL_V2,
    ).run(incident, alerts)
    after = provider.accounting_snapshot()
    attempts = after.outbound_api_attempts - before.outbound_api_attempts
    if not 1 <= attempts <= 5:
        raise RuntimeError("synthetic smoke used an invalid number of provider attempts")
    output = adapt_external_output(result)
    smoke_dir = RUN_ROOT / "ITB-E5-SMOKE-001" / "smoke"
    atomic_json_write(smoke_dir / "native_artifact.json", result.model_dump(mode="json"))
    atomic_json_write(smoke_dir / "itbench_output.json", output.model_dump(mode="json"))
    atomic_json_write(smoke_dir / "usage.json", result.usage)
    manifest = {
        "execution": EXECUTION,
        "experiment": EXPERIMENT,
        "synthetic_fixture": True,
        "scenario_id": scenario.scenario_id,
        "protocol": ITBENCH_EXTERNAL_PROTOCOL_V2,
        "source_revision": ITBENCH_DATASET_REVISION,
        "provider_attempts": attempts,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
    atomic_json_write(smoke_dir / "smoke_manifest.json", manifest)
    reloaded = ITBenchExternalResult.model_validate_json(
        (smoke_dir / "native_artifact.json").read_bytes()
    )
    if reloaded.model_dump(mode="json") != result.model_dump(mode="json"):
        raise RuntimeError("smoke native artifact reload mismatch")
    reloaded_output = ITBenchAgentOutput.model_validate_json(
        (smoke_dir / "itbench_output.json").read_bytes()
    )
    if reloaded_output.model_dump(mode="json") != output.model_dump(mode="json"):
        raise RuntimeError("smoke ITBench output reload mismatch")
    return {
        "scenario_id": scenario.scenario_id,
        "terminal": result.terminal,
        "provider_calls": result.usage["model_calls"],
        "outbound_attempts": attempts,
        "input_tokens": result.usage["input_tokens_total"],
        "output_tokens": result.usage["output_tokens_total"],
        "tool_calls": result.usage["tool_calls"],
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
    store = ITBenchRunStore(RUN_ROOT / "official", execution_id=EXECUTION)
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
            protocol_version=ITBENCH_EXTERNAL_PROTOCOL_V2,
        ).run(incident, alerts)
        output = adapt_external_output(result)
        trial_hash = store.write_trial(
            scenario.scenario_id,
            1,
            native_artifact=result.model_dump(mode="json"),
            itbench_output=output,
            usage=result.usage,
        )
        trial_dir = RUN_ROOT / "official" / scenario.scenario_id / "1"
        reloaded = ITBenchExternalResult.model_validate_json(
            (trial_dir / "native_artifact.json").read_bytes()
        )
        reloaded_output = ITBenchAgentOutput.model_validate_json(
            (trial_dir / "outputs/agent_output.json").read_bytes()
        )
        if reloaded.model_dump(mode="json") != result.model_dump(
            mode="json"
        ) or reloaded_output.model_dump(mode="json") != output.model_dump(mode="json"):
            raise RuntimeError(f"checkpoint reload failed: {scenario.scenario_id}")
        # The evaluator boundary begins only after the prediction is durable.
        ground_truth = dataset.load_ground_truth(scenario.scenario_id)
        grade = grade_root_cause_entities(output, ground_truth)
        grade_hash = atomic_json_write(trial_dir / "grade.json", grade.model_dump(mode="json"))
        after = provider.accounting_snapshot()
        checkpoint = {
            "scenario_id": scenario.scenario_id,
            "terminal": result.terminal,
            "model_calls": result.usage["model_calls"],
            "tool_calls": result.usage["tool_calls"],
            "predicted_entities": len(output.contributing_factor),
            "true_positive": grade.true_positive,
            "precision": grade.precision,
            "recall": grade.recall,
            "f1": grade.f1,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "provider_outbound_delta": after.outbound_api_attempts - before.outbound_api_attempts,
            "checkpoint_sha256": trial_hash,
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
                "protocol": ITBENCH_EXTERNAL_PROTOCOL_V2,
                "adapter": "itbench_lite_snapshot_adapter_v3",
                "prompt_version": ITBENCH_EXTERNAL_PROMPT_ACTIVE_VERSION,
                "prompt_sha256": external_prompt_hash(),
                "manifest_sha256": manifest_sha,
                **checkpoint,
            },
        )
        completed.append(checkpoint)
        ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
        write_partial(
            completed, ITBENCH_SCENARIO_IDS[len(completed)] if len(completed) < 35 else None, ledger
        )
        print(f"completed {scenario.scenario_id}", flush=True)
    return completed


def main() -> int:
    dataset = ITBenchLiteDataset.open(DATA_ROOT)
    preflight_info = preflight(dataset)
    budget = LiveModelBudget.from_environment(require_shared_ledger=True)
    if budget.snapshot().calls_used != 0:
        raise RuntimeError("E5 ledger preflight failed")
    provider = OpenAIProvider(budget=budget, max_retry=0)
    smoke = run_smoke(provider)
    manifest_sha = digest(MANIFEST)
    source_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    completed = run_official(provider, dataset, manifest_sha, source_sha)
    grades = []
    for item in completed:
        grade_path = RUN_ROOT / "official" / item["scenario_id"] / "1" / "grade.json"
        from packages.evals.itbench.grader import ITBenchEntityGrade

        grades.append(ITBenchEntityGrade.model_validate_json(grade_path.read_bytes()))
    final_budget = budget.snapshot()
    summary = {
        "execution": EXECUTION,
        "experiment": EXPERIMENT,
        "runtime_source_sha": source_sha,
        "manifest_sha256": manifest_sha,
        "dataset_revision": ITBENCH_DATASET_REVISION,
        "scenario_count": len(completed),
        "scenario_order": [x["scenario_id"] for x in completed],
        "smoke": smoke,
        "scenarios": completed,
        "macro": macro_average(grades),
        "ledger": {
            "cap": final_budget.limit,
            "consumed": final_budget.calls_used,
            "remaining": final_budget.calls_remaining,
        },
        "official_judge": "NOT RUN",
        "openai_calls_total_e5": final_budget.calls_used,
        "preflight": preflight_info,
        "safety": {"ground_truth_exposure": 0, "writes": 0, "arbitrary_execution": 0},
    }
    atomic_json_write(RESULT, summary)
    RESULT_SHA.write_text(digest(RESULT) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"status": "ITB_E5_COMPLETE", "scenarios": len(completed), "ledger": summary["ledger"]},
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
                    "status": "ITB_E5_INVALIDATED_BY_INTEGRATION_FAILURE",
                    "error_type": type(error).__name__,
                    "error": str(error)[:500],
                }
            ),
            file=sys.stderr,
        )
        raise

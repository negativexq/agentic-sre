#!/usr/bin/env python3
"""Run one synthetic ITBench-Lite integration smoke, never an official case."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, cast

from packages.evals.itbench import (
    ITBENCH_DATASET_REVISION,
    ITBenchAgentOutput,
    ITBenchEvidenceCategory,
    ITBenchLiteDataset,
    ITBenchRunStore,
    ITBenchScenario,
    ITBenchSnapshotBackend,
    ITBenchSnapshotToolRegistry,
    adapt_a1_output,
    atomic_json_write,
    build_observable_incident,
    entities_from_k8s_records,
)
from packages.investigation import InvestigationLimits, InvestigationRuntime
from packages.investigation.artifacts import A1RunArtifact
from packages.investigation.context import derive_observation_window
from packages.provider import LiveModelBudget, OpenAIProvider, live_model_config

ROOT = Path(__file__).resolve().parents[1]
SMOKE_SCENARIO_ID = "Scenario-999"
SMOKE_FIXTURE_ID = "ITB-SMOKE-001"
SMOKE_FIXTURE = ROOT / "tests" / "fixtures" / "itbench_smoke" / SMOKE_SCENARIO_ID
RUN_ROOT = ROOT / ".local" / "itbench-lite-runs" / SMOKE_FIXTURE_ID / "smoke"
SMOKE_ARTIFACT = ROOT / "docs" / "benchmarks" / "itbench-lite-e1-live-smoke.json"
LEDGER = ROOT / ".local" / "itbench-lite-e1-budget.json"

_FILES: dict[ITBenchEvidenceCategory, tuple[str, ...]] = {
    ITBenchEvidenceCategory.ALERTS: ("alerts/alerts.json",),
    ITBenchEvidenceCategory.METRICS: ("metrics/checkout.tsv",),
    ITBenchEvidenceCategory.K8S_EVENTS: ("k8s_events_raw.tsv",),
    ITBenchEvidenceCategory.K8S_OBJECTS: ("k8s_objects_raw.tsv",),
    ITBenchEvidenceCategory.LOGS: ("otel_logs_raw.tsv",),
    ITBenchEvidenceCategory.TRACES: ("otel_traces_raw.tsv",),
}


def _scenario() -> ITBenchScenario:
    return ITBenchScenario(
        scenario_id=SMOKE_SCENARIO_ID,
        snapshot_path=str(SMOKE_FIXTURE),
        evidence_categories=tuple(ITBenchEvidenceCategory),
        evidence_files=_FILES,
        observation_start="2025-12-15T17:25:19Z",
        observation_end="2025-12-15T17:25:21Z",
    )


def _assert_preflight(registry: ITBenchSnapshotToolRegistry, incident: Any) -> None:
    """Fail closed before provider construction if the synthetic boundary is wrong."""
    if not SMOKE_FIXTURE.is_dir() or not incident:
        raise RuntimeError("synthetic snapshot or observable incident is missing")
    context = registry.public_context()
    forbidden = ("ground_truth", "DO_NOT_LEAK", "evaluator", "fault_metadata")
    if any(item.casefold() in context.casefold() for item in forbidden):
        raise RuntimeError("ground-truth or evaluator data leaked into investigator context")
    runtime_registry = registry.investigation_registry()
    forbidden_names = {"shell", "bash", "python", "sql", "ground_truth", "evaluator"}
    if forbidden_names.intersection(name.casefold() for name in runtime_registry.names()):
        raise RuntimeError("forbidden external investigation tool is registered")
    for category, name in (
        (ITBenchEvidenceCategory.ALERTS, "itbench_alerts"),
        (ITBenchEvidenceCategory.METRICS, "itbench_metrics"),
        (ITBenchEvidenceCategory.K8S_EVENTS, "itbench_kubernetes_events"),
        (ITBenchEvidenceCategory.K8S_OBJECTS, "itbench_kubernetes_objects"),
        (ITBenchEvidenceCategory.LOGS, "itbench_logs"),
        (ITBenchEvidenceCategory.TRACES, "itbench_traces"),
    ):
        response = registry.invoke(name, {"limit": 5})
        if response.get("category") != category.value or not isinstance(
            response.get("records"), list
        ):
            raise RuntimeError(f"synthetic category preflight failed: {category.value}")
    if RUN_ROOT.exists() or SMOKE_ARTIFACT.exists():
        raise RuntimeError("synthetic smoke output already exists; refusing to overwrite it")
    RUN_ROOT.mkdir(parents=True, exist_ok=False)
    if not os.access(RUN_ROOT, os.W_OK):
        raise RuntimeError("synthetic smoke output directory is not writable")
    if not LEDGER.exists():
        raise RuntimeError(f"external budget ledger is missing: {LEDGER}")


def _turn_accounting(result: Any) -> dict[str, Any]:
    turns = result.turns
    return {
        "turns": turns,
        "requested": sum(int(item.get("requested_tool_count", 0)) for item in turns),
        "valid": sum(int(item.get("tool_calls_succeeded", 0)) for item in turns),
        "invalid": sum(
            int(item.get("tool_calls_failed", 0))
            for item in turns
            if item.get("validation_stage") in {"TOOL_ARGUMENTS", "TOOL_REGISTRY"}
        )
        + sum(
            int(item.get("tool_calls_failed", 0))
            for item in turns
            if item.get("validation_stage") == "TOOL_EXECUTION"
            and any(
                isinstance(summary, dict) and summary.get("error_code") == "INVALID_QUERY"
                for summary in item.get("summaries", [])
            )
        ),
        "duplicate_reused": result.usage.duplicate_requests_suppressed,
        "executions": result.usage.tool_calls,
        "backend_failures": sum(
            int(item.get("tool_calls_failed", 0))
            for item in turns
            if item.get("validation_stage") == "TOOL_EXECUTION"
        ),
    }


def main() -> int:
    """Run one provider-backed synthetic smoke after all safe preflight gates."""
    scenario = _scenario()
    backend = ITBenchSnapshotBackend(
        cast(ITBenchLiteDataset, None), scenario, max_rows=50, max_bytes=100_000
    )
    registry = ITBenchSnapshotToolRegistry(backend)
    incident, alerts = build_observable_incident(backend)
    _assert_preflight(registry, incident)

    budget = LiveModelBudget.from_environment(require_shared_ledger=True)
    before = budget.snapshot()
    if before.limit != 180 or before.calls_used != 0 or before.calls_remaining != 180:
        raise RuntimeError(f"unexpected external ledger preflight: {before}")
    budget.ensure_capacity(5)
    config = live_model_config()
    if not config.enabled or config.model != "gpt-5.6-luna" or config.reasoning_effort != "none":
        raise RuntimeError("frozen external model configuration is not enabled and exact")

    # This is the provider-construction boundary.  No provider is constructed above it.
    provider = OpenAIProvider(budget=budget, max_retry=0)
    try:
        result = InvestigationRuntime(
            provider,
            registry.investigation_registry(),
            model="gpt-5.6-luna",
            reasoning_effort="none",
            limits=InvestigationLimits(
                max_model_calls=5,
                max_tool_calls=12,
                max_agent_turns=5,
                max_wall_time_seconds=180,
            ),
            a1_protocol=True,
        ).run(incident, alerts=alerts)
        after = budget.snapshot()
        attempts = result.usage.outbound_api_attempts
        if not 1 <= attempts <= 5 or after.calls_used != attempts:
            raise RuntimeError("external ledger and provider accounting do not reconcile")

        observation_window = derive_observation_window(incident, alerts, now=incident.updated_at)
        artifact = A1RunArtifact.from_result(
            result,
            experiment_id="ITB-E1",
            observation_window=observation_window.time_window(),
            configuration_hashes={
                "dataset_revision": ITBENCH_DATASET_REVISION,
                "adapter": "itbench_lite_snapshot_adapter_v2",
                "smoke_fixture": SMOKE_FIXTURE_ID,
            },
        )
        artifact_json = artifact.model_dump_json()
        reloaded_artifact = A1RunArtifact.from_json(artifact_json)
        if artifact.model_dump(mode="json") != reloaded_artifact.model_dump(mode="json"):
            raise RuntimeError("native artifact semantic round-trip failed")

        native_output = result.model_dump(mode="json")
        observed_entities = entities_from_k8s_records(
            backend.complete_source_records(ITBenchEvidenceCategory.K8S_OBJECTS)
        )
        external_output = adapt_a1_output(
            scenario_id=SMOKE_SCENARIO_ID,
            incident_id=str(incident.incident_id),
            native_output=native_output,
            observed_entities=observed_entities,
        )
        store = ITBenchRunStore(RUN_ROOT, execution_id="ITB-E1")
        checkpoint_sha = store.write_trial(
            SMOKE_SCENARIO_ID,
            1,
            native_artifact=artifact.model_dump(mode="json"),
            itbench_output=external_output,
            usage=result.usage.model_dump(mode="json"),
        )
        stored = store.read_trial(SMOKE_SCENARIO_ID, 1)
        A1RunArtifact.from_json(
            (RUN_ROOT / SMOKE_SCENARIO_ID / "1" / "native_artifact.json").read_bytes()
        )
        output_path = RUN_ROOT / SMOKE_SCENARIO_ID / "1" / "outputs" / "agent_output.json"
        if not output_path.exists():
            raise RuntimeError("ITBench official-compatible output was not persisted")
        ITBenchAgentOutput.model_validate_json(output_path.read_bytes())

        evidence_ids = {str(item.evidence_id) for item in result.evidence}
        referenced_ids = {
            str(item) for item in (result.causal_hypothesis or {}).get("evidence_ids", [])
        }
        safety = artifact.safety.model_dump(mode="json")
        if any(value != 0 for value in safety.values()) or not referenced_ids.issubset(
            evidence_ids
        ):
            raise RuntimeError("synthetic evidence provenance or safety check failed")
        artifact_sha = atomic_json_write(
            SMOKE_ARTIFACT,
            {
                "purpose": "external integration smoke",
                "excluded_from_external_benchmark_metrics": True,
                "synthetic_fixture": True,
                "execution": "ITB-E1",
                "experiment": "itbench-lite-sre-external-eval-v1",
                "fixture_id": SMOKE_FIXTURE_ID,
                "scenario_id": SMOKE_SCENARIO_ID,
                "incident_id": str(incident.incident_id),
                "dataset_revision": ITBENCH_DATASET_REVISION,
                "adapter": "itbench_lite_snapshot_adapter_v2",
                "model": config.model,
                "reasoning": config.reasoning_effort,
                "limits": {
                    "model_calls": 5,
                    "tool_executions": 12,
                    "turns": 5,
                    "wall_seconds": 180,
                },
                "provider_calls": result.usage.model_calls,
                "outbound_attempts": attempts,
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
                "latency_ms": result.usage.latency_ms,
                "investigation_duration_ms": result.usage.latency_ms,
                "terminal_state": result.termination_reason.value,
                "tool_accounting": _turn_accounting(result),
                "evidence_count": len(result.evidence),
                "evidence_max_summary_chars": max(
                    (len(item.bounded_observation_summary) for item in artifact.evidence), default=0
                ),
                "truncated_summary_count": sum(
                    "...[truncated]" in item.bounded_observation_summary
                    for item in artifact.evidence
                ),
                "canonical_evidence_equality": True,
                "native_artifact": {
                    "construction": True,
                    "serialization": True,
                    "reload": True,
                    "semantic_equality": True,
                },
                "native_artifact_checkpoint_sha256": checkpoint_sha,
                "itbench_output": {"construction": True, "persistence": True, "reload": True},
                "ledger_before": before.calls_used,
                "ledger_after": after.calls_used,
                "ledger_limit": after.limit,
                "ground_truth_isolation": True,
                "safety": safety,
                "official_judge_run": False,
            },
        )
        print(
            json.dumps(
                {
                    "classification": "ITB_E1_SMOKE_PASS",
                    "smoke_artifact": str(SMOKE_ARTIFACT),
                    "smoke_artifact_sha256": artifact_sha,
                    "provider_calls": attempts,
                    "ledger_after": after.calls_used,
                    "terminal": result.termination_reason.value,
                    "official_scenarios": 0,
                    "official_judge": False,
                    "stored_trial": str(RUN_ROOT / SMOKE_SCENARIO_ID / "1"),
                    "stored_trial_payload": stored["scenario_id"],
                },
                sort_keys=True,
            )
        )
        return 0
    except Exception as error:
        print(
            json.dumps(
                {
                    "classification": "ITB_E1_SMOKE_INVALIDATED_BY_INTEGRATION_FAILURE",
                    "provider_started": True,
                    "error_type": type(error).__name__,
                    "error": str(error)[:500],
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

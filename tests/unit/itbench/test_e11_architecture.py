"""Offline invariants for the E11 observability/control architecture."""

from __future__ import annotations

import inspect
import json
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from packages.evals.itbench.contracts import ITBenchEvidenceCategory
from packages.evals.itbench.e11_canary import run_e11_fake_provider_canary
from packages.evals.itbench.e11_context import E11_CONTEXT_MAX_CHARS, build_e11_context
from packages.evals.itbench.e11_control import (
    CandidateStatus,
    E11CaseMemory,
    EvidenceAssessment,
    e11_control_surface,
)
from packages.evals.itbench.e11_observability import (
    ObservedEntityCatalog,
    RankedCandidate,
    RetrievalEvidence,
    build_observed_entity_catalog,
    rank_observed_candidates,
)


class _FakeBackend:
    def __init__(
        self,
        objects: list[dict[str, Any]],
        events: list[dict[str, Any]],
        alerts: list[dict[str, Any]],
        *,
        topology: tuple[dict[str, Any], ...] = (),
        metrics: tuple[dict[str, Any], ...] = (),
        logs: tuple[dict[str, Any], ...] = (),
        traces: tuple[dict[str, Any], ...] = (),
    ) -> None:
        self.scenario = SimpleNamespace(
            scenario_id="offline-adversarial", snapshot_path=".", evidence_files={}
        )
        self._records = {
            ITBenchEvidenceCategory.K8S_OBJECTS: objects,
            ITBenchEvidenceCategory.K8S_EVENTS: events,
            ITBenchEvidenceCategory.ALERTS: alerts,
            ITBenchEvidenceCategory.METRICS: metrics,
            ITBenchEvidenceCategory.LOGS: logs,
            ITBenchEvidenceCategory.TRACES: traces,
        }
        self._topology = topology

    def complete_source_records(
        self, category: ITBenchEvidenceCategory
    ) -> tuple[dict[str, Any], ...]:
        return tuple(self._records[category])

    def topology(self, *, limit: int | None = None, **_kwargs: Any) -> tuple[dict[str, Any], ...]:
        return self._topology


def _object(
    kind: str,
    name: str,
    *,
    namespace: str = "default",
    labels: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body = {
        "kind": kind,
        "metadata": {"name": name, "namespace": namespace, "labels": labels or {}},
    }
    return {"evidence_id": f"object:{kind}:{name}", "record": {"Body": json.dumps(body)}}


def _event(kind: str, name: str, reason: str, *, namespace: str = "default") -> dict[str, Any]:
    body = {
        "object": {
            "kind": "Event",
            "metadata": {"name": f"event-{name}", "namespace": namespace},
            "regarding": {"kind": kind, "name": name, "namespace": namespace},
            "reason": reason,
            "note": reason,
            "type": "Warning",
        }
    }
    return {
        "evidence_id": f"event:{name}",
        "record": {"Body": json.dumps(body), "Timestamp": "2025-01-01T00:00:01Z"},
    }


def _candidate(handle: str, canonical: str, rank: int = 1) -> RankedCandidate:
    return RankedCandidate(
        handle=handle,
        canonical=canonical,
        rank=rank,
        score=1.0,
        family=canonical.rsplit("/", 2)[0] + "/" + canonical.rsplit("/", 2)[1],
        retrieval_evidence=(RetrievalEvidence("FAILURE_EVENT", "fixture", "bounded", 1.0),),
    )


def test_incident_event_beats_unrelated_config_object() -> None:
    backend = _FakeBackend(
        [_object("ConfigMap", "unrelated"), _object("Deployment", "checkout")],
        [_event("Deployment", "checkout", "BackOff")],
        [],
    )
    catalog = build_observed_entity_catalog(backend, include_telemetry=False)
    ranked = rank_observed_candidates(backend, catalog, include_telemetry=False)
    assert ranked[0].canonical == "default/Deployment/checkout"
    assert catalog.get("default/ConfigMap/unrelated") is not None
    deployment = catalog.get("default/Deployment/checkout")
    assert deployment is not None
    assert deployment.provenance == {
        "DIRECT_K8S_OBJECT",
        "DIRECT_K8S_EVENT",
    }


def test_alert_identity_is_exact_not_substring() -> None:
    backend = _FakeBackend(
        [_object("Service", "checkout"), _object("Service", "checkout-worker")],
        [],
        [
            {
                "evidence_id": "alert:1",
                "record": {
                    "labels": {
                        "alertname": "ErrorRate",
                        "service": "checkout",
                        "namespace": "default",
                    }
                },
            }
        ],
    )
    catalog = build_observed_entity_catalog(backend, include_telemetry=False)
    ranked = rank_observed_candidates(backend, catalog, include_telemetry=False)
    assert ranked[0].canonical == "default/Service/checkout"
    assert all(item.canonical != "default/Service/checkout-worker" for item in ranked[:1])


def test_monitoring_centrality_does_not_outrank_incident_failure() -> None:
    monitoring = _object("Deployment", "prometheus")
    workload = _object("Deployment", "checkout")
    topology = tuple(
        {
            "source": "default/Deployment/prometheus",
            "target": f"default/Service/monitor-{index}",
            "relationship": "calls",
        }
        for index in range(8)
    )
    backend = _FakeBackend(
        [monitoring, workload],
        [_event("Deployment", "checkout", "BackOff")],
        [],
        topology=topology,
    )
    catalog = build_observed_entity_catalog(backend, include_telemetry=False)
    ranked = rank_observed_candidates(backend, catalog, include_telemetry=False)
    assert ranked[0].canonical == "default/Deployment/checkout"
    assert all(item.canonical != "default/Deployment/prometheus" for item in ranked[:1])


def test_stale_chaos_object_has_no_priority_without_incident_evidence() -> None:
    backend = _FakeBackend(
        [_object("NetworkChaos", "partition-stale"), _object("Deployment", "checkout")],
        [_event("Deployment", "checkout", "BackOff")],
        [],
    )
    catalog = build_observed_entity_catalog(backend, include_telemetry=False)
    ranked = rank_observed_candidates(backend, catalog, include_telemetry=False)
    assert ranked[0].canonical == "default/Deployment/checkout"
    assert all(item.canonical != "default/NetworkChaos/partition-stale" for item in ranked)


def test_upstream_failure_beats_downstream_error_volume() -> None:
    upstream = _object("Deployment", "payment")
    downstream = _object("Deployment", "frontend")
    downstream_events = [
        _event("Deployment", "frontend", "Failed", namespace="default") for _ in range(10)
    ]
    upstream_event = _event("Deployment", "payment", "OOMKilled", namespace="default")
    upstream_event["record"]["Timestamp"] = "2025-01-01T00:00:01Z"
    for item in downstream_events:
        item["record"]["Timestamp"] = "2025-01-01T00:00:20Z"
    backend = _FakeBackend([upstream, downstream], [upstream_event, *downstream_events], [])
    catalog = build_observed_entity_catalog(backend, include_telemetry=False)
    ranked = rank_observed_candidates(backend, catalog, include_telemetry=False)
    assert ranked[0].canonical == "default/Deployment/payment"


def test_metric_counter_without_diagnostic_signal_is_not_anomaly() -> None:
    metrics = tuple(
        {
            "evidence_id": f"metric:{index}",
            "record": {
                "service": "checkout",
                "metric_name": "http_requests_total",
                "value": str(value),
                "Timestamp": f"2025-01-01T00:00:0{index}Z",
            },
        }
        for index, value in enumerate((1, 100), start=1)
    )
    backend = _FakeBackend([_object("Service", "checkout")], [], [], metrics=metrics)
    telemetry = {ITBenchEvidenceCategory.METRICS: metrics}
    catalog = build_observed_entity_catalog(backend, telemetry_records=telemetry)
    ranked = rank_observed_candidates(backend, catalog, telemetry_records=telemetry)
    assert all(item.canonical != "default/Service/checkout" for item in ranked)


def test_namespace_mapping_is_name_based_and_config_signal_is_causal() -> None:
    namespace = {
        "evidence_id": "object:Namespace:otel-demo",
        "record": {"Body": json.dumps({"kind": "Namespace", "metadata": {"name": "otel-demo"}})},
    }
    topology = (
        {
            "source": "otel-demo/Deployment/checkout",
            "target": "otel-demo/ConfigMap/checkout-config",
            "relationship": "configuration_reference",
        },
    )
    backend = _FakeBackend(
        [
            namespace,
            _object("Deployment", "checkout", namespace="otel-demo"),
            _object("ConfigMap", "checkout-config", namespace="otel-demo"),
        ],
        [_event("Deployment", "checkout", "BackOff", namespace="otel-demo")],
        [],
        topology=topology,
    )
    catalog = build_observed_entity_catalog(backend, include_telemetry=False)
    deployment = catalog.get("otel-demo/Deployment/checkout")
    config = catalog.get("otel-demo/ConfigMap/checkout-config")
    assert deployment is not None and config is not None
    assert (
        deployment.canonical,
        "_cluster/Namespace/otel-demo",
        "namespace_contains",
    ) in deployment.relationships
    ranked = rank_observed_candidates(backend, catalog, include_telemetry=False)
    config_candidate = next(item for item in ranked if item.canonical == config.canonical)
    assert any(item.channel == "CAUSAL_RELATION" for item in config_candidate.retrieval_evidence)


def test_telemetry_identity_is_structured_and_trace_edges_are_directional() -> None:
    traces = (
        {
            "evidence_id": "trace:frontend-payment",
            "record": {
                "ServiceName": "frontend",
                "ResourceAttributes": "{'k8s.namespace.name': 'default'}",
                "SpanAttributes": "{'peer.service': 'payment'}",
                "StatusCode": "ERROR",
                "Timestamp": "2025-01-01T00:00:01Z",
            },
        },
    )
    backend = _FakeBackend([], [], [], traces=traces)
    catalog = build_observed_entity_catalog(
        backend,
        telemetry_records={
            ITBenchEvidenceCategory.METRICS: (),
            ITBenchEvidenceCategory.LOGS: (),
            ITBenchEvidenceCategory.TRACES: traces,
        },
    )
    frontend = catalog.get("default/Service/frontend")
    payment = catalog.get("default/Service/payment")
    assert frontend is not None and payment is not None
    assert (frontend.canonical, payment.canonical, "calls") in frontend.relationships
    assert payment.identity_type == "ServiceIdentity"
    assert payment.provenance == {"TRACE_RESOURCE", "TOPOLOGY_DERIVED"}


def test_observe_first_dynamic_discovery_and_supported_submit() -> None:
    catalog = ObservedEntityCatalog(scenario_id="offline")
    first = catalog.add(
        canonical="default/Deployment/first",
        identity_type="KubernetesEntity",
        namespace="default",
        kind="Deployment",
        name="first",
        source_category="k8s_objects",
        provenance="DIRECT_K8S_OBJECT",
        evidence_ref="object:first",
    )
    initial = (_candidate(first.handle, first.canonical),)
    memory = E11CaseMemory(scenario_id="offline", catalog=catalog)
    memory.initialize(initial)
    surface = e11_control_surface(memory)
    assert surface.actions == ("OBSERVE", "HYPOTHESIZE", "STOP")
    assert "INCIDENT_OVERVIEW" in surface.operations
    memory.observe(initial)
    memory.hypothesize(first.handle)
    evidence = memory.add_evidence(first.handle, "EVENT_ANALYSIS", {"reason": "BackOff"})
    memory.assess(first.handle, evidence, EvidenceAssessment.SUPPORTS, dimension="causal")
    discovered = catalog.discover(
        [
            {
                "canonical": "default/Service/discovered",
                "identity_type": "ServiceIdentity",
                "namespace": "default",
                "kind": "Service",
                "name": "discovered",
                "evidence_ref": "trace:1",
            }
        ]
    )
    discovered_entity = discovered[0]
    assert discovered_entity.handle != first.handle
    revised = _candidate(discovered_entity.handle, discovered_entity.canonical)
    memory.rerank((revised, initial[0]), evidence)
    memory.revise(discovered_entity.handle)
    first_from_catalog = memory.catalog.by_handle(first.handle)
    assert first_from_catalog is not None
    assert first_from_catalog.canonical == "default/Deployment/first"
    assert memory.ranking_history[-1]["triggering_evidence_ref"] == evidence
    assert memory.submit_ready((discovered_entity.handle,)) is False
    assert memory.submit_ready((first.handle,)) is False
    assert memory.candidate_status[first.handle] == CandidateStatus.SUPPORTED


def test_contradictory_evidence_blocks_submission() -> None:
    catalog = ObservedEntityCatalog(scenario_id="polarity")
    entity = catalog.add(
        canonical="default/Deployment/checkout",
        identity_type="KubernetesEntity",
        namespace="default",
        kind="Deployment",
        name="checkout",
        source_category="k8s_objects",
        provenance="DIRECT_K8S_OBJECT",
        evidence_ref="object:checkout",
    )
    memory = E11CaseMemory(scenario_id="polarity", catalog=catalog)
    candidate = _candidate(entity.handle, entity.canonical)
    memory.initialize((candidate,))
    memory.hypothesize(entity.handle)
    evidence = memory.add_evidence(entity.handle, "EVENT_ANALYSIS", {"reason": "BackOff"})
    memory.assess(entity.handle, evidence, EvidenceAssessment.SUPPORTS, dimension="causal")
    memory.assess(entity.handle, evidence, EvidenceAssessment.CONTRADICTS, dimension="temporal")
    assert memory.candidate_status[entity.handle] == CandidateStatus.CONTRADICTED
    assert not memory.submit_ready((entity.handle,))


def test_compare_replicas_requires_explicit_shared_owner() -> None:
    from packages.evals.itbench.e11_operations import (
        available_e11_operations,
        comparable_peer_count,
    )

    catalog = ObservedEntityCatalog(scenario_id="replicas")
    first = catalog.add(
        canonical="default/Pod/first",
        identity_type="KubernetesEntity",
        namespace="default",
        kind="Pod",
        name="first",
        source_category="k8s_objects",
        provenance="DIRECT_K8S_OBJECT",
        evidence_ref="object:first",
    )
    peer = catalog.add(
        canonical="default/Pod/peer",
        identity_type="KubernetesEntity",
        namespace="default",
        kind="Pod",
        name="peer",
        source_category="k8s_objects",
        provenance="DIRECT_K8S_OBJECT",
        evidence_ref="object:peer",
    )
    owner = catalog.add(
        canonical="default/ReplicaSet/workload",
        identity_type="KubernetesEntity",
        namespace="default",
        kind="ReplicaSet",
        name="workload",
        source_category="k8s_objects",
        provenance="DIRECT_K8S_OBJECT",
        evidence_ref="object:owner",
    )
    catalog.add_relationship(first.canonical, owner.canonical, "owner")
    catalog.add_relationship(peer.canonical, owner.canonical, "owner")
    assert comparable_peer_count(catalog, first) == 1
    assert "COMPARE_REPLICAS" in available_e11_operations(first, comparable_peers=1)


def test_verify_surface_exposes_bounded_alternative_operation_scopes() -> None:
    catalog = ObservedEntityCatalog(scenario_id="alternatives")
    first = catalog.add(
        canonical="default/Service/first",
        identity_type="ServiceIdentity",
        namespace="default",
        kind="Service",
        name="first",
        source_category="logs",
        provenance="LOG_RESOURCE",
        evidence_ref="log:first",
    )
    second = catalog.add(
        canonical="default/Service/second",
        identity_type="ServiceIdentity",
        namespace="default",
        kind="Service",
        name="second",
        source_category="metrics",
        provenance="METRIC_RESOURCE",
        evidence_ref="metric:second",
    )
    candidates = tuple(
        RankedCandidate(
            handle=entity.handle,
            canonical=entity.canonical,
            rank=index,
            score=1.0,
            family="default/Service",
            retrieval_evidence=(),
        )
        for index, entity in enumerate((first, second), start=1)
    )
    memory = E11CaseMemory(scenario_id="alternatives", catalog=catalog)
    memory.initialize(candidates)
    memory.hypothesize(first.handle)
    surface = e11_control_surface(memory)
    assert "LOG_ANALYSIS" in surface.operations
    assert "METRIC_ANOMALIES" in surface.operations
    assert dict(surface.operation_targets)[second.handle] == (
        "ENTITY_CONTEXT",
        "METRIC_ANOMALIES",
    )


def test_context_is_bounded_and_has_budget_state() -> None:
    candidate = _candidate("C001", "default/Deployment/checkout")
    context = build_e11_context(
        incident={"summary": "x" * 100_000, "alerts": ["a"] * 100},
        candidates=(candidate,),
        phase="OBSERVE",
        remaining_model_calls=12,
        remaining_semantic_actions=8,
    )
    assert len(context) <= E11_CONTEXT_MAX_CHARS
    assert "remaining_model_calls" in context


def test_fake_provider_canary_exercises_35_control_paths() -> None:
    scenario_ids = tuple(f"Scenario-{index}" for index in range(1, 36))
    result = run_e11_fake_provider_canary(scenario_ids)
    assert result["completed"] == 35
    assert result["provider_invocations"] == 0
    assert result["ground_truth_access"] == 0
    assert result["stable_handles"] is True


def test_new_e11_modules_have_no_ground_truth_or_provider_dependency() -> None:
    from packages.evals.itbench import e11_control, e11_observability, e11_official

    assert "load_ground_truth" not in inspect.getsource(e11_observability)
    assert not hasattr(e11_control, "OpenAIProvider")
    assert "load_ground_truth" not in inspect.getsource(e11_official)
    assert "OpenAIProvider" not in inspect.getsource(e11_official)


def test_e11_ledger_cap_is_derived_from_frozen_counts() -> None:
    from packages.evals.itbench.e11_official import (
        E11OfficialManifestV1,
        E11RuntimeLimits,
        build_e11_ledger,
    )
    from packages.evals.itbench.live_smoke_contract import ITBenchLiveSmokeRuntimeIdentity

    identity = ITBenchLiveSmokeRuntimeIdentity(
        git_head="offline",
        relevant_paths=["packages/evals/itbench/e11_official.py"],
        relevant_content_sha256={"packages/evals/itbench/e11_official.py": "hash"},
        bundle_sha256="bundle",
        relevant_worktree_dirty=False,
    )
    manifest = E11OfficialManifestV1(
        execution="ITB-E11",
        experiment="itbench-lite-sre-external-eval-v11",
        dataset_revision="revision",
        scenario_order=[f"Scenario-{index}" for index in range(35)],
        scenario_order_hash="order",
        scenario_count=35,
        trial_count=1,
        runtime_identity=identity,
        provider="openai",
        model="gpt-5.6-luna",
        reasoning_effort="none",
        provider_retries=0,
        runtime_limits=E11RuntimeLimits(
            max_model_calls=12,
            max_tool_calls=24,
            max_agent_turns=12,
            max_wall_time_seconds=240,
            max_consecutive_rejected_actions=2,
        ),
        gt_policy="POST_SEAL_ONLY",
        judge_policy="DEFERRED_UNTIL_PREDICTIONS_FROZEN_AND_HUMAN_AUTHORIZED",
        resume_policy="CHECKPOINT_ONLY",
        failure_policy="ABORT_ON_INFRASTRUCTURE_FAILURE",
        prompt_version="prompt",
        prompt_hash="hash",
        protocol_version="v5",
        protocol_hash="hash",
        context_version="context",
        catalog_version="catalog",
        retrieval_version="retrieval",
    )
    assert build_e11_ledger(manifest)["cap"] == 420


def test_e11_seal_requires_and_verifies_all_35_checkpoint_hashes(tmp_path: Path) -> None:
    from packages.evals.itbench.e11_official import (
        E11OfficialManifestV1,
        seal_e11_predictions,
        verify_e11_seal,
    )

    order = [f"Scenario-{index}" for index in range(1, 36)]
    from packages.evals.itbench.live_smoke_contract import ITBenchLiveSmokeRuntimeIdentity

    manifest = E11OfficialManifestV1.model_construct(
        execution="ITB-E11",
        scenario_order=order,
        dataset_revision="offline",
        runtime_identity=ITBenchLiveSmokeRuntimeIdentity.model_construct(
            git_head="offline",
            relevant_paths=["offline"],
            relevant_content_sha256={"offline": "hash"},
            bundle_sha256="bundle",
            relevant_worktree_dirty=False,
        ),
    )
    root = tmp_path / "predictions"
    for scenario_id in order:
        trial = root / scenario_id / "1"
        trial.mkdir(parents=True)
        for name in (
            "native_artifact.json",
            "agent_output.json",
            "turn_trace.json",
            "case_state.json",
            "event_log.json",
            "usage.json",
        ):
            (trial / name).write_text("{}\n", encoding="utf-8")
        hashes = {
            name.removesuffix(".json"): sha256((trial / name).read_bytes()).hexdigest()
            for name in (
                "native_artifact.json",
                "agent_output.json",
                "turn_trace.json",
                "case_state.json",
                "event_log.json",
                "usage.json",
            )
        }
        (trial / "trial_manifest.json").write_text(
            json.dumps(
                {
                    "scenario_id": scenario_id,
                    "trial": 1,
                    "terminal": "STOP",
                    "safety": {
                        "ground_truth_exposure": 0,
                        "cross_scenario_evidence": 0,
                        "writes": 0,
                        "arbitrary_execution": 0,
                    },
                    "ground_truth_access": 0,
                    "judge_access": 0,
                    "hashes": hashes,
                }
            )
            + "\n",
            encoding="utf-8",
        )
    seal = tmp_path / "seal.json"
    payload = seal_e11_predictions(manifest, root, seal)
    assert payload["completion_count"] == 35
    assert verify_e11_seal(manifest, root, seal)["completion_count"] == 35
    (root / order[0] / "1" / "native_artifact.json").write_text(
        '{"mutated":true}\n', encoding="utf-8"
    )
    try:
        verify_e11_seal(manifest, root, seal)
    except Exception as error:
        assert "hash mismatch" in str(error)
    else:
        raise AssertionError("mutated sealed artifact was accepted")


def test_e11_checkpoint_runner_resumes_without_rerunning_completed_trials(tmp_path: Path) -> None:
    from packages.evals.itbench.e11_official import (
        E11OfficialManifestV1,
        predict_e11,
        seal_e11_predictions,
        verify_e11_seal,
    )
    from packages.evals.itbench.live_smoke_contract import ITBenchLiveSmokeRuntimeIdentity

    order = [f"Scenario-{index}" for index in range(1, 36)]
    manifest = E11OfficialManifestV1.model_construct(
        execution="ITB-E11",
        scenario_order=order,
        dataset_revision="offline",
        runtime_identity=ITBenchLiveSmokeRuntimeIdentity.model_construct(
            git_head="offline",
            relevant_paths=["offline"],
            relevant_content_sha256={"offline": "hash"},
            bundle_sha256="bundle",
            relevant_worktree_dirty=False,
        ),
    )
    calls: list[str] = []

    def executor(scenario_id: str) -> dict[str, object]:
        calls.append(scenario_id)
        return {
            "native_artifact": {"scenario_id": scenario_id},
            "agent_output": {"scenario_id": scenario_id},
            "turn_trace": [],
            "case_state": {},
            "event_log": [],
            "usage": {"provider_invocations": 0},
            "terminal": "STOP",
            "safety": {
                "ground_truth_exposure": 0,
                "cross_scenario_evidence": 0,
                "writes": 0,
                "arbitrary_execution": 0,
            },
        }

    predictions = tmp_path / "predictions"
    predict_e11(manifest, predictions, executor)
    assert len(calls) == 35
    calls.clear()
    predict_e11(manifest, predictions, lambda _: (_ for _ in ()).throw(AssertionError("rerun")))
    assert calls == []
    seal = tmp_path / "seal.json"
    seal_e11_predictions(manifest, predictions, seal)
    assert verify_e11_seal(manifest, predictions, seal)["completion_count"] == 35


def _minimal_e11_manifest(order: list[str]) -> Any:
    from packages.evals.itbench.e11_official import E11OfficialManifestV1
    from packages.evals.itbench.live_smoke_contract import ITBenchLiveSmokeRuntimeIdentity

    return E11OfficialManifestV1.model_construct(
        execution="ITB-E11",
        scenario_order=order,
        dataset_revision="offline",
        runtime_identity=ITBenchLiveSmokeRuntimeIdentity.model_construct(
            git_head="offline",
            relevant_paths=["offline"],
            relevant_content_sha256={"offline": "hash"},
            bundle_sha256="bundle",
            relevant_worktree_dirty=False,
        ),
    )


def _e11_executor_result(
    scenario_id: str,
    *,
    terminal: str = "STOP",
    safety: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "native_artifact": {"scenario_id": scenario_id},
        "agent_output": {"scenario_id": scenario_id},
        "turn_trace": [],
        "case_state": {},
        "event_log": [],
        "usage": {"provider_invocations": 1},
        "terminal": terminal,
        "safety": safety
        or {
            "ground_truth_exposure": 0,
            "cross_scenario_evidence": 0,
            "writes": 0,
            "arbitrary_execution": 0,
        },
    }


def test_e11_safety_violation_persists_failure_and_refuses_rerun(tmp_path: Path) -> None:
    from packages.evals.itbench.e11_official import E11PredictionError, predict_e11

    predictions = tmp_path / "predictions"
    manifest = _minimal_e11_manifest(["Scenario-1"])
    with pytest.raises(E11PredictionError, match="safety violation"):
        predict_e11(
            manifest,
            predictions,
            lambda scenario_id: _e11_executor_result(
                scenario_id,
                safety={
                    "ground_truth_exposure": 0,
                    "cross_scenario_evidence": 0,
                    "writes": 1,
                    "arbitrary_execution": 0,
                },
            ),
        )
    failure = json.loads((predictions / "prediction_failure.json").read_text(encoding="utf-8"))
    assert failure["error_code"] == "RUNTIME_SAFETY_VIOLATION"
    with pytest.raises(E11PredictionError, match="failure artifact already exists"):
        predict_e11(manifest, predictions, lambda _: (_ for _ in ()).throw(AssertionError()))


def test_e11_continuable_terminal_is_checkpointed_and_next_scenario_runs(tmp_path: Path) -> None:
    from packages.evals.itbench.e11_official import predict_e11

    predictions = tmp_path / "predictions"
    manifest = _minimal_e11_manifest(["Scenario-1", "Scenario-2"])
    calls: list[str] = []

    def executor(scenario_id: str) -> dict[str, Any]:
        calls.append(scenario_id)
        terminal = "MODEL_STEP_LIMIT" if scenario_id == "Scenario-2" else "STOP"
        return _e11_executor_result(scenario_id, terminal=terminal)

    checkpoints = predict_e11(manifest, predictions, executor)
    assert calls == ["Scenario-1", "Scenario-2"]
    assert [item["terminal"] for item in checkpoints] == ["STOP", "MODEL_STEP_LIMIT"]


def test_e11_existing_partial_scenario_fails_closed(tmp_path: Path) -> None:
    from packages.evals.itbench.e11_official import E11PredictionError, predict_e11

    predictions = tmp_path / "predictions"
    (predictions / "Scenario-1" / "1").mkdir(parents=True)
    (predictions / "Scenario-1" / "1" / "native_artifact.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(E11PredictionError, match="partial scenario"):
        predict_e11(
            _minimal_e11_manifest(["Scenario-1"]),
            predictions,
            lambda scenario_id: _e11_executor_result(scenario_id),
        )


@pytest.mark.parametrize("terminal", ["PROVIDER_ERROR", "TOTALLY_UNKNOWN"])
def test_e11_infrastructure_and_unknown_terminals_fail_closed(
    tmp_path: Path, terminal: str
) -> None:
    from packages.evals.itbench.e11_official import E11PredictionError, predict_e11

    predictions = tmp_path / "predictions"
    manifest = _minimal_e11_manifest(["Scenario-1"])
    with pytest.raises(E11PredictionError, match="terminal"):
        predict_e11(
            manifest,
            predictions,
            lambda scenario_id: _e11_executor_result(scenario_id, terminal=terminal),
        )
    failure = json.loads((predictions / "prediction_failure.json").read_text(encoding="utf-8"))
    assert failure["error_code"] == "UNEXPECTED_RUNTIME_TERMINAL"

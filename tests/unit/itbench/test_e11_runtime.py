"""End-to-end E11 control-path qualification with a fake provider."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from packages.evals.itbench import ITBenchEvidenceCategory, ITBenchLiteDataset, ITBenchScenario
from packages.evals.itbench.e11_control import (
    E11CaseMemory,
    EvidenceAssessment,
)
from packages.evals.itbench.e11_runtime import E11InvestigationRuntime
from packages.provider.fake import FakeModelProvider


def test_e11_context_contract_preserves_semantic_content() -> None:
    from packages.evals.itbench.e11_context import build_e11_context
    from packages.evals.itbench.e11_observability import RankedCandidate, RetrievalEvidence

    candidate = RankedCandidate(
        "C001",
        "prod/Deployment/checkout",
        1,
        1.0,
        "prod/Deployment",
        (RetrievalEvidence("FAILURE_EVENT", "event:1", "BackOff", 8.0),),
    )
    context = build_e11_context(
        incident={
            "scenario_id": "Scenario-1",
            "diagnostic_alerts": [{"alert_name": "CheckoutDown"}],
        },
        candidates=(candidate,),
        phase="VERIFY",
        remaining_model_calls=4,
        remaining_semantic_actions=8,
        investigation_state={
            "current_hypothesis": {"entity_handle": "C001"},
            "candidate_status": {"C001": {"status": "SUPPORTED"}},
            "unresolved_questions": "Does checkout explain the incident?",
            "ranking_history": [{"revision": 2, "handles": ["C001"]}],
            "last_rejection": {"code": "INVALID_TRANSITION", "reason": "bad operation"},
        },
        observation_evidence=(
            {
                "evidence_ref": "E001",
                "operation": "EVENT_ANALYSIS",
                "finding": {"reason": "BackOff"},
                "result_status": "POSITIVE_FINDING",
            },
        ),
        legal_next_actions=("INVESTIGATE", "STOP"),
        legal_target_operations={"C001": ("EVENT_ANALYSIS",)},
    )
    parsed = json.loads(context)
    investigation = parsed["investigation"]
    assert investigation["candidate_state"]["C001"]["status"] == "SUPPORTED"
    assert investigation["unresolved_question"] == "Does checkout explain the incident?"
    assert investigation["ranking_revisions"][0]["revision"] == 2
    assert investigation["last_rejection"]["code"] == "INVALID_TRANSITION"
    assert investigation["recent_evidence"][0]["finding"]["reason"] == "BackOff"
    assert investigation["legal_target_operations"]["C001"] == ["EVENT_ANALYSIS"]


def _backend(tmp_path: Path, **kwargs: Any) -> Any:
    root = tmp_path / "Scenario-runtime"
    root.mkdir(parents=True)
    (root / "alerts.json").write_text(
        json.dumps({"data": {"alerts": kwargs.get("alerts", [])}}), encoding="utf-8"
    )

    def write_tsv(name: str, rows: list[dict[str, str]], fields: tuple[str, ...]) -> str:
        content = ["\t".join(fields)]
        content.extend("\t".join(row.get(field, "") for field in fields) for row in rows)
        (root / name).write_text("\n".join(content) + "\n", encoding="utf-8")
        return name

    objects = kwargs.get("object_bodies", [])
    events = kwargs.get("event_bodies", [])
    metric_rows = kwargs.get("metric_rows", [])
    log_rows = kwargs.get("log_rows", [])
    object_rows = [
        {"Timestamp": "2025-01-01T00:00:00Z", "Body": json.dumps(body)} for body in objects
    ]
    event_rows = [
        {"Timestamp": "2025-01-01T00:00:00Z", "Body": json.dumps(body)} for body in events
    ]
    files: dict[ITBenchEvidenceCategory, tuple[str, ...]] = {
        ITBenchEvidenceCategory.ALERTS: ("alerts.json",),
        ITBenchEvidenceCategory.K8S_OBJECTS: (
            write_tsv("objects.tsv", object_rows, ("Timestamp", "Body")),
        ),
        ITBenchEvidenceCategory.K8S_EVENTS: (
            write_tsv("events.tsv", event_rows, ("Timestamp", "Body")),
        ),
        ITBenchEvidenceCategory.METRICS: (
            write_tsv(
                "metrics.tsv",
                metric_rows,
                ("Timestamp", "metric_name", "value", "workload", "namespace", "metric_type"),
            ),
        ),
        ITBenchEvidenceCategory.LOGS: (
            write_tsv(
                "logs.tsv", log_rows, ("Timestamp", "Body", "severity", "workload", "namespace")
            ),
        ),
        ITBenchEvidenceCategory.TRACES: (
            write_tsv("traces.tsv", [], ("Timestamp", "ServiceName", "Body")),
        ),
    }
    scenario = ITBenchScenario(
        scenario_id="Scenario-997",
        snapshot_path=str(root),
        evidence_categories=tuple(ITBenchEvidenceCategory),
        evidence_files=files,
    )
    from packages.evals.itbench.snapshot_backend import ITBenchSnapshotBackend

    return ITBenchSnapshotBackend(cast(ITBenchLiteDataset, object()), scenario, max_rows=50)


def test_fake_provider_uses_real_e11_runtime_path(tmp_path: Path) -> None:
    backend = _backend(
        tmp_path,
        object_bodies=[
            {"kind": "Deployment", "metadata": {"name": "checkout", "namespace": "prod"}}
        ],
        event_bodies=[
            {
                "involvedObject": {"kind": "Deployment", "name": "checkout", "namespace": "prod"},
                "reason": "BackOff",
                "type": "Warning",
            }
        ],
    )
    provider = FakeModelProvider(
        [
            {"action": "OBSERVE", "operation": "INCIDENT_OVERVIEW", "rationale": None},
            {"action": "HYPOTHESIZE", "target": "C001", "rationale": None},
            {
                "action": "INVESTIGATE",
                "target": "C001",
                "operation": "EVENT_ANALYSIS",
                "rationale": None,
            },
            {"action": "SUBMIT", "targets": ["C001"], "rationale": None},
        ]
    )
    result = E11InvestigationRuntime(provider, backend, execution_id="offline").run()
    assert result["terminal"] == "SUBMIT"
    assert result["safety"] == {
        "ground_truth_exposure": 0,
        "cross_scenario_evidence": 0,
        "writes": 0,
        "arbitrary_execution": 0,
    }
    assert provider.accounting_snapshot().provider_invocations == 4
    assert any(item.get("operation") == "EVENT_ANALYSIS" for item in result["turn_trace"])
    assert len(result["case_state"]["ranking_history"]) >= 2
    later_context = provider.requests[3].messages[-1].content
    assert "investigation" in later_context
    assert "supporting_evidence" in later_context
    assert "ranking_revisions" in later_context


def test_no_data_evidence_cannot_support_candidate() -> None:
    from packages.evals.itbench.e11_observability import (
        ObservedEntityCatalog,
        RankedCandidate,
        RetrievalEvidence,
    )

    catalog = ObservedEntityCatalog(scenario_id="offline")
    entity = catalog.add(
        canonical="prod/Deployment/checkout",
        identity_type="KubernetesEntity",
        namespace="prod",
        kind="Deployment",
        name="checkout",
        source_category="k8s_objects",
        provenance="DIRECT_K8S_OBJECT",
        evidence_ref="object:checkout",
    )
    memory = E11CaseMemory(scenario_id="offline", catalog=catalog)
    memory.initialize(
        (
            RankedCandidate(
                entity.handle,
                entity.canonical,
                1,
                1.0,
                "prod/Deployment",
                (RetrievalEvidence("ALERT_LINK", "alert:1", "bounded", 1.0),),
            ),
        )
    )
    memory.hypothesize(entity.handle)
    evidence = memory.add_evidence(
        entity.handle,
        "LOG_ANALYSIS",
        {"result_status": "NO_DATA", "usable": False, "patterns": []},
    )
    try:
        memory.assess(entity.handle, evidence, EvidenceAssessment.SUPPORTS, dimension="causal")
    except ValueError as error:
        assert "unusable" in str(error)
    else:
        raise AssertionError("NO_DATA evidence was accepted as causal support")


def test_identical_immutable_no_data_recheck_is_not_allowed() -> None:
    from packages.evals.itbench.e11_observability import (
        ObservedEntityCatalog,
    )

    catalog = ObservedEntityCatalog(scenario_id="offline")
    entity = catalog.add(
        canonical="prod/Deployment/checkout",
        identity_type="KubernetesEntity",
        namespace="prod",
        kind="Deployment",
        name="checkout",
        source_category="k8s_objects",
        provenance="DIRECT_K8S_OBJECT",
        evidence_ref="object:checkout",
    )
    from packages.evals.itbench.e9_memory import E9CaseMemory

    memory = E9CaseMemory(execution_id="offline", scenario_id="offline")
    memory.discover_entities((entity.as_dict(),))
    memory.append(
        "OPERATION_REQUESTED",
        1,
        {"entity_handle": entity.handle, "operation": "EVENT_ANALYSIS"},
    )
    assert not memory.recheck_allowed(entity.handle, "EVENT_ANALYSIS")


def test_same_operation_on_different_target_has_independent_identity() -> None:
    from packages.evals.itbench.e11_observability import (
        ObservedEntityCatalog,
    )

    catalog = ObservedEntityCatalog(scenario_id="offline")
    first = catalog.add(
        canonical="prod/Deployment/checkout",
        identity_type="KubernetesEntity",
        namespace="prod",
        kind="Deployment",
        name="checkout",
        source_category="k8s_objects",
        provenance="DIRECT_K8S_OBJECT",
        evidence_ref="object:checkout",
    )
    second = catalog.add(
        canonical="prod/Deployment/payment",
        identity_type="KubernetesEntity",
        namespace="prod",
        kind="Deployment",
        name="payment",
        source_category="k8s_objects",
        provenance="DIRECT_K8S_OBJECT",
        evidence_ref="object:payment",
    )
    from packages.evals.itbench.e9_memory import E9CaseMemory

    memory = E9CaseMemory(execution_id="offline", scenario_id="offline")
    memory.discover_entities((first.as_dict(), second.as_dict()))
    memory.append(
        "OPERATION_REQUESTED",
        1,
        {"entity_handle": first.handle, "operation": "EVENT_ANALYSIS"},
    )
    assert not memory.recheck_allowed(first.handle, "EVENT_ANALYSIS")
    assert not memory.has_operation(second.handle, "EVENT_ANALYSIS")


def test_two_semantically_illegal_actions_reach_protocol_stalled(tmp_path: Path) -> None:
    """Parse success must not clear the rejection window before _apply()."""
    backend = _backend(
        tmp_path,
        object_bodies=[
            {"kind": "Deployment", "metadata": {"name": "checkout", "namespace": "prod"}}
        ],
        event_bodies=[
            {
                "involvedObject": {"kind": "Deployment", "name": "checkout", "namespace": "prod"},
                "reason": "BackOff",
                "type": "Warning",
            }
        ],
    )
    provider = FakeModelProvider(
        [
            {"action": "OBSERVE", "operation": "INCIDENT_OVERVIEW", "rationale": None},
            {"action": "HYPOTHESIZE", "target": "C001", "rationale": None},
            {
                "action": "INVESTIGATE",
                "target": "C001",
                "operation": "COMPARE_REPLICAS",
                "rationale": None,
            },
            {
                "action": "INVESTIGATE",
                "target": "C001",
                "operation": "COMPARE_REPLICAS",
                "rationale": None,
            },
        ]
    )
    result = E11InvestigationRuntime(provider, backend, execution_id="reject-window").run()
    assert result["terminal"] == "PROTOCOL_STALLED"
    assert result["case_state"]["action_rejections"] == 2
    assert result["case_state"]["consecutive_rejections"] == 2
    assert len(provider.requests) >= 3


def test_rejected_target_operation_is_visible_and_recoverable(tmp_path: Path) -> None:
    backend = _backend(
        tmp_path,
        object_bodies=[
            {"kind": "Deployment", "metadata": {"name": "checkout", "namespace": "prod"}}
        ],
        event_bodies=[
            {
                "involvedObject": {"kind": "Deployment", "name": "checkout", "namespace": "prod"},
                "reason": "BackOff",
                "type": "Warning",
            }
        ],
    )
    provider = FakeModelProvider(
        [
            {"action": "HYPOTHESIZE", "target": "C001", "rationale": None},
            {
                "action": "INVESTIGATE",
                "target": "C001",
                "operation": "COMPARE_REPLICAS",
                "rationale": None,
            },
            {
                "action": "INVESTIGATE",
                "target": "C001",
                "operation": "SPEC_ANALYSIS",
                "rationale": None,
            },
            {"action": "STOP", "stop_reason": "no causal support"},
        ]
    )
    result = E11InvestigationRuntime(provider, backend, execution_id="recovery").run()
    assert result["terminal"] == "STOP"
    rejection_context = json.loads(provider.requests[2].messages[-1].content)
    rejection = rejection_context["investigation"]["last_rejection"]
    assert rejection["attempted_operation"] == "COMPARE_REPLICAS"
    assert "SPEC_ANALYSIS" in rejection["valid_operations"]
    assert result["case_state"]["recovered_action_rejections"] == 1


def test_submit_is_hidden_until_runtime_support_exists(tmp_path: Path) -> None:
    backend = _backend(
        tmp_path,
        object_bodies=[
            {"kind": "Deployment", "metadata": {"name": "checkout", "namespace": "prod"}}
        ],
        event_bodies=[
            {
                "involvedObject": {
                    "kind": "Deployment",
                    "name": "checkout",
                    "namespace": "prod",
                },
                "reason": "BackOff",
                "type": "Warning",
            }
        ],
    )
    seen: list[tuple[str, ...]] = []

    def response(request: Any) -> dict[str, Any]:
        seen.append(tuple(request.allowed_decisions or ()))
        if len(seen) == 1:
            return {"action": "OBSERVE", "operation": "INCIDENT_OVERVIEW", "rationale": None}
        if len(seen) == 2:
            return {"action": "HYPOTHESIZE", "target": "C001", "rationale": None}
        return {"action": "STOP", "stop_reason": "no additional evidence"}

    provider = FakeModelProvider([response] * 3)
    result = E11InvestigationRuntime(provider, backend, execution_id="submit-surface").run()
    assert result["terminal"] == "STOP"
    assert "SUBMIT_DIAGNOSIS" not in seen[1]
    assert "SUBMIT_DIAGNOSIS" not in seen[2]
    assert result["agent_output"]["contributing_factor"] == []


def test_runtime_contradiction_blocks_submission(tmp_path: Path) -> None:
    backend = _backend(
        tmp_path,
        object_bodies=[
            {"kind": "Deployment", "metadata": {"name": "checkout", "namespace": "prod"}}
        ],
        event_bodies=[
            {
                "involvedObject": {
                    "kind": "Deployment",
                    "name": "checkout",
                    "namespace": "prod",
                },
                "reason": "BackOff",
                "type": "Warning",
            }
        ],
        metric_rows=[
            {
                "Timestamp": "2025-01-01T00:00:01Z",
                "metric_name": "request_latency_seconds",
                "value": "5",
                "workload": "checkout",
                "namespace": "prod",
                "metric_type": "gauge",
            },
            {
                "Timestamp": "2025-01-01T00:00:02Z",
                "metric_name": "request_latency_seconds",
                "value": "5",
                "workload": "checkout",
                "namespace": "prod",
                "metric_type": "gauge",
            },
        ],
    )

    def metric_analysis(_arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "matching_count": 2,
            "aggregates_by_metric": {
                "request_latency_seconds": {
                    "delta": 0.0,
                    "relative_change": 0.0,
                    "anomaly": False,
                }
            },
        }

    backend.metric_analysis = metric_analysis
    provider = FakeModelProvider(
        [
            {"action": "OBSERVE", "operation": "INCIDENT_OVERVIEW", "rationale": None},
            {"action": "HYPOTHESIZE", "target": "C001", "rationale": None},
            {
                "action": "INVESTIGATE",
                "target": "C001",
                "operation": "METRIC_ANOMALIES",
                "rationale": None,
            },
            {"action": "STOP", "stop_reason": "metric evidence contradicts the hypothesis"},
        ]
    )
    result = E11InvestigationRuntime(provider, backend, execution_id="polarity").run()
    assert result["terminal"] == "STOP"
    assessments = result["assessment_history"]
    assert assessments[0]["assessment"] == "CONTRADICTS"
    assert result["case_state"]["candidate_state"]["C001"]["status"] == "CONTRADICTED"


def _checkout_alert(active_at: str) -> dict[str, Any]:
    return {
        "state": "firing",
        "activeAt": active_at,
        "labels": {"alertname": "RequestErrorRate", "service_name": "checkout"},
        "annotations": {},
    }


def _config_fixture(event_time: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    deployment = {
        "kind": "Deployment",
        "metadata": {"name": "checkout", "namespace": "prod", "labels": {"app": "checkout"}},
        "spec": {
            "template": {
                "metadata": {"labels": {"app": "checkout"}},
                "spec": {
                    "containers": [
                        {"name": "app", "envFrom": [{"configMapRef": {"name": "checkout-config"}}]}
                    ]
                },
            }
        },
    }
    config = {
        "kind": "ConfigMap",
        "metadata": {"name": "checkout-config", "namespace": "prod"},
        "data": {"mode": "deny"},
    }
    event = {
        "involvedObject": {"kind": "Deployment", "name": "checkout", "namespace": "prod"},
        "reason": "BackOff",
        "type": "Warning",
        "lastTimestamp": event_time,
    }
    return [deployment, config], [event]


def _spec_run(backend: Any, execution_id: str) -> tuple[dict[str, Any], FakeModelProvider]:
    provider = FakeModelProvider(
        [
            {"action": "HYPOTHESIZE", "target": "C002", "rationale": None},
            {
                "action": "INVESTIGATE",
                "target": "C002",
                "operation": "SPEC_ANALYSIS",
                "rationale": None,
            },
            {"action": "SUBMIT", "targets": ["C002"], "rationale": None},
            {"action": "STOP", "stop_reason": "unsupported"},
        ]
    )
    return E11InvestigationRuntime(provider, backend, execution_id=execution_id).run(), provider


def test_experimental_spec_finding_is_not_submit_support(tmp_path: Path) -> None:
    objects, events = _config_fixture("2025-01-01T00:01:00Z")
    backend = _backend(
        tmp_path,
        object_bodies=objects,
        event_bodies=events,
        alerts=[_checkout_alert("2025-01-01T00:00:30Z")],
    )
    result, _provider = _spec_run(backend, "config")
    assert result["terminal"] == "STOP"
    assert result["assessment_history"][0]["dimension"] == "configuration"
    assert result["assessment_history"][0]["assessment"] == "INCONCLUSIVE"
    assert "SUBMIT_DIAGNOSIS" not in tuple(_provider.requests[2].allowed_decisions or ())

    plain_backend = _backend(
        tmp_path / "plain",
        object_bodies=[{"kind": "ConfigMap", "metadata": {"name": "plain", "namespace": "prod"}}],
        event_bodies=[
            {
                "involvedObject": {"kind": "ConfigMap", "name": "plain", "namespace": "prod"},
                "reason": "BackOff",
                "type": "Warning",
            }
        ],
    )
    plain_provider = FakeModelProvider(
        [
            {"action": "HYPOTHESIZE", "target": "C001", "rationale": None},
            {
                "action": "INVESTIGATE",
                "target": "C001",
                "operation": "SPEC_ANALYSIS",
                "rationale": None,
            },
            {"action": "STOP", "stop_reason": "existence is not causality"},
        ]
    )
    plain = E11InvestigationRuntime(plain_provider, plain_backend, execution_id="plain").run()
    assert plain["terminal"] == "STOP"
    assert "SUBMIT_DIAGNOSIS" not in tuple(plain_provider.requests[2].allowed_decisions or ())


def test_spec_analysis_requires_failure_near_incident_onset(tmp_path: Path) -> None:
    objects, events = _config_fixture("2025-01-01T02:00:00Z")
    backend = _backend(
        tmp_path,
        object_bodies=objects,
        event_bodies=events,
        alerts=[_checkout_alert("2025-01-01T00:00:30Z")],
    )
    result, provider = _spec_run(backend, "config-late")
    assert result["terminal"] != "SUBMIT"
    assert result["assessment_history"][0]["assessment"] == "INCONCLUSIVE"
    assert "SUBMIT_DIAGNOSIS" not in tuple(provider.requests[2].allowed_decisions or ())


def test_spec_analysis_without_observable_onset_is_inconclusive(tmp_path: Path) -> None:
    objects, events = _config_fixture("2025-01-01T00:01:00Z")
    backend = _backend(tmp_path, object_bodies=objects, event_bodies=events)
    result, _provider = _spec_run(backend, "config-no-alert")
    assert result["terminal"] != "SUBMIT"
    assert result["assessment_history"][0]["assessment"] == "INCONCLUSIVE"


def test_contradiction_can_be_explicitly_superseded() -> None:
    from packages.evals.itbench.e11_observability import (
        ObservedEntityCatalog,
        RankedCandidate,
        RetrievalEvidence,
    )

    catalog = ObservedEntityCatalog(scenario_id="offline")
    entity = catalog.add(
        canonical="prod/Deployment/checkout",
        identity_type="KubernetesEntity",
        namespace="prod",
        kind="Deployment",
        name="checkout",
        source_category="k8s_objects",
        provenance="DIRECT_K8S_OBJECT",
        evidence_ref="object:checkout",
    )
    memory = E11CaseMemory(scenario_id="offline", catalog=catalog)
    candidate = RankedCandidate(
        entity.handle,
        entity.canonical,
        1,
        1.0,
        "prod/Deployment",
        (RetrievalEvidence("FAILURE_EVENT", "event:1", "bounded", 1.0),),
    )
    memory.initialize((candidate,))
    memory.hypothesize(entity.handle)
    contradiction = memory.add_evidence(entity.handle, "EVENT_ANALYSIS", {"reason": "none"})
    memory.assess(entity.handle, contradiction, EvidenceAssessment.CONTRADICTS, dimension="causal")
    support = memory.add_evidence(entity.handle, "LOG_ANALYSIS", {"error_count": 1})
    memory.assess(
        entity.handle,
        support,
        EvidenceAssessment.SUPPORTS,
        dimension="causal",
        supersedes_evidence_ref=contradiction,
    )
    assert memory.submit_ready((entity.handle,))


def test_trace_support_requires_candidate_to_be_error_origin() -> None:
    from packages.evals.itbench.e9_semantic import _error_origins

    edges = {
        ("frontend", "checkout", "ERROR"): 5,
        ("checkout", "payment", "ERROR"): 3,
        ("payment", "unknown", "OK"): 9,
    }
    assert _error_origins(edges) == ["payment"]
    assess = E11InvestigationRuntime._assess_semantic_result
    downstream = {
        "edges": [
            {"source_service": "frontend", "destination_service": "checkout", "status": "ERROR"}
        ],
        "candidate_service": "frontend",
        "candidate_is_error_origin": False,
    }
    assert assess("TRACE_ERROR_TREE", downstream)[0] == EvidenceAssessment.INCONCLUSIVE
    origin = {**downstream, "candidate_service": "payment", "candidate_is_error_origin": True}
    assert assess("TRACE_ERROR_TREE", origin)[0] == EvidenceAssessment.SUPPORTS


def test_incident_context_groups_alerts_and_separates_background() -> None:
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from packages.evals.itbench.e11_runtime import _diagnostic_onset, _incident_context

    base = datetime(2025, 1, 1, tzinfo=UTC)

    def alert(name: str, service: str, minutes: int) -> Any:
        return SimpleNamespace(
            alert_name=name,
            service=service,
            namespace="prod",
            starts_at=base + timedelta(minutes=minutes),
            labels={},
        )

    alerts = (
        *(alert("KubeSchedulerDown", "unknown", 0) for _ in range(20)),
        alert("RequestErrorRate", "checkout", 12),
        alert("RequestErrorRate", "checkout", 14),
        alert("RequestLatency", "frontend", 10),
    )
    context = _incident_context("S", SimpleNamespace(), alerts)
    assert [item["alert_name"] for item in context["diagnostic_alerts"]] == [
        "RequestLatency",
        "RequestErrorRate",
    ]
    assert context["diagnostic_alerts"][1]["occurrence_count"] == 2
    assert context["background_alert_counts"] == {"KubeSchedulerDown": 20}
    assert context["incident_start"] == (base + timedelta(minutes=10)).isoformat()
    assert _diagnostic_onset(alerts) == base + timedelta(minutes=10)


def test_context_bounding_never_emits_partial_json_strings() -> None:
    from packages.evals.itbench.e11_context import _bounded_item

    value = {f"key{index}": "x" * 60 for index in range(20)}
    bounded = _bounded_item(value, 400)
    assert "summary" not in bounded
    assert bounded["truncated_keys"] > 0
    assert all(item == "x" * 60 for key, item in bounded.items() if key != "truncated_keys")
    items = _bounded_item([{"n": index, "pad": "y" * 50} for index in range(12)], 300)
    assert items and items[-1]["n"] == 11
    assert len(json.dumps(items)) <= 300


def test_canary_submit_explanation_uses_persisted_support_provenance() -> None:
    from packages.evals.itbench.e11_canary import _submit_explanations

    result = {
        "case_state": {
            "submitted_targets": ["C001"],
            "hypothesis_history": [{"entity_handle": "C001"}],
        },
        "catalog": [{"handle": "C001", "canonical": "prod/Service/checkout"}],
        "assessment_history": [
            {
                "entity_handle": "C001",
                "evidence_ref": "ASM-E001",
                "assessment": "SUPPORTS",
                "dimension": "dependency",
                "rationale": "candidate is the observed error origin",
            }
        ],
        "evidence_ledger": {
            "ASM-E001": {"operation": "TRACE_ERROR_TREE"},
        },
        "final_ranking": [{"handle": "C001", "rank": 1}],
    }
    explanation = _submit_explanations("Scenario-1", result)[0]
    assert explanation["supporting_operations"] == ["TRACE_ERROR_TREE"]
    assert explanation["support_dimensions"] == ["dependency"]
    assert explanation["final_ranking_position"] == 1
    assert explanation["submit_trigger_operation"] == "TRACE_ERROR_TREE"


def test_real_runtime_empty_output_passes_official_checkpoint(tmp_path: Path) -> None:
    from packages.evals.itbench.e11_official import E11OfficialManifestV1, predict_e11
    from packages.evals.itbench.live_smoke_contract import ITBenchLiveSmokeRuntimeIdentity

    backend = _backend(
        tmp_path / "snapshot", object_bodies=_config_fixture("2025-01-01T00:01:00Z")[0]
    )
    provider = FakeModelProvider([{"action": "STOP", "stop_reason": "insufficient evidence"}])
    manifest = E11OfficialManifestV1.model_construct(
        execution="ITB-E11",
        scenario_order=["Scenario-997"],
        dataset_revision="offline",
        runtime_identity=ITBenchLiveSmokeRuntimeIdentity.model_construct(
            git_head="offline",
            relevant_paths=["offline"],
            relevant_content_sha256={"offline": "hash"},
            bundle_sha256="bundle",
            relevant_worktree_dirty=False,
        ),
    )
    checkpoints = predict_e11(
        manifest,
        tmp_path / "predictions",
        lambda _scenario_id: E11InvestigationRuntime(
            provider, backend, execution_id="ITB-E11"
        ).run(),
    )
    assert checkpoints[0]["terminal"] == "STOP"
    output = json.loads(
        (tmp_path / "predictions" / "Scenario-997" / "1" / "agent_output.json").read_text(
            encoding="utf-8"
        )
    )
    assert output["contributing_factor"] == []

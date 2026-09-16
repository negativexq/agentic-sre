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
    (root / "alerts.json").write_text(json.dumps({"data": {"alerts": []}}), encoding="utf-8")

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


def test_runtime_contradiction_then_explicit_supersession(tmp_path: Path) -> None:
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
    metric_calls = 0

    def metric_analysis(_arguments: dict[str, Any]) -> dict[str, Any]:
        nonlocal metric_calls
        metric_calls += 1
        if metric_calls == 1:
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
        return {
            "matching_count": 2,
            "aggregates_by_metric": {
                "request_latency_seconds": {
                    "delta": 5.0,
                    "relative_change": 1.0,
                    "anomaly": True,
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
            {
                "action": "INVESTIGATE",
                "target": "C001",
                "operation": "METRIC_ANOMALIES",
                "rationale": None,
            },
            {"action": "SUBMIT", "targets": ["C001"], "rationale": None},
        ]
    )
    result = E11InvestigationRuntime(provider, backend, execution_id="polarity").run()
    assert result["terminal"] == "SUBMIT"
    assessments = result["assessment_history"]
    assert assessments[0]["assessment"] == "CONTRADICTS"
    assert assessments[1]["assessment"] == "SUPPORTS"
    assert assessments[1]["supersedes_evidence_ref"] == assessments[0]["evidence_ref"]
    assert result["case_state"]["candidate_state"]["C001"]["status"] == "SUPPORTED"


def test_spec_analysis_has_causal_config_path_but_not_existence_only(tmp_path: Path) -> None:
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
    }
    backend = _backend(tmp_path, object_bodies=[deployment, config], event_bodies=[event])
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
        ]
    )
    result = E11InvestigationRuntime(provider, backend, execution_id="config").run()
    assert result["terminal"] == "SUBMIT"
    assert result["assessment_history"][0]["dimension"] == "configuration"

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

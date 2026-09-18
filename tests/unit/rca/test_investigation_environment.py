from __future__ import annotations

from typing import Any

from rca_builders import at

from packages.rca.engine import build_case, diagnose_case
from packages.rca.investigation.environment import SeedPolicy, initial_view, investigation_backend
from packages.rca.investigation.graph import investigate_diagnosis
from packages.rca.investigation.opportunity_audit import audit_incident
from packages.rca.investigation.policy import ScriptedInvestigationPolicy
from packages.rca.model import (
    Alert,
    ClusterEvent,
    EntityRef,
    InvestigationAction,
    InvestigationQuery,
    LogRecord,
    ObjectVersion,
)
from packages.rca.source import InMemorySource


def _ref(kind: str, name: str) -> EntityRef:
    return EntityRef(kind=kind, name=name, namespace="shop")


def _version(entity: EntityRef, minute: float, body: dict[str, Any], index: int) -> ObjectVersion:
    return ObjectVersion(
        entity=entity,
        observed_at=at(minute),
        body={
            "kind": entity.kind,
            "metadata": {"name": entity.name, "namespace": entity.namespace},
            **body,
        },
        evidence_id=f"object:{entity.canonical}:{minute}:{index}",
    )


def _event(entity: EntityRef, reason: str, minute: float) -> ClusterEvent:
    timestamp = at(minute)
    return ClusterEvent(
        entity=entity,
        reason=reason,
        type="Warning",
        message=reason,
        first_at=timestamp,
        last_at=timestamp,
        count=1,
        evidence_id=f"event:{entity.canonical}:{reason}:{minute}",
    )


def _source_with_hidden_hpa_history() -> tuple[InMemorySource, EntityRef]:
    versions: list[ObjectVersion] = []
    for name in ("left", "right"):
        labels = {"app": "api"}
        deployment = _ref("Deployment", name)
        hpa = _ref("HorizontalPodAutoscaler", f"{name}-hpa")
        pod = _ref("Pod", f"{name}-pod")
        deployment_body = {
            "metadata": {"labels": labels},
            "spec": {
                "selector": {"matchLabels": labels},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {"containers": [{"name": name, "image": "app:1"}]},
                },
            },
        }
        versions.extend(
            (
                _version(deployment, 0, deployment_body, 0),
                _version(
                    pod,
                    0,
                    {
                        "metadata": {
                            "labels": labels,
                            "ownerReferences": [{"kind": "ReplicaSet", "name": f"{name}-rs"}],
                        },
                        "spec": {"containers": [{"name": name, "image": "app:1"}]},
                    },
                    0,
                ),
            )
        )
        hpa_body: dict[str, Any] = {
            "spec": {"scaleTargetRef": {"kind": "Deployment", "name": name}, "maxReplicas": 3}
        }
        if name == "right":
            versions.append(
                _version(
                    hpa,
                    -20,
                    {
                        "spec": {
                            "scaleTargetRef": {"kind": "Deployment", "name": name},
                            "maxReplicas": 2,
                        }
                    },
                    0,
                )
            )
        versions.append(_version(hpa, 0, hpa_body, 1 if name == "right" else 0))

    versions.append(_version(_ref("Service", "api"), 0, {"spec": {"selector": {"app": "api"}}}, 0))
    left_hpa = _ref("HorizontalPodAutoscaler", "left-hpa")
    right_hpa = _ref("HorizontalPodAutoscaler", "right-hpa")
    source = InMemorySource(
        name="production-investigation-history",
        alert_items=[
            Alert(
                name="ApiLatency",
                service="api",
                namespace="shop",
                starts_at=at(2),
            )
        ],
        versions=versions,
        event_items=[_event(left_hpa, "MetricWarning", 1), _event(right_hpa, "MetricWarning", 1)],
        cutoff=at(10),
    )
    return source, right_hpa


def test_bounded_initial_view_can_resolve_from_real_history_query() -> None:
    source, right_hpa = _source_with_hidden_hpa_history()
    bounded_case = build_case(initial_view(source))
    initial = diagnose_case(bounded_case)

    assert initial.resolution.value == "AMBIGUOUS"
    hidden_ref = "object:shop/HorizontalPodAutoscaler/right-hpa:-20:0"
    assert hidden_ref not in {
        evidence_id for finding in bounded_case.findings for evidence_id in finding.evidence_ids
    }
    gap = next(gap for gap in initial.information_gaps if gap.dimension.value == "FAILURE_ONSET")
    assert "history" in gap.candidate_tools

    action = InvestigationAction(
        action="inspect",
        gap_id=gap.gap_id,
        capability="history",
        target=right_hpa,
        query=InvestigationQuery(start=at(-30), end=at(30)),
        rationale="inspect the wider HPA history",
    )
    result = investigate_diagnosis(
        source,
        policy=ScriptedInvestigationPolicy([action]),
    )

    assert result.initial_resolution.value == "AMBIGUOUS"
    assert result.final_resolution.value == "RESOLVED"
    assert result.diagnosis.root_cause == right_hpa
    assert result.tool_calls == 1
    assert len(result.ledger) == 1
    entry = result.ledger[0]
    assert entry.capability == "history"
    assert hidden_ref in entry.new_evidence_refs
    assert any("SPEC_CHANGE" in finding_id for finding_id in entry.normalized_finding_ids)


def test_latest_only_seed_keeps_structural_hpa_candidates_for_investigation() -> None:
    source, left_hpa = _source_with_hidden_hpa_history()
    view = initial_view(
        source,
        policy=SeedPolicy(
            name="latest-only-no-events",
            history_window=None,
            event_before=None,
            event_after=None,
        ),
    )
    case = build_case(view)
    diagnosis = diagnose_case(case)

    hpa_hypotheses = {
        hypothesis.causal_actor
        for hypothesis in case.hypotheses
        if hypothesis.causal_actor.kind == "HorizontalPodAutoscaler"
    }
    assert left_hpa in hpa_hypotheses
    assert diagnosis.resolution.value == "AMBIGUOUS"
    assert any(
        gap.resolvability.value == "RESOLVABLE" and gap.candidate_tools
        for gap in diagnosis.information_gaps
    )
    structural = [hypothesis for hypothesis in case.hypotheses if hypothesis.structural_basis]
    assert structural
    assert all(not hypothesis.findings and hypothesis.score == 0 for hypothesis in structural)


def test_hidden_chaos_events_cannot_create_initial_topology_edges() -> None:
    pod = _ref("Pod", "api-pod")
    schedule = _ref("Schedule", "nightly")
    chaos = _ref("StressChaos", "nightly-123")
    service = _ref("Service", "api")
    source = InMemorySource(
        name="hidden-chaos-topology",
        alert_items=[Alert(name="ApiLatency", service="api", namespace="shop", starts_at=at(0))],
        versions=[
            _version(pod, 0, {"metadata": {"labels": {"app": "api"}}}, 0),
            _version(service, 0, {"spec": {"selector": {"app": "api"}}}, 0),
            _version(schedule, 0, {}, 0),
            _version(chaos, 0, {}, 0),
        ],
        event_items=[
            _event(schedule, "Started", -30),
            ClusterEvent(
                entity=chaos,
                reason="Started",
                type="Normal",
                message="apply chaos for shop/api-pod",
                first_at=at(-30),
                last_at=at(-30),
                evidence_id="event:hidden-chaos",
            ),
        ],
        cutoff=at(10),
    )

    bounded = build_case(initial_view(source))

    assert not any(edge.relation in {"spawns", "disrupts"} for edge in bounded.topology.edges)


def test_investigation_only_error_logs_are_hidden_from_seed_but_queryable() -> None:
    log = LogRecord(
        service="api",
        at=at(-30),
        severity="ERROR",
        message="upstream dependency failed",
        evidence_id="log:hidden-api",
    )
    source = InMemorySource(
        name="hidden-investigation-log",
        alert_items=[Alert(name="ApiLatency", service="api", namespace="shop", starts_at=at(0))],
        versions=[_version(_ref("Service", "api"), 0, {}, 0)],
        error_items=[log],
        cutoff=at(10),
    )
    view = initial_view(source)
    initial = build_case(view)

    assert view.error_logs() == ()
    assert "log:hidden-api" not in {
        evidence_id for finding in initial.findings for evidence_id in finding.evidence_ids
    }
    records = investigation_backend(source).query_logs(
        _ref("Service", "api"), InvestigationQuery(start=at(-40), end=at(-20))
    )
    assert tuple(item.evidence_id for item in records) == ("log:hidden-api",)


def test_opportunity_audit_uses_production_query_normalizer_and_resolver() -> None:
    source, _right_hpa = _source_with_hidden_hpa_history()

    audit = audit_incident(source)

    assert audit.classification == "RESOLVABLE_BY_AVAILABLE_QUERY"
    assert any(
        item.capability == "history" and item.new_findings and item.effect == "RESOLUTION_CHANGED"
        for item in audit.opportunities
    )

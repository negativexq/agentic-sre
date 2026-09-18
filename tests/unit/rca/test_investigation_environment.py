from __future__ import annotations

from typing import Any

from rca_builders import at

from packages.rca.engine import build_case, diagnose_case
from packages.rca.investigation.environment import initial_view
from packages.rca.investigation.graph import investigate_diagnosis
from packages.rca.investigation.policy import ScriptedInvestigationPolicy
from packages.rca.model import (
    Alert,
    ClusterEvent,
    EntityRef,
    InvestigationAction,
    InvestigationQuery,
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

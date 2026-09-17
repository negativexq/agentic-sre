"""Synthetic invariants for causal episode grouping."""

from __future__ import annotations

from rca_builders import alert, at, deployment, event, pod, ref, replicaset, service, version

from packages.rca.engine import build_case
from packages.rca.hypotheses import group_candidates, summarize_diagnostics
from packages.rca.model import (
    Candidate,
    EvidenceTemporalRole,
    Finding,
    FindingKind,
)
from packages.rca.ranking import RankingConfig
from packages.rca.source import InMemorySource


def _rollout_source(
    *, changed_body: dict[str, object] | None = None, pod_body: dict[str, object] | None = None
) -> InMemorySource:
    versions = [
        version("shop/Deployment/catalog", 0, deployment("catalog", image="app:1")),
        version(
            "shop/ReplicaSet/catalog-rs",
            0,
            replicaset("catalog-rs", "catalog"),
        ),
        version(
            "shop/Pod/catalog-rs-abcde",
            0,
            pod_body or pod("catalog-rs-abcde", "catalog", "catalog-rs"),
        ),
        version("shop/Service/catalog", 0, service("catalog")),
    ]
    if changed_body is not None:
        versions.append(version("shop/Deployment/catalog", 5, changed_body, 1))
    return InMemorySource(
        name="hypothesis-rollout",
        alert_items=[alert("RequestErrorRate", "catalog", 7)],
        versions=versions,
        cutoff=at(10),
    )


def test_rollout_and_pod_manifestation_form_one_hypothesis() -> None:
    source = _rollout_source(changed_body=deployment("catalog", image="app:2"))
    source.event_items.append(
        event("shop/Pod/catalog-rs-abcde", "ImagePullBackOff", 6, type_="Warning")
    )

    case = build_case(source)

    assert len(case.hypotheses) == 1
    hypothesis = case.hypotheses[0]
    assert hypothesis.causal_actor == ref("shop/Deployment/catalog")
    assert hypothesis.manifestations == (ref("shop/Pod/catalog-rs-abcde"),)
    assert [finding.kind for finding in hypothesis.initiating_findings] == [
        FindingKind.IMAGE_CHANGE
    ]
    assert [finding.kind for finding in hypothesis.supporting_findings] == [
        FindingKind.FAILURE_EVENT
    ]
    assert hypothesis.findings[0].entity == ref("shop/Deployment/catalog")
    assert case.hypothesis_diagnostics.multi_entity_hypotheses == 1


def test_scheduling_failure_groups_with_changed_workload() -> None:
    changed = deployment("catalog", image="app:1")
    changed["spec"]["template"]["spec"]["nodeSelector"] = {"pool": "missing"}
    source = _rollout_source(changed_body=changed)
    source.event_items.append(
        event("shop/Pod/catalog-rs-abcde", "FailedScheduling", 6, type_="Warning")
    )

    hypothesis = build_case(source).hypotheses[0]

    assert hypothesis.causal_actor == ref("shop/Deployment/catalog")
    assert ref("shop/Pod/catalog-rs-abcde") in hypothesis.manifestations


def test_unchanged_deployment_is_not_promoted_over_pod_local_oom() -> None:
    body = pod("catalog-rs-abcde", "catalog", "catalog-rs")
    body["status"] = {
        "containerStatuses": [
            {
                "name": "catalog",
                "restartCount": 3,
                "lastState": {
                    "terminated": {
                        "reason": "OOMKilled",
                        "finishedAt": at(6).isoformat(),
                    }
                },
            }
        ]
    }
    case = build_case(_rollout_source(pod_body=body))

    assert len(case.hypotheses) == 1
    assert case.hypotheses[0].causal_actor == ref("shop/Pod/catalog-rs-abcde")
    assert case.hypotheses[0].members == (ref("shop/Pod/catalog-rs-abcde"),)


def test_unrelated_siblings_remain_separate_hypotheses() -> None:
    source = _rollout_source(changed_body=deployment("catalog", image="app:2"))
    case = build_case(source)
    candidates = [
        Candidate(
            entity=ref("shop/Deployment/ad"),
            score=5,
            findings=(
                Finding(
                    kind=FindingKind.IMAGE_CHANGE,
                    entity=ref("shop/Deployment/ad"),
                    at=at(4),
                    summary="ad image changed",
                    evidence_ids=("ad-image",),
                    temporal_role=EvidenceTemporalRole.INITIATING,
                ),
            ),
        ),
        Candidate(
            entity=ref("shop/Deployment/cart"),
            score=5,
            findings=(
                Finding(
                    kind=FindingKind.IMAGE_CHANGE,
                    entity=ref("shop/Deployment/cart"),
                    at=at(4),
                    summary="cart image changed",
                    evidence_ids=("cart-image",),
                    temporal_role=EvidenceTemporalRole.INITIATING,
                ),
            ),
        ),
    ]

    result = group_candidates(candidates, case.topology, case.context, RankingConfig())

    assert len(result.hypotheses) == 2
    assert all(len(item.members) == 1 for item in result.hypotheses)


def test_late_upstream_change_does_not_group_with_earlier_manifestation() -> None:
    source = _rollout_source(changed_body=deployment("catalog", image="app:2"))
    case = build_case(source)
    late = case.candidates[0].model_copy(
        update={
            "findings": tuple(
                finding.model_copy(
                    update={
                        "at": at(120),
                        "incident_onset": at(5),
                        "onset_delta_seconds": 6900,
                        "temporal_role": EvidenceTemporalRole.CONSEQUENCE,
                    }
                )
                for finding in case.candidates[0].findings
            )
        }
    )
    pod_candidate = Candidate(
        entity=ref("shop/Pod/catalog-rs-abcde"),
        score=5,
        findings=(
            Finding(
                kind=FindingKind.FAILURE_EVENT,
                entity=ref("shop/Pod/catalog-rs-abcde"),
                at=at(6),
                summary="pod failed",
                evidence_ids=("pod-failure",),
                temporal_role=EvidenceTemporalRole.SUPPORTING,
            ),
        ),
    )

    result = group_candidates([late, pod_candidate], case.topology, case.context, RankingConfig())

    assert len(result.hypotheses) == 2


def test_duplicate_evidence_is_scored_once_but_kept_for_provenance() -> None:
    source = _rollout_source(changed_body=deployment("catalog", image="app:2"))
    source.event_items.append(
        event("shop/Pod/catalog-rs-abcde", "ImagePullBackOff", 6, type_="Warning")
    )
    case = build_case(source)
    actor, manifestation = case.candidates[:2]
    duplicated = manifestation.model_copy(
        update={
            "findings": tuple(
                finding.model_copy(update={"evidence_ids": actor.findings[0].evidence_ids})
                for finding in manifestation.findings
            )
        }
    )

    result = group_candidates([actor, duplicated], case.topology, case.context, RankingConfig())
    hypothesis = result.hypotheses[0]

    assert result.diagnostics.duplicate_evidence_ids_removed == 1
    assert len(hypothesis.findings) == 2
    assert hypothesis.score == actor.score


def test_grouping_is_deterministic_and_preserves_hypothesis_id() -> None:
    source = _rollout_source(changed_body=deployment("catalog", image="app:2"))
    first = build_case(source)
    second = build_case(source)

    assert [item.model_dump(mode="json") for item in first.hypotheses] == [
        item.model_dump(mode="json") for item in second.hypotheses
    ]
    assert first.hypotheses[0].hypothesis_id == second.hypotheses[0].hypothesis_id


def test_ownership_chain_expansion_collapses_deployment_replicaset_and_pod() -> None:
    case = build_case(_rollout_source(changed_body=deployment("catalog", image="app:2")))
    candidates = [
        Candidate(
            entity=ref("shop/Deployment/catalog"),
            score=10,
            findings=(
                Finding(
                    kind=FindingKind.IMAGE_CHANGE,
                    entity=ref("shop/Deployment/catalog"),
                    at=at(5),
                    summary="image changed",
                    evidence_ids=("image-change",),
                    temporal_role=EvidenceTemporalRole.INITIATING,
                ),
            ),
        ),
        Candidate(
            entity=ref("shop/ReplicaSet/catalog-rs"),
            score=2,
            findings=(
                Finding(
                    kind=FindingKind.ROLLOUT_RESTART,
                    entity=ref("shop/ReplicaSet/catalog-rs"),
                    at=at(5),
                    summary="new replica set observed",
                    evidence_ids=("replicaset-created",),
                    temporal_role=EvidenceTemporalRole.INITIATING,
                ),
            ),
        ),
        Candidate(
            entity=ref("shop/Pod/catalog-rs-abcde"),
            score=3,
            findings=(
                Finding(
                    kind=FindingKind.FAILURE_EVENT,
                    entity=ref("shop/Pod/catalog-rs-abcde"),
                    at=at(6),
                    summary="pod failed",
                    evidence_ids=("pod-failure",),
                    temporal_role=EvidenceTemporalRole.SUPPORTING,
                ),
            ),
        ),
    ]

    result = group_candidates(candidates, case.topology, case.context, RankingConfig())

    assert len(result.hypotheses) == 1
    assert result.hypotheses[0].causal_actor == ref("shop/Deployment/catalog")
    assert result.hypotheses[0].members == (
        ref("shop/Deployment/catalog"),
        ref("shop/Pod/catalog-rs-abcde"),
        ref("shop/ReplicaSet/catalog-rs"),
    )


def test_recurring_chaos_schedule_is_actor_and_execution_is_manifestation() -> None:
    target = "Successfully apply chaos for shop/checkout-5d8f7c9b4-abcde"
    source = InMemorySource(
        name="scheduled-chaos",
        alert_items=[alert("RequestLatency", "checkout", 6)],
        versions=[
            version(
                "shop/Deployment/checkout",
                0,
                deployment("checkout"),
            ),
            version(
                "shop/ReplicaSet/checkout-rs",
                0,
                replicaset("checkout-rs", "checkout"),
            ),
            version(
                "shop/Pod/checkout-rs-abcde",
                0,
                pod("checkout-rs-abcde", "checkout", "checkout-rs"),
            ),
            version("shop/Service/checkout", 0, service("checkout")),
        ],
        event_items=[
            event("chaos/Schedule/checkout-delay", "Spawned", 1),
            event(
                "chaos/NetworkChaos/checkout-delay-x1y2z",
                "Applied",
                1,
                message=target,
            ),
        ],
    )

    hypothesis = build_case(source).hypotheses[0]

    assert hypothesis.causal_actor == ref("chaos/Schedule/checkout-delay")
    assert hypothesis.manifestations == (ref("chaos/NetworkChaos/checkout-delay-x1y2z"),)
    assert [finding.kind for finding in hypothesis.initiating_findings] == [
        FindingKind.FAULT_SCHEDULE
    ]
    assert [finding.kind for finding in hypothesis.supporting_findings] == [
        FindingKind.FAULT_INJECTION
    ]


def test_diagnostic_summary_aggregates_without_ground_truth() -> None:
    source = _rollout_source(changed_body=deployment("catalog", image="app:2"))
    diagnostics = [build_case(source).hypothesis_diagnostics]

    assert summarize_diagnostics(diagnostics) == {
        "diagnoses": 1,
        "raw_candidate_count": 1,
        "hypothesis_count": 1,
        "multi_entity_hypotheses": 0,
        "ownership_chains_collapsed": 0,
        "duplicate_evidence_ids_removed": 0,
        "exact_score_ties": 0,
        "structurally_similar_top_hypotheses": 0,
    }


def test_irrelevant_sibling_does_not_change_the_clear_hypothesis() -> None:
    source = _rollout_source(changed_body=deployment("catalog", image="app:2"))
    case = build_case(source)
    primary = case.candidates[0]
    unrelated = Candidate(
        entity=ref("shop/Deployment/unrelated"),
        score=1,
        findings=(
            Finding(
                kind=FindingKind.CONFIG_CHANGE,
                entity=ref("shop/Deployment/unrelated"),
                at=at(4),
                summary="unrelated config changed",
                evidence_ids=("unrelated",),
                temporal_role=EvidenceTemporalRole.INITIATING,
            ),
        ),
    )

    base = group_candidates([primary], case.topology, case.context, RankingConfig())
    expanded = group_candidates([primary, unrelated], case.topology, case.context, RankingConfig())

    assert base.hypotheses[0].causal_actor == expanded.hypotheses[0].causal_actor
    assert base.hypotheses[0].hypothesis_id == expanded.hypotheses[0].hypothesis_id

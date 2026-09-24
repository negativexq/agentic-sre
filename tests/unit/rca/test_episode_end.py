"""Rule m16.ended-manifestation-episode.v1 (contract section 17)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from packages.rca.episode_end import EpisodeEndBasis, assess_ended_episode
from packages.rca.live import LiveSource
from packages.rca.model import (
    CausalHop,
    EliminationConsequence,
    EntityRef,
    EvidenceTemporalRole,
    Finding,
    FindingKind,
    Hypothesis,
    JournalEntry,
    Lifecycle,
    ObjectVersion,
    PodStatusObservation,
    Resolution,
    ResolutionReasonCode,
)
from packages.rca.resolution import EPISODE_END_RULE, resolve_hypotheses
from packages.rca.root_cause_eligibility import RootCauseEligibilities

ONSET = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
GRACE = timedelta(minutes=15)
POD = EntityRef(namespace="shop", kind="Pod", name="worker-abc")


def _failure(minutes: float, *, entity: EntityRef = POD, evidence: str = "event-1") -> Finding:
    return Finding(
        kind=FindingKind.FAILURE_EVENT,
        entity=entity,
        at=ONSET + timedelta(minutes=minutes),
        incident_onset=ONSET,
        onset_delta_seconds=minutes * 60,
        temporal_role=EvidenceTemporalRole.AMBIGUOUS,
        summary="Unhealthy x3: readiness probe failed",
        evidence_ids=(evidence,),
    )


def _manifestation(*findings: Finding) -> Hypothesis:
    return Hypothesis(
        hypothesis_id="hypothesis:worker",
        causal_actor=POD,
        members=(POD,),
        findings=findings,
        supporting_findings=findings,
        causal_explanation="DIRECT",
    )


def _status(
    observed: float, *, ready: bool | None = True, since: float | None = -5, uid: str = "u1"
) -> PodStatusObservation:
    return PodStatusObservation(
        pod=POD,
        uid=uid,
        observed_at=ONSET + timedelta(minutes=observed),
        ready=ready,
        ready_since=ONSET + timedelta(minutes=since) if since is not None else None,
        evidence_id=f"status:{observed}",
    )


def _tombstone(minutes: float, evidence: str = "journal:9") -> dict[EntityRef, list[ObjectVersion]]:
    body: dict[str, Any] = {"kind": "Pod", "metadata": {"name": POD.name, "uid": "u1"}}
    return {
        POD: [
            ObjectVersion(
                entity=POD,
                observed_at=ONSET - timedelta(minutes=30),
                body=body,
                evidence_id="journal:1",
                lifecycle=Lifecycle.CREATED,
            ),
            ObjectVersion(
                entity=POD,
                observed_at=ONSET + timedelta(minutes=minutes),
                body=body,
                evidence_id=evidence,
                lifecycle=Lifecycle.DELETED,
            ),
        ]
    }


def _assess(
    hypothesis: Hypothesis,
    *,
    statuses: tuple[PodStatusObservation, ...] = (),
    history: dict[EntityRef, list[ObjectVersion]] | None = None,
) -> Any:
    return assess_ended_episode(
        hypothesis, history=history or {}, pod_statuses=statuses, onset=ONSET, grace=GRACE
    )


def test_recovered_pod_whose_failures_precede_continuous_readiness_is_ended() -> None:
    ended = _assess(_manifestation(_failure(-10)), statuses=(_status(20),))

    assert ended is not None
    assert ended.basis is EpisodeEndBasis.RECOVERED
    assert ended.ended_at == ONSET - timedelta(minutes=5)
    assert ended.observed_at == ONSET + timedelta(minutes=20)
    assert [check.name for check in ended.preconditions] == [
        "pod_actor",
        "manifestation_only",
        "timed_manifestations",
        "positive_episode_end_recovered",
        "no_overlap",
    ]
    assert all(check.passed for check in ended.preconditions)


def test_readiness_must_cover_onset_plus_grace_and_start_before_onset() -> None:
    hypothesis = _manifestation(_failure(-10))
    # Observed too early to cover onset + grace.
    assert _assess(hypothesis, statuses=(_status(10),)) is None
    # Ready only since after onset.
    assert _assess(hypothesis, statuses=(_status(20, since=2),)) is None
    # Not ready, or readiness unknown.
    assert _assess(hypothesis, statuses=(_status(20, ready=False),)) is None
    assert _assess(hypothesis, statuses=(_status(20, ready=None, since=None),)) is None


def test_failures_after_the_episode_end_overlap_and_block_the_rule() -> None:
    assert _assess(_manifestation(_failure(-3)), statuses=(_status(20),)) is None


def test_disagreeing_observation_of_the_same_pod_breaks_continuity() -> None:
    statuses = (_status(20), _status(10, ready=False, since=8))
    assert _assess(_manifestation(_failure(-10)), statuses=statuses) is None


def test_missing_status_and_untimed_findings_are_neutral() -> None:
    assert _assess(_manifestation(_failure(-10))) is None
    untimed = _failure(-10).model_copy(update={"at": None})
    assert _assess(_manifestation(untimed), statuses=(_status(20),)) is None


def test_journal_tombstone_at_or_before_onset_ends_the_episode() -> None:
    ended = _assess(_manifestation(_failure(-10)), history=_tombstone(-2))

    assert ended is not None
    assert ended.basis is EpisodeEndBasis.TERMINATED
    assert ended.end_evidence_id == "journal:9"


def test_late_or_synthetic_tombstones_do_not_end_the_episode() -> None:
    hypothesis = _manifestation(_failure(-10))
    assert _assess(hypothesis, history=_tombstone(3)) is None
    assert _assess(hypothesis, history=_tombstone(-2, evidence="cluster:missing")) is None


def test_hypotheses_outside_the_manifestation_only_scope_are_untouched() -> None:
    statuses = (_status(20),)
    deployment = EntityRef(namespace="shop", kind="Deployment", name="worker")
    other = _manifestation(_failure(-10)).model_copy(
        update={"causal_actor": deployment, "members": (deployment,)}
    )
    assert _assess(other, statuses=statuses) is None
    foreign = _manifestation(
        _failure(-10, entity=EntityRef(namespace="shop", kind="Pod", name="x"))
    )
    assert _assess(foreign, statuses=statuses) is None
    change = Finding(
        kind=FindingKind.CONFIG_CHANGE,
        entity=POD,
        at=ONSET - timedelta(minutes=20),
        incident_onset=ONSET,
        temporal_role=EvidenceTemporalRole.INITIATING,
        summary="config changed",
        evidence_ids=("change",),
    )
    initiating = _manifestation(_failure(-10), change).model_copy(
        update={"initiating_findings": (change,)}
    )
    assert _assess(initiating, statuses=statuses) is None


def _supported_change() -> Hypothesis:
    config = EntityRef(namespace="shop", kind="ConfigMap", name="settings")
    workload = EntityRef(namespace="shop", kind="Deployment", name="checkout")
    change = Finding(
        kind=FindingKind.CONFIG_CHANGE,
        entity=config,
        at=ONSET - timedelta(minutes=1),
        incident_onset=ONSET,
        onset_delta_seconds=-60,
        temporal_role=EvidenceTemporalRole.INITIATING,
        summary="settings changed",
        evidence_ids=("settings-change",),
    )
    return Hypothesis(
        hypothesis_id="hypothesis:settings",
        causal_actor=config,
        members=(config, workload),
        findings=(change,),
        initiating_findings=(change,),
        causal_paths=((CausalHop(source=config, relation="configures", target=workload),),),
        linked_symptoms=("errors",),
        causal_explanation="PATH",
    )


def test_ended_alternative_no_longer_blocks_resolution_and_is_audited() -> None:
    supported, stale = _supported_change(), _manifestation(_failure(-10))
    assert resolve_hypotheses((supported, stale)).state is Resolution.AMBIGUOUS

    ended = _assess(stale, statuses=(_status(20),))
    eligibilities = RootCauseEligibilities((), {stale.hypothesis_id: ended})
    trace = resolve_hypotheses((supported, stale), root_cause_eligibilities=eligibilities)

    assert trace.state is Resolution.RESOLVED
    assert trace.leading_hypothesis_ids == (supported.hypothesis_id,)
    assert trace.decision_basis == "ROOT_CAUSE_ELIGIBILITY"
    assert not eligibilities.is_root_cause_selectable(stale.hypothesis_id)
    (item,) = trace.eliminations
    assert item.code is ResolutionReasonCode.MANIFESTATION_EPISODE_ENDED_BEFORE_ONSET
    assert (item.rule_id, item.rule_version) == EPISODE_END_RULE
    assert item.consequence is EliminationConsequence.ROOT_INELIGIBILITY
    assert item.mechanism == "EPISODE_TIMING"
    assert item.evidence_ids == ("event-1", "status:20")
    (basis,) = item.time_basis
    assert basis.interval_start == ONSET - timedelta(minutes=5)
    assert basis.interval_end == ONSET + timedelta(minutes=20)
    assert all(check.passed for check in item.preconditions)


def test_ended_alternative_never_makes_an_unsupported_incident_resolved() -> None:
    stale = _manifestation(_failure(-10))
    ended = _assess(stale, statuses=(_status(20),))
    trace = resolve_hypotheses(
        (stale,), root_cause_eligibilities=RootCauseEligibilities((), {stale.hypothesis_id: ended})
    )
    assert trace.state is Resolution.INSUFFICIENT_EVIDENCE


def test_live_source_keeps_the_current_pod_status_that_the_journal_drops() -> None:
    body: dict[str, Any] = {
        "kind": "Pod",
        "metadata": {"name": POD.name, "namespace": "shop", "uid": "u1"},
        "spec": {"containers": [{"name": "app", "image": "app:1"}]},
    }
    journal_body = {
        **body,
        "status": {"conditions": [{"type": "Ready", "status": "False"}]},
    }
    current_body = {
        **body,
        "status": {
            "conditions": [
                {"type": "Ready", "status": "True", "lastTransitionTime": "2026-09-24T11:55:00Z"}
            ]
        },
    }
    source = LiveSource(
        incident="i1",
        alert_items=[],
        journal=[
            JournalEntry(
                object_key="shop/Pod/worker-abc",
                observed_at=ONSET - timedelta(minutes=30),
                body=journal_body,
                version_id=1,
                lifecycle=Lifecycle.CREATED,
            )
        ],
        current_objects=[current_body],
        event_bodies=[],
        observed_at=ONSET + timedelta(minutes=20),
    )

    # Same desired state: the history keeps only the journal version ...
    assert len(source.object_history()[POD]) == 1
    # ... while the current listing's status is still observed.
    statuses = source.pod_status_observations()
    assert [(item.ready, item.evidence_id) for item in statuses] == [
        (False, "journal:1"),
        (True, "cluster:current"),
    ]
    assert statuses[-1].ready_since == ONSET - timedelta(minutes=5)

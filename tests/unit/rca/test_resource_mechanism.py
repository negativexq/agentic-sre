"""Rule m16.resource-pressure.v1 (contract section 18)."""

from __future__ import annotations

import copy
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from packages.rca.live import PROMETHEUS_MAX_QUERY_SPAN, LiveSource
from packages.rca.model import (
    CausalHop,
    EliminationConsequence,
    EntityRef,
    EvidenceTemporalRole,
    Finding,
    FindingKind,
    Hypothesis,
    HypothesisEpistemicState,
    InvestigationQuery,
    Lifecycle,
    ObjectVersion,
    Resolution,
    ResolutionReasonCode,
    ResourcePressure,
)
from packages.rca.resolution import RESOURCE_PRESSURE_RULE, resolve_hypotheses
from packages.rca.resource_mechanism import assess_resource_mechanism

ONSET = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
GRACE = timedelta(minutes=15)
CHANGE_AT = ONSET - timedelta(minutes=2)
NS = "shop"
DEPLOY = EntityRef(namespace=NS, kind="Deployment", name="payment")
RS = EntityRef(namespace=NS, kind="ReplicaSet", name="payment-new")
POD = EntityRef(namespace=NS, kind="Pod", name="payment-new-a")


def _container(memory: str = "512Mi", image: str = "payment:1") -> dict[str, Any]:
    return {"name": "app", "image": image, "resources": {"limits": {"memory": memory}}}


def _deployment(container: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "Deployment",
        "metadata": {"name": DEPLOY.name, "namespace": NS},
        "spec": {"replicas": 1, "template": {"spec": {"containers": [container]}}},
    }


def _owned(kind: str, name: str, owner_kind: str, owner: str, **extra: Any) -> dict[str, Any]:
    return {
        "kind": kind,
        "metadata": {
            "name": name,
            "namespace": NS,
            "ownerReferences": [{"kind": owner_kind, "name": owner, "controller": True}],
            **extra,
        },
    }


def _history(
    before: dict[str, Any], after: dict[str, Any], *, pod_status: dict[str, Any] | None = None
) -> dict[EntityRef, list[ObjectVersion]]:
    replica_set = _owned("ReplicaSet", RS.name, "Deployment", DEPLOY.name)
    replica_set["spec"] = copy.deepcopy(after["spec"])
    pod = _owned(
        "Pod",
        POD.name,
        "ReplicaSet",
        RS.name,
        creationTimestamp=(CHANGE_AT + timedelta(seconds=5)).isoformat(),
    )
    pod["status"] = pod_status or {}
    return {
        DEPLOY: [
            ObjectVersion(
                entity=DEPLOY,
                observed_at=ONSET - timedelta(hours=1),
                body=before,
                evidence_id="journal:1",
                lifecycle=Lifecycle.OBSERVED,
            ),
            ObjectVersion(
                entity=DEPLOY, observed_at=CHANGE_AT, body=after, evidence_id="journal:2"
            ),
        ],
        RS: [
            ObjectVersion(
                entity=RS,
                observed_at=CHANGE_AT,
                body=replica_set,
                evidence_id="journal:3",
                lifecycle=Lifecycle.CREATED,
            )
        ],
        POD: [
            ObjectVersion(
                entity=POD,
                observed_at=CHANGE_AT + timedelta(seconds=5),
                body=pod,
                evidence_id="journal:4",
                lifecycle=Lifecycle.CREATED,
            )
        ],
    }


def _limit_change() -> Hypothesis:
    change = Finding(
        kind=FindingKind.SPEC_CHANGE,
        entity=DEPLOY,
        at=CHANGE_AT,
        incident_onset=ONSET,
        onset_delta_seconds=-120,
        temporal_role=EvidenceTemporalRole.INITIATING,
        summary="spec changed: resources.limits.memory 512Mi -> 128Mi",
        evidence_ids=("journal:1", "journal:2"),
    )
    return Hypothesis(
        hypothesis_id="hypothesis:payment-limits",
        causal_actor=DEPLOY,
        members=(DEPLOY, POD),
        findings=(change,),
        initiating_findings=(change,),
        causal_paths=((CausalHop(source=DEPLOY, relation="owns", target=POD),),),
        linked_symptoms=("errors",),
        causal_explanation="PATH",
    )


def _normal(pod: EntityRef = POD, *, peak: float = 0.31, **overrides: Any) -> ResourcePressure:
    values: dict[str, Any] = {
        "pod": pod,
        "container": "app",
        "resource": "memory",
        "baseline": 0.30,
        "peak": peak,
        "at": ONSET,
        "evidence_id": "prometheus:resource:abc",
        "sample_count": 120,
        "sample_start": ONSET - timedelta(minutes=10),
        "sample_end": ONSET + timedelta(minutes=20),
    }
    values.update(overrides)
    return ResourcePressure(**values)


def _reader(records: Sequence[ResourcePressure]) -> Any:
    def read(pods: Sequence[EntityRef], since: datetime) -> Sequence[ResourcePressure]:
        del since
        return [item for item in records if item.pod in set(pods)]

    return read


def _assess(
    *,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    records: Sequence[ResourcePressure] = (),
    findings: Sequence[Finding] = (),
    pod_status: dict[str, Any] | None = None,
    hypothesis: Hypothesis | None = None,
) -> Any:
    history = _history(
        before or _deployment(_container("512Mi")),
        after or _deployment(_container("128Mi")),
        pod_status=pod_status,
    )
    return assess_resource_mechanism(
        hypothesis or _limit_change(),
        history=history,
        findings=findings,
        read_pressure=_reader(records),
        onset=ONSET,
        grace=GRACE,
    )


def test_lowered_limit_with_normal_measurements_is_a_mechanism_mismatch() -> None:
    mismatch = _assess(records=[_normal()])

    assert mismatch is not None
    assert mismatch.lowered == (("app", "memory"),)
    assert mismatch.pods == (POD,)
    (coverage,) = mismatch.coverage
    assert coverage.window_start == CHANGE_AT + timedelta(seconds=5)
    assert coverage.window_end == ONSET + GRACE
    assert all(check.passed for check in mismatch.preconditions)


def test_only_resource_only_limit_reductions_require_the_mechanism() -> None:
    records = [_normal()]
    # The change also swapped the image: resources are not the only premise.
    assert (
        _assess(after=_deployment(_container("128Mi", image="payment:2")), records=records) is None
    )
    # Raising a limit cannot cause pressure.
    assert _assess(after=_deployment(_container("1Gi")), records=records) is None


def test_missing_partial_or_abnormal_measurements_never_contradict() -> None:
    assert _assess(records=[]) is None
    assert _assess(records=[_normal(peak=0.95)]) is None
    assert _assess(records=[_normal(sample_end=ONSET + timedelta(minutes=5))]) is None
    assert _assess(records=[_normal(sample_start=ONSET)]) is None
    assert _assess(records=[_normal(sample_count=1)]) is None


def test_positive_pressure_evidence_on_bound_pods_blocks_the_rule() -> None:
    oom = {"containerStatuses": [{"lastState": {"terminated": {"reason": "OOMKilled"}}}]}
    assert _assess(records=[_normal()], pod_status=oom) is None
    evicted = Finding(
        kind=FindingKind.FAILURE_EVENT,
        entity=POD,
        at=ONSET,
        summary="Evicted",
        evidence_ids=("event:evicted",),
        details={"reason": "Evicted"},
    )
    assert _assess(records=[_normal()], findings=[evicted]) is None


def test_non_deployment_actors_are_not_bound_in_v1() -> None:
    statefulset = EntityRef(namespace=NS, kind="StatefulSet", name="payment")
    hypothesis = _limit_change().model_copy(update={"causal_actor": statefulset})
    assert _assess(records=[_normal()], hypothesis=hypothesis) is None


def _other_supported() -> Hypothesis:
    config = EntityRef(namespace=NS, kind="ConfigMap", name="payment-settings")
    change = Finding(
        kind=FindingKind.CONFIG_CHANGE,
        entity=config,
        at=ONSET - timedelta(minutes=1),
        incident_onset=ONSET,
        onset_delta_seconds=-60,
        temporal_role=EvidenceTemporalRole.INITIATING,
        summary="payment-settings changed",
        evidence_ids=("config-change",),
    )
    return Hypothesis(
        hypothesis_id="hypothesis:payment-settings",
        causal_actor=config,
        members=(config, DEPLOY),
        findings=(change,),
        initiating_findings=(change,),
        causal_paths=((CausalHop(source=config, relation="configures", target=DEPLOY),),),
        linked_symptoms=("errors",),
        causal_explanation="PATH",
    )


def test_mismatch_contradicts_the_limit_hypothesis_and_resolves_to_the_other() -> None:
    limits, settings = _limit_change(), _other_supported()
    assert resolve_hypotheses((limits, settings)).state is Resolution.AMBIGUOUS

    mismatch = _assess(records=[_normal()])
    trace = resolve_hypotheses(
        (limits, settings), mechanism_mismatches={limits.hypothesis_id: mismatch}
    )

    assert trace.state is Resolution.RESOLVED
    assert trace.leading_hypothesis_ids == (settings.hypothesis_id,)
    assert trace.decision_basis == "VALID_CONTRADICTION"
    (item,) = trace.eliminations
    assert item.code is ResolutionReasonCode.OBSERVED_NORMAL_MECHANISM_MISMATCH
    assert (item.rule_id, item.rule_version) == RESOURCE_PRESSURE_RULE
    assert item.consequence is EliminationConsequence.CONTRADICTION
    assert item.mechanism == "RESOURCE_PRESSURE"
    assert item.targets == (DEPLOY.canonical, POD.canonical)
    assert item.evidence_ids == ("journal:1", "journal:2", "prometheus:resource:abc")
    assert item.coverage_basis == "1/1 pod x resource series observed normal"
    audit = next(a for a in trace.hypothesis_audits if a.hypothesis_id == limits.hypothesis_id)
    assert audit.epistemic_state is HypothesisEpistemicState.CONTRADICTED
    assert audit.plausible is False


class _Prometheus:
    def __init__(self, fail_for: str | None = None) -> None:
        self.windows: list[tuple[datetime, datetime]] = []
        self.fail_for = fail_for

    def query_resource_pressure(
        self, target: EntityRef, query: InvestigationQuery
    ) -> tuple[ResourcePressure, ...]:
        assert query.start is not None and query.end is not None
        self.windows.append((query.start, query.end))
        if target.name == self.fail_for:
            raise ConnectionError("prometheus down")
        return (_normal(target),)


@pytest.mark.parametrize("cutoff_minutes", [20, 180])
def test_live_resource_reads_are_bounded_and_failures_are_missing_data(
    cutoff_minutes: int,
) -> None:
    prometheus = _Prometheus(fail_for="payment-new-b")
    source = LiveSource(
        incident="i1",
        alert_items=[],
        journal=[],
        current_objects=[],
        event_bodies=[],
        observed_at=ONSET + timedelta(minutes=cutoff_minutes),
        prometheus_reader=prometheus,  # type: ignore[arg-type]
    )
    other = EntityRef(namespace=NS, kind="Pod", name="payment-new-b")

    records = source.resource_pressure([POD, other, DEPLOY], ONSET - timedelta(minutes=5))

    assert [item.pod for item in records] == [POD]
    assert len(prometheus.windows) == 2  # the Deployment is never queried
    for start, end in prometheus.windows:
        assert start == ONSET - timedelta(minutes=10)
        assert end - start <= PROMETHEUS_MAX_QUERY_SPAN
        assert end <= source.observed_at

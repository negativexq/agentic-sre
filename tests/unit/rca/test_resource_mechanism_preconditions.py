"""A2 normal-coverage maturity uses the M19-5.1 tri-state contract (M19-5.3)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

import pytest
from test_resource_mechanism import (
    CHANGE_AT,
    DEPLOY,
    GRACE,
    NS,
    ONSET,
    POD,
    _container,
    _deployment,
    _history,
    _limit_change,
    _normal,
    _other_supported,
    _reader,
)

from packages.rca.engine import build_case, diagnose_case
from packages.rca.epistemic_digest import diagnosis_epistemic_digest
from packages.rca.model import (
    Alert,
    Confidence,
    Diagnosis,
    EntityRef,
    Finding,
    FindingKind,
    Hypothesis,
    HypothesisEpistemicState,
    PreconditionAuditReason,
    PreconditionResult,
    PreconditionStatus,
    ProviderReadFailure,
    Resolution,
    ResolutionTrace,
    ResourcePressure,
    Symptoms,
)
from packages.rca.resolution import resolve_hypotheses
from packages.rca.resource_mechanism import (
    RULE_ID,
    ResourceMechanismEvaluations,
    evaluate_resource_mechanisms,
)
from packages.rca.source import InMemorySource

BOUNDARY = ONSET + GRACE
EARLY = ONSET + timedelta(minutes=8)  # R_early: before maturity
LATE = ONSET + timedelta(minutes=20)  # after maturity
HYPOTHESIS_ID = "hypothesis:payment-limits"
FAILURE = ProviderReadFailure(
    capability="resource_pressure", error_type="ConnectionError", error_message="prometheus down"
)


def _partial(**overrides: Any) -> ResourcePressure:
    """A normal-looking series that has not yet reached the maturity boundary."""
    return _normal(sample_end=EARLY, **overrides)


def _evaluate(
    *,
    records: Sequence[ResourcePressure] | ProviderReadFailure = (),
    evaluation_at: datetime | None,
    after: dict[str, Any] | None = None,
    findings: Sequence[Finding] = (),
    pod_status: dict[str, Any] | None = None,
    hypothesis: Hypothesis | None = None,
    history: Any = None,
) -> ResourceMechanismEvaluations:
    def read(pods: Sequence[EntityRef], since: datetime) -> Any:
        if isinstance(records, ProviderReadFailure):
            return records
        return _reader(records)(pods, since)

    return evaluate_resource_mechanisms(
        (hypothesis or _limit_change(),),
        history=history
        or _history(
            _deployment(_container("512Mi")),
            after or _deployment(_container("128Mi")),
            pod_status=pod_status,
        ),
        findings=findings,
        read_pressure=read,
        onset=ONSET,
        grace=GRACE,
        evaluation_at=evaluation_at,
    )


def _nothing(result: ResourceMechanismEvaluations) -> bool:
    return not result.mismatches and not result.preconditions and not result.reasons


@pytest.mark.parametrize(
    "records",
    [
        pytest.param((), id="missing"),
        pytest.param((_partial(),), id="partial-end"),
        pytest.param((_normal(sample_start=ONSET),), id="partial-start"),
        pytest.param((_normal(sample_count=1),), id="single-sample"),
        pytest.param(FAILURE, id="provider-error"),
    ],
)
def test_incomplete_normal_coverage_before_maturity_is_pending_at_onset_plus_grace(
    records: Any,
) -> None:
    result = _evaluate(records=records, evaluation_at=EARLY)

    assert result.mismatches == {}
    assert result.reasons == {}
    (pending,) = result.preconditions[HYPOTHESIS_ID]
    assert pending == PreconditionResult.pending(BOUNDARY)
    assert pending.status is PreconditionStatus.PENDING
    assert pending.not_before == ONSET + GRACE


def test_positive_pressure_evidence_is_never_pending() -> None:
    oom = {"containerStatuses": [{"lastState": {"terminated": {"reason": "OOMKilled"}}}]}
    evicted = Finding(
        kind=FindingKind.FAILURE_EVENT,
        entity=POD,
        at=ONSET,
        summary="Evicted",
        evidence_ids=("event:evicted",),
        details={"reason": "Evicted"},
    )
    pressure = Finding(
        kind=FindingKind.RESOURCE_PRESSURE,
        entity=POD,
        at=ONSET,
        summary="memory pressure",
        evidence_ids=("prometheus:resource:abc",),
    )
    for at in (EARLY, LATE):
        # A measured peak at the threshold is positive pressure even with partial coverage.
        assert _nothing(_evaluate(records=[_partial(peak=0.95)], evaluation_at=at))
        assert _nothing(_evaluate(records=[_partial(), _partial(peak=0.95)], evaluation_at=at))
        assert _nothing(_evaluate(records=[], pod_status=oom, evaluation_at=at))
        assert _nothing(_evaluate(records=[], findings=[evicted], evaluation_at=at))
        assert _nothing(_evaluate(records=[], findings=[pressure], evaluation_at=at))


def test_prerequisites_and_bound_pods_are_required_before_pending() -> None:
    statefulset = EntityRef(namespace=NS, kind="StatefulSet", name="payment")
    history = _history(_deployment(_container("512Mi")), _deployment(_container("128Mi")))
    # A Pod whose creation time is unknown makes the bound set unknowable.
    history[POD] = [
        version.model_copy(update={"body": {**version.body, "metadata": {}}})
        for version in history[POD]
    ]
    cases: list[dict[str, Any]] = [
        # R2: not a resource-only change.
        {"after": _deployment(_container("128Mi", image="payment:2"))},
        # R3: raised limit.
        {"after": _deployment(_container("1Gi"))},
        # R1: the actor is not a bindable Deployment.
        {"hypothesis": _limit_change().model_copy(update={"causal_actor": statefulset})},
        # R1: no initiating premise.
        {"hypothesis": _limit_change().model_copy(update={"initiating_findings": ()})},
        # Bound Pod set not positively determined.
        {"history": history},
    ]
    for case in cases:
        for at in (EARLY, LATE):
            assert _nothing(_evaluate(records=[], evaluation_at=at, **case))


def test_after_deadline_missing_and_partial_coverage_are_neutral_audit_reasons() -> None:
    expected: dict[str, tuple[Any, PreconditionAuditReason]] = {
        "missing": ((), PreconditionAuditReason.NO_DATA_AFTER_DEADLINE),
        "provider-error": (FAILURE, PreconditionAuditReason.NO_DATA_AFTER_DEADLINE),
        "partial": ((_partial(),), PreconditionAuditReason.PARTIAL_COVERAGE_AFTER_DEADLINE),
        "duplicate": (
            (_normal(), _normal()),
            PreconditionAuditReason.PARTIAL_COVERAGE_AFTER_DEADLINE,
        ),
    }
    for name, (records, reason) in expected.items():
        for at in (BOUNDARY, LATE):  # the maturity boundary itself is inclusive
            result = _evaluate(records=records, evaluation_at=at)
            assert result.mismatches == {}, name
            assert result.preconditions == {}, name
            assert result.reasons == {HYPOTHESIS_ID: (reason,)}, name


def test_partial_is_distinguished_from_missing_per_required_series() -> None:
    other = EntityRef(namespace=NS, kind="Pod", name="payment-new-b")
    history = _history(_deployment(_container("512Mi")), _deployment(_container("128Mi")))
    history[other] = [
        version.model_copy(
            update={
                "entity": other,
                "body": {
                    **version.body,
                    "metadata": {**version.body["metadata"], "name": other.name},
                },
                "evidence_id": "journal:5",
            }
        )
        for version in history[POD]
    ]
    # One Pod fully normal, the other unmeasured: some coverage exists, so PARTIAL.
    result = _evaluate(records=[_normal()], evaluation_at=LATE, history=history)
    assert result.reasons == {
        HYPOTHESIS_ID: (PreconditionAuditReason.PARTIAL_COVERAGE_AFTER_DEADLINE,)
    }
    assert result.mismatches == {}
    early = _evaluate(records=[_normal()], evaluation_at=EARLY, history=history)
    assert early.preconditions == {HYPOTHESIS_ID: (PreconditionResult.pending(BOUNDARY),)}


def test_full_normal_coverage_still_produces_the_unchanged_mismatch() -> None:
    result = _evaluate(records=[_normal()], evaluation_at=LATE)

    assert result.preconditions == {}
    assert result.reasons == {}
    mismatch = result.mismatches[HYPOTHESIS_ID]
    assert mismatch.lowered == (("app", "memory"),)
    assert mismatch.pods == (POD,)
    assert mismatch.boundary == BOUNDARY
    (coverage,) = mismatch.coverage
    assert coverage.window_start == CHANGE_AT + timedelta(seconds=5)
    assert coverage.window_end == BOUNDARY


def test_unknown_evaluation_time_records_no_maturity_state() -> None:
    for records in ((), (_partial(),), FAILURE):
        assert _nothing(_evaluate(records=records, evaluation_at=None))


def test_pressure_is_read_once_per_in_scope_hypothesis() -> None:
    calls: list[tuple[tuple[EntityRef, ...], datetime]] = []

    def read(pods: Sequence[EntityRef], since: datetime) -> Sequence[ResourcePressure]:
        calls.append((tuple(pods), since))
        return []

    evaluate_resource_mechanisms(
        (_limit_change(), _other_supported()),
        history=_history(_deployment(_container("512Mi")), _deployment(_container("128Mi"))),
        findings=(),
        read_pressure=read,
        onset=ONSET,
        grace=GRACE,
        evaluation_at=EARLY,
    )

    assert calls == [((POD,), max(CHANGE_AT, ONSET - timedelta(minutes=5)))]


def _diagnosis(hypothesis: Hypothesis, trace: ResolutionTrace) -> Diagnosis:
    return Diagnosis(
        incident_id="synthetic",
        root_cause=None,
        confidence=Confidence.UNVERIFIED,
        resolution=trace.state,
        summary="synthetic",
        symptoms=Symptoms(onset=ONSET, last_seen=ONSET, services=(), namespaces=(), alert_names=()),
        hypothesis=hypothesis,
        resolution_trace=trace,
    )


@pytest.mark.parametrize(
    ("records", "at"),
    [
        pytest.param((), EARLY, id="pending-missing"),
        pytest.param((_partial(),), EARLY, id="pending-partial"),
        pytest.param((), LATE, id="deadline-missing"),
        pytest.param((_partial(),), LATE, id="deadline-partial"),
    ],
)
def test_pending_and_deadline_gaps_never_contradict_eliminate_or_change_the_digest(
    records: Sequence[ResourcePressure], at: datetime
) -> None:
    limits, settings = _limit_change(), _other_supported()
    evaluated = _evaluate(records=records, evaluation_at=at)
    assert evaluated.mismatches == {}
    baseline = resolve_hypotheses((limits, settings))
    trace = resolve_hypotheses(
        (limits, settings),
        mechanism_mismatches=evaluated.mismatches,
        rule_preconditions={
            (key, RULE_ID): value for key, value in evaluated.preconditions.items()
        },
        precondition_reasons={(key, RULE_ID): value for key, value in evaluated.reasons.items()},
    )

    assert baseline.state is trace.state is Resolution.AMBIGUOUS
    assert trace.eliminations == baseline.eliminations == ()
    assert trace.eliminated_hypotheses == baseline.eliminated_hypotheses == ()
    audit = next(a for a in trace.hypothesis_audits if a.hypothesis_id == limits.hypothesis_id)
    before = next(a for a in baseline.hypothesis_audits if a.hypothesis_id == limits.hypothesis_id)
    # Not contradicted, not made root-ineligible, and not read as healthy/normal.
    assert audit.epistemic_state is before.epistemic_state
    assert audit.epistemic_state is not HypothesisEpistemicState.CONTRADICTED
    assert audit.plausible is before.plausible is True
    assert audit.contradictory_evidence_ids == before.contradictory_evidence_ids == ()
    (recorded,) = audit.precondition_audit
    assert recorded.rule_id == RULE_ID
    if at < BOUNDARY:
        assert recorded.result == PreconditionResult.pending(BOUNDARY)
    else:
        assert recorded.result is None and recorded.reason is not None
    assert diagnosis_epistemic_digest(_diagnosis(limits, trace)) == diagnosis_epistemic_digest(
        _diagnosis(limits, baseline)
    )


def _engine_source(cutoff: datetime, records: Sequence[ResourcePressure] = ()) -> InMemorySource:
    history = _history(_deployment(_container("512Mi")), _deployment(_container("128Mi")))
    return InMemorySource(
        name="a2-maturity",
        alert_items=[
            Alert(
                name="RequestErrorRate",
                service=DEPLOY.name,
                namespace=NS,
                starts_at=ONSET,
                labels={"alertname": "RequestErrorRate", "service_name": DEPLOY.name},
            )
        ],
        versions=[version for versions in history.values() for version in versions],
        pressure_items=list(records),
        cutoff=cutoff,
    )


def test_engine_classifies_a2_maturity_at_the_observation_cutoff() -> None:
    early = build_case(_engine_source(EARLY))
    late = build_case(_engine_source(LATE))
    covered = build_case(_engine_source(LATE, [_normal()]))
    (hypothesis,) = early.hypotheses
    assert hypothesis.causal_actor == DEPLOY
    key = (hypothesis.hypothesis_id, RULE_ID)

    assert early.context.window_end == EARLY
    assert early.rule_preconditions == {key: (PreconditionResult.pending(BOUNDARY),)}
    assert early.precondition_reasons == {}
    assert early.mechanism_mismatches == {}
    assert late.rule_preconditions == {}
    assert late.precondition_reasons == {key: (PreconditionAuditReason.NO_DATA_AFTER_DEADLINE,)}
    assert late.mechanism_mismatches == {}
    assert covered.rule_preconditions == {}
    assert covered.precondition_reasons == {}
    assert set(covered.mechanism_mismatches) == {hypothesis.hypothesis_id}

    early_trace, late_trace = diagnose_case(early), diagnose_case(late)
    for diagnosis in (early_trace, late_trace):
        trace = diagnosis.resolution_trace
        assert trace is not None
        (audit,) = trace.hypothesis_audits
        assert audit.epistemic_state is not HypothesisEpistemicState.CONTRADICTED
        assert trace.eliminations == ()
        (recorded,) = audit.precondition_audit
        assert recorded.rule_id == RULE_ID
    # Removing the A2 maturity metadata leaves the epistemic digest unchanged.
    for diagnosis in (early_trace, late_trace):
        trace = diagnosis.resolution_trace
        assert trace is not None
        stripped = diagnosis.model_copy(
            update={
                "resolution_trace": trace.model_copy(
                    update={
                        "hypothesis_audits": tuple(
                            audit.model_copy(update={"precondition_audit": ()})
                            for audit in trace.hypothesis_audits
                        )
                    }
                )
            }
        )
        assert stripped != diagnosis
        assert diagnosis_epistemic_digest(stripped) == diagnosis_epistemic_digest(diagnosis)
    covered_trace = diagnose_case(covered).resolution_trace
    assert covered_trace is not None
    assert covered_trace.hypothesis_audits[0].epistemic_state is (
        HypothesisEpistemicState.CONTRADICTED
    )

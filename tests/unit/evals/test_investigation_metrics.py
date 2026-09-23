from __future__ import annotations

from packages.evals.investigation_metrics import derive_investigation_metrics
from packages.rca.model import (
    Confidence,
    Diagnosis,
    EntityRef,
    GapDimension,
    GapOutcomeKind,
    GapResolvability,
    HypothesisEpistemicState,
    InvestigationAction,
    InvestigationActionAudit,
    InvestigationExecutionStatus,
    InvestigationGapState,
    InvestigationHypothesisState,
    InvestigationObservation,
    InvestigationQuery,
    InvestigationResult,
    InvestigationStopReason,
    Resolution,
    Symptoms,
)


def _diagnosis(resolution: Resolution) -> Diagnosis:
    return Diagnosis(
        incident_id="incident-1",
        root_cause=None,
        confidence=Confidence.UNVERIFIED,
        resolution=resolution,
        summary="test diagnosis",
        symptoms=Symptoms(
            onset=None,
            last_seen=None,
            services=(),
            namespaces=(),
            alert_names=(),
        ),
    )


def _audit(
    *,
    turn: int,
    capability: str = "events",
    target: EntityRef | None = None,
    status: InvestigationExecutionStatus = InvestigationExecutionStatus.SUCCEEDED,
    authorization: str = "AUTHORIZED",
    outcome: GapOutcomeKind | None = GapOutcomeKind.SUPPORTS,
    returned: tuple[str, ...] = (),
    new: tuple[str, ...] = (),
    known: tuple[str, ...] = (),
    finding_ids: tuple[str, ...] = (),
    before_state: HypothesisEpistemicState = HypothesisEpistemicState.UNRESOLVED,
    after_state: HypothesisEpistemicState = HypothesisEpistemicState.UNRESOLVED,
    before_resolution: Resolution = Resolution.AMBIGUOUS,
    after_resolution: Resolution = Resolution.AMBIGUOUS,
    before_gap: bool = True,
    after_gap: bool = True,
    progress: str = "NO_PROGRESS",
) -> InvestigationActionAudit:
    target = target or EntityRef(kind="Deployment", name="api", namespace="shop")
    hypothesis_before = InvestigationHypothesisState(
        hypothesis_id="h1", actor=target, state=before_state
    )
    hypothesis_after = InvestigationHypothesisState(
        hypothesis_id="h1", actor=target, state=after_state
    )
    gap_before = (
        (
            InvestigationGapState(
                gap_id="gap-1",
                dimension=GapDimension.FAILURE_ONSET,
                missing_fact="when did it fail",
                resolvability=GapResolvability.RESOLVABLE,
            ),
        )
        if before_gap
        else ()
    )
    gap_after = (
        (
            InvestigationGapState(
                gap_id="gap-1",
                dimension=GapDimension.FAILURE_ONSET,
                missing_fact="when did it fail",
                resolvability=GapResolvability.RESOLVABLE,
            ),
        )
        if after_gap
        else ()
    )
    return InvestigationActionAudit(
        turn_index=turn,
        action=InvestigationAction(
            action="inspect",
            gap_id="gap-1",
            capability=capability,
            target=target,
            query=InvestigationQuery(limit=8),
        ),
        authorization_result=authorization,  # type: ignore[arg-type]
        backend_execution_status=status,
        observation_id=f"obs-{turn}"
        if status is not InvestigationExecutionStatus.NOT_EXECUTED
        else None,
        observation_outcome=outcome,
        returned_evidence_refs=returned,
        new_evidence_refs=new,
        already_known_refs=known,
        normalized_finding_ids=finding_ids,
        resolution_before=before_resolution,
        resolution_after=after_resolution,
        hypothesis_states_before=(hypothesis_before,),
        hypothesis_states_after=(hypothesis_after,),
        gap_states_before=gap_before,
        gap_states_after=gap_after,
        decision_state_changed=(
            before_state is not after_state
            or before_resolution is not after_resolution
            or before_gap != after_gap
        ),
        progress_classification=progress,
    )


def test_metrics_v1_derive_usefulness_novelty_duplicates_and_decision_impact() -> None:
    first = _audit(
        turn=1,
        returned=("e1",),
        new=("e1",),
        finding_ids=("finding-1",),
        after_state=HypothesisEpistemicState.SUPPORTED,
        after_resolution=Resolution.RESOLVED,
        after_gap=False,
    )
    duplicate = _audit(
        turn=2,
        returned=("e1",),
        known=("e1",),
        before_state=HypothesisEpistemicState.SUPPORTED,
        after_state=HypothesisEpistemicState.SUPPORTED,
        before_resolution=Resolution.RESOLVED,
        after_resolution=Resolution.RESOLVED,
        before_gap=False,
        after_gap=False,
    )
    no_data = _audit(
        turn=3,
        capability="logs",
        outcome=GapOutcomeKind.NO_DATA,
    )
    rejected = _audit(
        turn=4,
        capability="events",
        status=InvestigationExecutionStatus.NOT_EXECUTED,
        authorization="REJECTED",
        outcome=None,
    )
    result = InvestigationResult(
        diagnosis=_diagnosis(Resolution.RESOLVED),
        initial_diagnosis=_diagnosis(Resolution.AMBIGUOUS),
        initial_resolution=Resolution.AMBIGUOUS,
        final_resolution=Resolution.RESOLVED,
        model_calls=0,
        tool_calls=3,
        rejected_actions=1,
        stop_reason=InvestigationStopReason.RESOLVED,
        observations=(
            InvestigationObservation(
                observation_id="obs-1",
                gap_id="gap-1",
                capability="events",
                target=EntityRef(kind="Deployment", name="api", namespace="shop"),
                outcome=GapOutcomeKind.SUPPORTS,
            ),
            InvestigationObservation(
                observation_id="obs-2",
                gap_id="gap-1",
                capability="events",
                target=EntityRef(kind="Deployment", name="api", namespace="shop"),
                outcome=GapOutcomeKind.SUPPORTS,
            ),
            InvestigationObservation(
                observation_id="obs-3",
                gap_id="gap-1",
                capability="logs",
                target=EntityRef(kind="Deployment", name="api", namespace="shop"),
                outcome=GapOutcomeKind.NO_DATA,
            ),
        ),
        action_audits=(first, duplicate, no_data, rejected),
        resolved_during_investigation=True,
    )

    metrics = derive_investigation_metrics(result)

    assert metrics.metric_version == "m14.v1"
    assert metrics.tool_calls == 3
    assert metrics.unique_observations == 3
    assert metrics.useful_call_count == 1
    assert metrics.useful_call_rate == 1 / 3
    assert metrics.novel_evidence_count == 1
    assert metrics.novel_evidence_rate == 1 / 3
    assert metrics.duplicate_read_count == 1
    assert metrics.duplicate_read_rate == 1 / 3
    assert metrics.no_data_count == 1
    assert metrics.no_data_rate == 1 / 3
    assert metrics.decision_relevant_call_count == 1
    assert metrics.hypotheses_changed == 1
    assert metrics.gap_state_changes == 1
    assert metrics.resolution_transitions == 1
    assert metrics.resolved_during_investigation is True
    assert metrics.reads_to_resolution == 1
    assert metrics.invalid_actions == 1
    assert metrics.rejected_actions == 1
    assert metrics.out_of_policy_execution == 0


def test_metrics_report_safety_execution_and_undefined_zero_call_rates() -> None:
    forbidden = _audit(
        turn=1,
        capability="secret_write",
        target=EntityRef(kind="Secret", name="credentials", namespace="shop"),
    )
    result = InvestigationResult(
        diagnosis=_diagnosis(Resolution.INSUFFICIENT_EVIDENCE),
        initial_diagnosis=_diagnosis(Resolution.INSUFFICIENT_EVIDENCE),
        initial_resolution=Resolution.INSUFFICIENT_EVIDENCE,
        final_resolution=Resolution.INSUFFICIENT_EVIDENCE,
        tool_calls=1,
        stop_reason=InvestigationStopReason.TOOL_ERROR,
        action_audits=(forbidden,),
    )

    metrics = derive_investigation_metrics(result)

    assert metrics.write_execution == 1
    assert metrics.secret_access == 1
    assert metrics.useful_call_rate == 0
    assert metrics.novel_evidence_rate == 0

    no_calls = InvestigationResult(
        diagnosis=_diagnosis(Resolution.INSUFFICIENT_EVIDENCE),
        initial_diagnosis=_diagnosis(Resolution.INSUFFICIENT_EVIDENCE),
        initial_resolution=Resolution.INSUFFICIENT_EVIDENCE,
        final_resolution=Resolution.INSUFFICIENT_EVIDENCE,
        stop_reason=InvestigationStopReason.NO_RESOLVABLE_GAP,
    )
    empty_metrics = derive_investigation_metrics(no_calls)
    assert empty_metrics.useful_call_rate is None
    assert empty_metrics.decision_relevant_call_rate is None
    assert empty_metrics.duplicate_read_rate is None


def test_metrics_reject_inconsistent_persisted_tool_accounting() -> None:
    result = InvestigationResult(
        diagnosis=_diagnosis(Resolution.INSUFFICIENT_EVIDENCE),
        initial_diagnosis=_diagnosis(Resolution.INSUFFICIENT_EVIDENCE),
        initial_resolution=Resolution.INSUFFICIENT_EVIDENCE,
        final_resolution=Resolution.INSUFFICIENT_EVIDENCE,
        tool_calls=1,
        stop_reason=InvestigationStopReason.NO_RESOLVABLE_GAP,
    )

    try:
        derive_investigation_metrics(result)
    except ValueError as error:
        assert "executed-action count" in str(error)
    else:
        raise AssertionError("inconsistent tool accounting must fail closed")

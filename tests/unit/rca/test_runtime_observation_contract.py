from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from packages.rca.engine import build_case
from packages.rca.investigation.normalizers import normalize_observation
from packages.rca.model import (
    EntityRef,
    GapDimension,
    GapOutcomeKind,
    InformationGap,
    InvestigationObservation,
    RuntimeEvidencePillar,
    RuntimeObservationContext,
    RuntimeObservationState,
    RuntimeQueryDescriptor,
)
from packages.rca.source import InMemorySource

T0 = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


def _query() -> RuntimeQueryDescriptor:
    return RuntimeQueryDescriptor(
        descriptor_id="sha256:bounded-template-target-window",
        template_id="prometheus.resource_pressure.v1",
        target=EntityRef(namespace="shop", kind="Pod", name="checkout-1"),
        requested_start=T0 - timedelta(minutes=5),
        requested_end=T0,
        effective_start=T0 - timedelta(minutes=5),
        effective_end=T0,
        limit=16,
    )


def _context(state: RuntimeObservationState) -> RuntimeObservationContext:
    return RuntimeObservationContext(
        pillar=RuntimeEvidencePillar.PROMETHEUS,
        capability="resource_pressure",
        state=state,
        query=_query(),
        source_observation_ids=(
            ("prometheus:sample:1",)
            if state
            in {
                RuntimeObservationState.OBSERVED_NORMAL,
                RuntimeObservationState.OBSERVED_ABNORMAL,
            }
            else ()
        ),
    )


def test_runtime_outcome_states_are_distinct_and_round_trip() -> None:
    states = tuple(RuntimeObservationState)
    assert states == (
        RuntimeObservationState.UNKNOWN,
        RuntimeObservationState.NO_DATA,
        RuntimeObservationState.OBSERVED_NORMAL,
        RuntimeObservationState.OBSERVED_ABNORMAL,
    )
    for state in states:
        observation = InvestigationObservation(
            observation_id=f"runtime:{state.value}",
            gap_id="gap:resource",
            capability="resource_pressure",
            target=_query().target,
            outcome=GapOutcomeKind.NO_DATA
            if state is RuntimeObservationState.NO_DATA
            else GapOutcomeKind.UNKNOWN,
            runtime=_context(state),
        )
        restored = InvestigationObservation.model_validate(observation.model_dump(mode="json"))
        assert restored.runtime is not None
        assert restored.runtime.state is state
        assert restored.runtime.query.target == observation.target


def test_runtime_query_descriptor_is_semantic_bounded_and_native_query_free() -> None:
    descriptor = _query()
    assert descriptor.template_id == "prometheus.resource_pressure.v1"
    assert descriptor.limit <= 32
    assert "promql" not in descriptor.model_dump(mode="json")
    with pytest.raises(ValidationError, match="at most 3600 seconds"):
        RuntimeQueryDescriptor(
            descriptor_id="query:long",
            template_id="tempo.traces.v1",
            target=EntityRef(namespace="shop", kind="Deployment", name="checkout"),
            requested_start=T0 - timedelta(hours=2),
            requested_end=T0,
            effective_start=T0 - timedelta(hours=2),
            effective_end=T0,
            limit=8,
        )


def test_runtime_context_rejects_pillar_mismatch_and_rca_authority_fields() -> None:
    with pytest.raises(ValidationError, match="does not belong"):
        RuntimeObservationContext(
            pillar=RuntimeEvidencePillar.TEMPO,
            capability="logs",
            state=RuntimeObservationState.NO_DATA,
            query=_query(),
        )

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RuntimeObservationContext.model_validate(
            {
                **_context(RuntimeObservationState.NO_DATA).model_dump(mode="python"),
                "root_cause": "shop/Deployment/checkout",
            }
        )


def test_runtime_metadata_alone_cannot_create_findings_or_change_rca() -> None:
    case = build_case(InMemorySource(name="runtime-context-only"))
    findings_before = case.findings
    hypotheses_before = case.hypotheses
    candidates_before = case.candidates
    gap = InformationGap(
        gap_id="gap:runtime",
        dimension=GapDimension.FAILURE_ONSET,
        missing_fact="whether the target has a typed runtime observation",
    )
    observation = InvestigationObservation(
        observation_id="runtime:metadata-only",
        gap_id=gap.gap_id,
        capability="runtime_traces",
        target=EntityRef(namespace="shop", kind="Deployment", name="checkout"),
        outcome=GapOutcomeKind.UNKNOWN,
        runtime=RuntimeObservationContext(
            pillar=RuntimeEvidencePillar.TEMPO,
            capability="runtime_traces",
            state=RuntimeObservationState.OBSERVED_ABNORMAL,
            query=RuntimeQueryDescriptor(
                descriptor_id="tempo:query:1",
                template_id="tempo.target_traces.v1",
                target=EntityRef(namespace="shop", kind="Deployment", name="checkout"),
                requested_start=T0 - timedelta(minutes=5),
                requested_end=T0,
                effective_start=T0 - timedelta(minutes=5),
                effective_end=T0,
                limit=8,
            ),
            source_observation_ids=("tempo:trace:span",),
        ),
    )

    normalized = normalize_observation(observation, case=case, gap=gap)

    assert normalized.findings == ()
    assert normalized.observation.outcome is GapOutcomeKind.UNKNOWN
    assert case.findings == findings_before
    assert case.hypotheses == hypotheses_before
    assert case.candidates == candidates_before

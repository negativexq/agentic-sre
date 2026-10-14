"""Execution input goals are scoped planning, never substitute witnesses."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from packages.rca.causal_closure import ROLLOUT_EXECUTION_RULE
from packages.rca.engine import Case, EngineConfig, build_case, diagnose_case
from packages.rca.epistemic_digest import diagnosis_epistemic_digest
from packages.rca.investigation.candidates import build_observation_candidates
from packages.rca.investigation.execution_inputs import project_execution_input_gaps
from packages.rca.investigation.selection import candidate_to_action, rank_observation_candidates
from packages.rca.model import (
    Alert,
    CausalWitness,
    Diagnosis,
    EntityInstanceRef,
    EntityRef,
    Finding,
    FindingKind,
    Hypothesis,
    HypothesisResolutionAudit,
    HypothesisSignature,
    Lifecycle,
    ObjectVersion,
    Resolution,
    ResolutionTrace,
    RootSupportRecord,
    RootSupportStatus,
)
from packages.rca.resolution import CHANGE_ONSET_PATH_RULE
from packages.rca.source import InMemorySource

ONSET = datetime(2026, 1, 1, 1, tzinfo=UTC)
ACTOR = EntityRef(namespace="ns", kind="Deployment", name="pay")
RS = EntityRef(namespace="ns", kind="ReplicaSet", name="pay-rs")
POD = EntityRef(namespace="ns", kind="Pod", name="pay-rs-a")
SYMPTOM = EntityRef(namespace="ns", kind="Service", name="orders")


def fixture(*, owned: bool = False) -> tuple[Case, Diagnosis]:
    origin = Finding(
        entity=ACTOR,
        kind=FindingKind.IMAGE_CHANGE,
        summary="image changed",
        at=ONSET - timedelta(seconds=5),
        incident_onset=ONSET,
        evidence_ids=("change:1",),
    )
    instance = EntityInstanceRef(entity=ACTOR, uid="dep-uid") if owned else None
    hypothesis = Hypothesis(
        hypothesis_id="claim",
        causal_actor=ACTOR,
        actor_instance=instance,
        episode_onset=ONSET,
        findings=(origin,),
        members=(ACTOR, RS, POD),
        symptom_entities=(SYMPTOM,),
    )
    possible = RootSupportRecord(
        rule_id=CHANGE_ONSET_PATH_RULE[0],
        rule_version=CHANGE_ONSET_PATH_RULE[1],
        support_kind="POSSIBLE_INITIATOR",
        status=RootSupportStatus.FIRED,
        witnesses=(
            CausalWitness(
                actor=ACTOR,
                origin=origin,
                mechanism="IMAGE_CHANGE",
                symptom=SYMPTOM,
                onset=ONSET,
                rule_id=CHANGE_ONSET_PATH_RULE[0],
                rule_version="v2",
                evidence_ids=origin.evidence_ids,
            ),
        ),
    )
    execution = RootSupportRecord(
        rule_id=ROLLOUT_EXECUTION_RULE,
        rule_version="v1",
        support_kind="OBSERVED_MECHANISM_CAUSE",
        status=RootSupportStatus.NOT_FIRED,
        reasons=("NO_ROLLOUT_EXECUTION_WITH_INCIDENT_EFFECT_WITNESS",),
    )
    versions = []
    if owned:
        assert origin.at is not None
        versions = [
            ObjectVersion(
                entity=RS,
                uid="rs-uid",
                observed_at=origin.at,
                lifecycle=Lifecycle.CREATED,
                body={"metadata": {"ownerReferences": [{"uid": "dep-uid"}]}},
                evidence_id="rs:1",
            ),
            ObjectVersion(
                entity=POD,
                uid="pod-uid",
                observed_at=ONSET - timedelta(seconds=3),
                lifecycle=Lifecycle.CREATED,
                body={"metadata": {"ownerReferences": [{"uid": "rs-uid"}]}},
                evidence_id="pod:1",
            ),
        ]
    case = build_case(
        InMemorySource(
            name="execution-goals",
            versions=versions,
            alert_items=[Alert(name="failure", service="orders", namespace="ns", starts_at=ONSET)],
            alert_coverage_start=ONSET - timedelta(minutes=30),
            cutoff=ONSET + timedelta(minutes=1),
        )
    )
    diagnosis = diagnose_case(case).model_copy(
        update={
            "hypothesis": hypothesis,
            "resolution_trace": ResolutionTrace(
                state=Resolution.AMBIGUOUS,
                hypothesis_audits=(
                    HypothesisResolutionAudit(
                        hypothesis_id="claim",
                        signature=HypothesisSignature(),
                        plausible=True,
                        root_support=(possible, execution),
                    ),
                ),
            ),
        }
    )
    return case, diagnosis


def test_missing_uid_and_lifecycle_generate_real_authorized_candidates_without_changing_rca() -> (
    None
):
    case, diagnosis = fixture()
    before = diagnosis_epistemic_digest(diagnosis)
    planned = project_execution_input_gaps(case, diagnosis, ("history", "events", "runtime_traces"))
    assert diagnosis_epistemic_digest(planned) == before
    assert {
        gap.entity_scope[0] for gap in planned.information_gaps if gap.execution_input_target
    } == {ACTOR, RS, POD}
    candidates = build_observation_candidates(
        case=case, diagnosis=planned, engine_config=EngineConfig()
    )
    assert candidates
    selected = rank_observation_candidates(candidates=candidates, diagnosis=planned)[0]
    assert selected.utility.discrimination_value == 3
    assert any(
        target.input_scope == "RULE_INPUT" and target.acquisition_class == "PARTIAL"
        for target in selected.evidence_targets
    )
    action = candidate_to_action(selected, planned)
    assert action is not None and action.capability == "history"
    assert all(
        not target.resolution_authority and not target.no_data_is_exclusion
        for target in selected.evidence_targets
    )


def test_known_owned_lifecycle_leaves_effect_probe_with_prechange_baseline() -> None:
    case, diagnosis = fixture(owned=True)
    planned = project_execution_input_gaps(case, diagnosis, ("history", "events", "runtime_traces"))
    goals = [gap for gap in planned.information_gaps if gap.execution_input_target]
    assert len(goals) == 1
    assert goals[0].entity_scope == (ACTOR,)
    assert goals[0].candidate_tools == ("runtime_traces",)
    candidates = build_observation_candidates(
        case=case, diagnosis=planned, engine_config=EngineConfig()
    )
    assert candidates
    assert candidates[0].query.start is not None
    assert candidates[0].query.start <= ONSET - timedelta(minutes=5, seconds=5)


@pytest.mark.parametrize(
    "update",
    (
        {"status": RootSupportStatus.FIRED},
        {"rule_version": "unknown"},
        {"reasons": ("unregistered",)},
    ),
)
def test_closed_or_unknown_execution_requirement_removes_goals(update: dict[str, Any]) -> None:
    case, diagnosis = fixture()
    assert diagnosis.resolution_trace is not None
    audit = diagnosis.resolution_trace.hypothesis_audits[0]
    changed = diagnosis.model_copy(
        update={
            "resolution_trace": diagnosis.resolution_trace.model_copy(
                update={
                    "hypothesis_audits": (
                        audit.model_copy(
                            update={
                                "root_support": (
                                    audit.root_support[0],
                                    audit.root_support[1].model_copy(update=update),
                                )
                            }
                        ),
                    ),
                }
            )
        }
    )
    assert not project_execution_input_gaps(case, changed, ("history",)).information_gaps


def test_planning_uses_visible_inputs_and_never_reads_the_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case, diagnosis = fixture(owned=True)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("planning opened telemetry")

    for method in (
        "object_history",
        "events",
        "trace_observations",
        "error_logs",
        "observation_cutoff",
    ):
        monkeypatch.setattr(case.source, method, forbidden)
    planned = project_execution_input_gaps(case, diagnosis, ("runtime_traces",))
    assert any(gap.execution_input_target for gap in planned.information_gaps)
    assert planned.resolution == diagnosis.resolution


def test_default_graph_actually_selects_a_partial_input_query_and_preserves_empty_outcome() -> None:
    from packages.rca.investigation.graph import investigate_diagnosis
    from packages.rca.investigation.intents import DeterministicIntentPolicy
    from packages.rca.investigation.state import SEED_FULL_SOURCE, InvestigationConfig
    from packages.rca.model import GapOutcomeKind

    case, diagnosis = fixture()
    result = investigate_diagnosis(
        case.source,
        diagnosis=diagnosis,
        initial_case=case,
        policy=DeterministicIntentPolicy(),
        config=InvestigationConfig(seed_mode=SEED_FULL_SOURCE, max_turns=1),
    )
    assert result.tool_calls == 1
    audit = result.action_audits[0]
    assert audit.authorization_result == "AUTHORIZED"
    assert audit.action.capability == "history"
    assert audit.action.target in {ACTOR, RS, POD}
    assert audit.observation_outcome is GapOutcomeKind.NO_DATA
    assert audit.research_question is not None
    assert any(
        target.input_scope == "RULE_INPUT" and target.acquisition_class == "PARTIAL"
        for read in audit.research_question.reads
        for target in read.evidence_targets
    )
    assert result.final_resolution is not Resolution.RESOLVED


def test_quota_probe_is_bound_to_rejected_subject_not_the_quota_object() -> None:
    from packages.rca.causal_closure import EXECUTION_RULE

    case, diagnosis = fixture()
    assert diagnosis.hypothesis is not None and diagnosis.resolution_trace is not None
    quota = EntityRef(namespace="ns", kind="ResourceQuota", name="budget")
    origin = diagnosis.hypothesis.findings[0].model_copy(
        update={
            "entity": quota,
            "kind": FindingKind.QUOTA_EXCEEDED,
        }
    )
    hypothesis = diagnosis.hypothesis.model_copy(
        update={"causal_actor": quota, "findings": (origin,)}
    )
    audit = diagnosis.resolution_trace.hypothesis_audits[0]
    possible = audit.root_support[0].model_copy(
        update={
            "witnesses": (
                audit.root_support[0]
                .witnesses[0]
                .model_copy(update={"actor": quota, "origin": origin}),
            )
        }
    )
    execution = audit.root_support[1].model_copy(
        update={
            "rule_id": EXECUTION_RULE,
            "reasons": ("NO_DIRECT_INCIDENT_REJECTION_WITNESS",),
        }
    )
    diagnosis = diagnosis.model_copy(
        update={
            "hypothesis": hypothesis,
            "resolution_trace": diagnosis.resolution_trace.model_copy(
                update={
                    "hypothesis_audits": (
                        audit.model_copy(update={"root_support": (possible, execution)}),
                    ),
                }
            ),
        }
    )
    planned = project_execution_input_gaps(case, diagnosis, ("events",))
    assert len(planned.information_gaps) == 1
    gap = planned.information_gaps[0]
    assert gap.entity_scope == (SYMPTOM,)
    assert gap.authorized_queries[0].capability == "events"
    assert gap.execution_input_target is not None
    assert gap.execution_input_target.acquisition_class == "PARTIAL"


def test_failed_rollout_requests_uid_bound_pod_warning() -> None:
    from packages.rca.causal_closure import FAILED_ROLLOUT_RULE

    case, diagnosis = fixture(owned=True)
    assert diagnosis.resolution_trace is not None
    audit = diagnosis.resolution_trace.hypothesis_audits[0]
    execution = audit.root_support[1].model_copy(
        update={
            "rule_id": FAILED_ROLLOUT_RULE,
            "reasons": ("NO_FAILED_ROLLOUT_WITH_INCIDENT_EFFECT_WITNESS",),
        }
    )
    diagnosis = diagnosis.model_copy(
        update={
            "resolution_trace": diagnosis.resolution_trace.model_copy(
                update={
                    "hypothesis_audits": (
                        audit.model_copy(
                            update={"root_support": (audit.root_support[0], execution)}
                        ),
                    ),
                }
            )
        }
    )
    planned = project_execution_input_gaps(case, diagnosis, ("events", "history"))
    assert len(planned.information_gaps) == 1
    gap = planned.information_gaps[0]
    assert gap.entity_scope == (POD,)
    assert gap.candidate_tools == ("events",)
    assert gap.execution_input_target is not None
    assert any(
        "involved UID" in requirement
        for requirement in gap.execution_input_target.required_observations
    )


def test_missing_owner_uid_is_not_treated_as_a_matching_owner() -> None:
    case, diagnosis = fixture()
    source = InMemorySource(
        name="uid-missing",
        versions=[
            ObjectVersion(
                entity=RS,
                uid="rs-uid",
                observed_at=ONSET - timedelta(seconds=5),
                body={},
                lifecycle=Lifecycle.CREATED,
                evidence_id="rs:no-owner",
            )
        ],
        alert_items=list(case.inputs.alerts) if case.inputs else [],
        alert_coverage_start=ONSET - timedelta(minutes=30),
        cutoff=ONSET + timedelta(minutes=1),
    )
    case = build_case(source)
    planned = project_execution_input_gaps(case, diagnosis, ("history",))
    assert any(gap.entity_scope == (RS,) for gap in planned.information_gaps)


def test_no_positive_origin_and_explained_claims_do_not_gain_execution_goals() -> None:
    case, diagnosis = fixture()
    assert diagnosis.resolution_trace is not None
    audit = diagnosis.resolution_trace.hypothesis_audits[0]
    no_origin = diagnosis.model_copy(
        update={
            "resolution_trace": diagnosis.resolution_trace.model_copy(
                update={
                    "hypothesis_audits": (
                        audit.model_copy(update={"root_support": (audit.root_support[1],)}),
                    ),
                }
            )
        }
    )
    assert not project_execution_input_gaps(case, no_origin, ("history",)).information_gaps
    explained = diagnosis.model_copy(
        update={
            "resolution_trace": diagnosis.resolution_trace.model_copy(
                update={
                    "explained_hypotheses": ("claim",),
                }
            )
        }
    )
    assert not project_execution_input_gaps(case, explained, ("history",)).information_gaps


def test_effect_probe_does_not_use_error_log_pairs_that_omit_healthy_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import packages.rca.investigation.candidates as candidates_module

    case, diagnosis = fixture(owned=True)
    planned = project_execution_input_gaps(case, diagnosis, ("runtime_traces",))
    monkeypatch.setattr(candidates_module, "pair_provider", lambda source: object())

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("effect/baseline probes must not use error-only log pair selection")

    monkeypatch.setattr(candidates_module, "eligible_logs", forbidden)
    candidates = build_observation_candidates(
        case=case, diagnosis=planned, engine_config=EngineConfig()
    )
    assert candidates and all(not candidate.query.source_log_ids for candidate in candidates)

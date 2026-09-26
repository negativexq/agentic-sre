"""Canonical epistemic digest of an RCA's decision state."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from rca_builders import config_change_source, ref

from packages.rca.agent import LLMInvestigator
from packages.rca.engine import diagnose
from packages.rca.epistemic_digest import (
    EpistemicEliminationEntry,
    EpistemicHypothesisEntry,
    EpistemicState,
    compute_epistemic_digest,
    diagnosis_epistemic_digest,
    epistemic_state,
)
from packages.rca.llm import ScriptedLLM
from packages.rca.model import (
    Confidence,
    Diagnosis,
    EliminationConsequence,
    EliminationTimeBasis,
    Hypothesis,
    HypothesisEpistemicState,
    HypothesisResolutionAudit,
    HypothesisSignature,
    InvestigationStep,
    Resolution,
    ResolutionElimination,
    ResolutionReasonCode,
    ResolutionTrace,
    Symptoms,
)

AT = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def _hypothesis(hypothesis_id: str, actor: str, *, score: float = 1.0) -> Hypothesis:
    return Hypothesis(
        hypothesis_id=hypothesis_id,
        hypothesis_key=f"key:{hypothesis_id}",
        causal_actor=ref(actor),
        score=score,
        reasons=(f"reason text for {hypothesis_id}",),
    )


def _audit(
    hypothesis_id: str, state: HypothesisEpistemicState = HypothesisEpistemicState.SUPPORTED
) -> HypothesisResolutionAudit:
    return HypothesisResolutionAudit(
        hypothesis_id=hypothesis_id,
        signature=HypothesisSignature(),
        epistemic_state=state,
        plausible=state is HypothesisEpistemicState.SUPPORTED,
    )


def _elimination(
    hypothesis_id: str = "H3",
    *,
    evidence_ids: tuple[str, ...] = ("ev:b", "ev:a"),
    decisive_evidence_ids: tuple[str, ...] = ("ev:d", "ev:c"),
    detail: str = "free-text detail",
    consequence: EliminationConsequence = EliminationConsequence.CONTRADICTION,
    code: ResolutionReasonCode = ResolutionReasonCode.EXPLICIT_TEMPORAL_CONTRADICTION,
) -> ResolutionElimination:
    return ResolutionElimination(
        hypothesis_id=hypothesis_id,
        code=code,
        evidence_ids=evidence_ids,
        decisive_evidence_ids=decisive_evidence_ids,
        detail=detail,
        rule_id="m16.temporal-contradiction",
        rule_version="v1",
        consequence=consequence,
        time_basis=(EliminationTimeBasis(onset=AT, boundary=AT),),
    )


def _diagnosis(**overrides: Any) -> Diagnosis:
    leader = _hypothesis("H1", "shop/ConfigMap/checkout-flags", score=0.9)
    alternatives = (
        _hypothesis("H2", "shop/Deployment/payment", score=0.4),
        _hypothesis("H3", "shop/Deployment/cart", score=0.2),
    )
    trace = ResolutionTrace(
        state=Resolution.RESOLVED,
        hypothesis_audits=(
            _audit("H1"),
            _audit("H2", HypothesisEpistemicState.UNRESOLVED),
            _audit("H3", HypothesisEpistemicState.CONTRADICTED),
        ),
        eliminations=(_elimination(),),
        rationale="rationale text",
    )
    fields: dict[str, Any] = dict(
        incident_id="incident-1",
        root_cause=leader.causal_actor,
        confidence=Confidence.VERIFIED,
        resolution=Resolution.RESOLVED,
        summary="summary text",
        symptoms=Symptoms(
            onset=AT, last_seen=AT, services=("checkout",), namespaces=("shop",), alert_names=()
        ),
        hypothesis=leader,
        alternative_hypotheses=alternatives,
        resolution_trace=trace,
    )
    fields.update(overrides)
    return Diagnosis(**fields)


def _trace_with(diagnosis: Diagnosis, **update: Any) -> Diagnosis:
    assert diagnosis.resolution_trace is not None
    return diagnosis.model_copy(
        update={"resolution_trace": diagnosis.resolution_trace.model_copy(update=update)}
    )


def test_digest_is_sha256_of_exact_canonical_payload() -> None:
    expected = {
        "resolution": "RESOLVED",
        "root_causal_actor": "shop/ConfigMap/checkout-flags",
        "hypotheses": [
            ["key:H1", "H1", "shop/ConfigMap/checkout-flags", "SUPPORTED", True],
            ["key:H2", "H2", "shop/Deployment/payment", "UNRESOLVED", True],
            ["key:H3", "H3", "shop/Deployment/cart", "CONTRADICTED", True],
        ],
        "eliminations": [
            [
                "key:H3",
                "H3",
                "EXPLICIT_TEMPORAL_CONTRADICTION",
                "m16.temporal-contradiction",
                "v1",
                ["ev:a", "ev:b"],
                ["ev:c", "ev:d"],
            ]
        ],
    }
    canonical = json.dumps(expected, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert diagnosis_epistemic_digest(_diagnosis()) == sha256(canonical.encode()).hexdigest()


def test_explanation_text_scores_timestamps_and_llm_fields_do_not_change_digest() -> None:
    base = _diagnosis()
    text_only = _diagnosis(
        summary="entirely different explanation",
        confidence=Confidence.LIKELY,
        mode="llm",
        model_calls=7,
        steps=(InvestigationStep(actor="llm", action="conclude", detail="model prose"),),
        hypothesis=_hypothesis("H1", "shop/ConfigMap/checkout-flags", score=0.1).model_copy(
            update={"reasons": ("other reason",), "causal_explanation": "LINKED"}
        ),
        symptoms=Symptoms(
            onset=datetime(2030, 1, 1, tzinfo=UTC),
            last_seen=None,
            services=(),
            namespaces=(),
            alert_names=("other",),
        ),
    )
    text_only = _trace_with(
        text_only,
        rationale="other rationale",
        decision_basis="OTHER",
        eliminations=(
            _elimination(
                detail="other detail",
                evidence_ids=("ev:a", "ev:b"),
                decisive_evidence_ids=("ev:c", "ev:d"),
            ).model_copy(update={"time_basis": (), "mechanism": "X", "targets": ("t",)}),
        ),
    )
    assert diagnosis_epistemic_digest(text_only) == diagnosis_epistemic_digest(base)


def test_one_hypothesis_state_change_changes_digest() -> None:
    base = _diagnosis()
    assert base.resolution_trace is not None
    audits = list(base.resolution_trace.hypothesis_audits)
    audits[1] = _audit("H2", HypothesisEpistemicState.SUPPORTED)
    changed = _trace_with(base, hypothesis_audits=tuple(audits))
    assert diagnosis_epistemic_digest(changed) != diagnosis_epistemic_digest(base)


def test_each_decision_field_changes_digest() -> None:
    base = diagnosis_epistemic_digest(_diagnosis())
    variants = [
        _diagnosis(resolution=Resolution.AMBIGUOUS),
        _diagnosis(root_cause=ref("shop/Deployment/payment")),
        _diagnosis(root_cause=None),
        _diagnosis(
            alternative_hypotheses=(
                _hypothesis("H2", "shop/Deployment/other"),
                _hypothesis("H3", "shop/Deployment/cart"),
            )
        ),
        _diagnosis(
            alternative_hypotheses=(
                _hypothesis("H2", "shop/Deployment/payment").model_copy(
                    update={"hypothesis_key": "key:other"}
                ),
                _hypothesis("H3", "shop/Deployment/cart"),
            )
        ),
        _trace_with(_diagnosis(), eliminations=()),
        _trace_with(_diagnosis(), eliminations=(_elimination(evidence_ids=("ev:a",)),)),
        _trace_with(_diagnosis(), eliminations=(_elimination(decisive_evidence_ids=()),)),
        _trace_with(
            _diagnosis(),
            eliminations=(_elimination().model_copy(update={"rule_version": "v2"}),),
        ),
        _trace_with(
            _diagnosis(),
            eliminations=(_elimination().model_copy(update={"rule_id": "m16.other"}),),
        ),
        _trace_with(
            _diagnosis(),
            eliminations=(
                _elimination(code=ResolutionReasonCode.OBSERVED_NORMAL_MECHANISM_MISMATCH),
            ),
        ),
    ]
    digests = [diagnosis_epistemic_digest(item) for item in variants]
    assert base not in digests
    assert len(set(digests)) == len(digests)


def test_root_ineligibility_elimination_marks_hypothesis_ineligible() -> None:
    diagnosis = _trace_with(
        _diagnosis(),
        eliminations=(
            _elimination(),
            _elimination(
                "H2",
                consequence=EliminationConsequence.ROOT_INELIGIBILITY,
                code=ResolutionReasonCode.ROOT_CAUSE_INELIGIBLE_PROPAGATED_EFFECT,
            ),
        ),
    )
    eligibility = {
        item.hypothesis_id: item.root_eligible for item in epistemic_state(diagnosis).hypotheses
    }
    assert eligibility == {"H1": True, "H2": False, "H3": True}
    same_elimination_as_contradiction = _trace_with(
        diagnosis,
        eliminations=(
            _elimination(),
            _elimination(
                "H2",
                consequence=EliminationConsequence.CONTRADICTION,
                code=ResolutionReasonCode.ROOT_CAUSE_INELIGIBLE_PROPAGATED_EFFECT,
            ),
        ),
    )
    assert diagnosis_epistemic_digest(same_elimination_as_contradiction) != (
        diagnosis_epistemic_digest(diagnosis)
    )


def test_hypothesis_and_elimination_order_does_not_change_digest() -> None:
    base = _diagnosis()
    first = _elimination("H3")
    second = _elimination("H2", evidence_ids=("ev:z",))
    ordered = _trace_with(base, eliminations=(second, first))
    reordered = _trace_with(
        _diagnosis(
            hypothesis=_hypothesis("H1", "shop/ConfigMap/checkout-flags"),
            alternative_hypotheses=tuple(reversed(base.alternative_hypotheses)),
        ),
        hypothesis_audits=tuple(reversed(base.resolution_trace.hypothesis_audits)),  # type: ignore[union-attr]
        eliminations=(first, second),
    )
    assert diagnosis_epistemic_digest(reordered) == diagnosis_epistemic_digest(ordered)


def test_hypotheses_are_ordered_by_id_and_repeated_eliminations_are_kept() -> None:
    entry = EpistemicHypothesisEntry("k", "H1", "a", "SUPPORTED", True)
    other = EpistemicHypothesisEntry("k2", "H2", "b", "UNRESOLVED", True)
    elimination = EpistemicEliminationEntry("k2", "H2", "R", "rule", "v1", ("e",), ())

    def state(*eliminations: EpistemicEliminationEntry, hypotheses: Any = (entry, other)) -> str:
        return compute_epistemic_digest(
            EpistemicState("RESOLVED", "a", tuple(hypotheses), eliminations)
        )

    assert state(elimination) == state(elimination, hypotheses=(other, entry))
    assert state(elimination, elimination) != state(elimination)


def test_audit_only_and_retained_only_hypotheses_are_both_represented() -> None:
    diagnosis = _diagnosis(
        alternative_hypotheses=(_hypothesis("H9", "shop/Deployment/unaudited"),),
    )
    by_id = {item.hypothesis_id: item for item in epistemic_state(diagnosis).hypotheses}
    assert by_id["H9"] == EpistemicHypothesisEntry(
        "key:H9", "H9", "shop/Deployment/unaudited", None, True
    )
    assert by_id["H2"] == EpistemicHypothesisEntry(None, "H2", None, "UNRESOLVED", True)


def test_diagnosis_without_trace_digests_resolution_and_root_only() -> None:
    diagnosis = _diagnosis(
        root_cause=None,
        resolution=Resolution.INSUFFICIENT_EVIDENCE,
        hypothesis=None,
        alternative_hypotheses=(),
        resolution_trace=None,
    )
    assert epistemic_state(diagnosis) == EpistemicState("INSUFFICIENT_EVIDENCE", None, (), ())


def test_engine_diagnoses_differing_only_in_llm_narration_share_digest() -> None:
    deterministic = diagnose(config_change_source())
    llm = ScriptedLLM(
        [
            {"action": "inspect", "tool": "history", "target": "C1", "rationale": "because"},
            {"action": "conclude", "tool": None, "target": "C1", "rationale": "because"},
        ]
    )
    narrated = diagnose(config_change_source(), investigator=LLMInvestigator(llm))
    assert narrated.mode == "llm" and deterministic.mode != "llm"
    assert narrated.steps != deterministic.steps
    assert diagnosis_epistemic_digest(narrated) == diagnosis_epistemic_digest(deterministic)
    assert diagnosis_epistemic_digest(diagnose(config_change_source())) == (
        diagnosis_epistemic_digest(deterministic)
    )

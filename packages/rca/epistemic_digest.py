"""Canonical fingerprint of an RCA's epistemic decision state.

Only decision-bearing fields participate: the resolution, the root causal
actor, each hypothesis' identity, causal actor, epistemic state and root
eligibility, and each elimination's rule identity and evidence. In m21.v2 the
actor/time/source witnesses, admission and material frontier also participate.
Ranking scores and model output never enter the digest.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256

from packages.rca.model import (
    Diagnosis,
    EliminationConsequence,
    Hypothesis,
    TimingAssessment,
)


def _iso(value: object) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None


def _timing_document(timing: TimingAssessment) -> dict[str, object]:
    """Everything replay needs to audit why the same onset set and outcomes were produced.

    Not just the computed bounds: the qualified episodes each member derives from (alert
    fingerprint, start, capture evidence ids), the cutoff and coverage boundary, and every
    stability outcome. Deterministic order only.
    """
    uncertainty = timing.uncertainty
    return {
        "version": timing.version,
        "reason": uncertainty.reason,
        "cutoff": _iso(uncertainty.cutoff),
        "alert_observation_start": _iso(uncertainty.alert_observation_start),
        "h0": _iso(uncertainty.h0),
        "upper": _iso(uncertainty.upper),
        "members": [
            {
                "onset": _iso(member.onset),
                "is_h0": member.is_h0,
                "is_upper": member.is_upper,
                "episodes": [
                    {
                        "fingerprint": episode.fingerprint,
                        "name": episode.name,
                        "starts_at": _iso(episode.starts_at),
                        "evidence": sorted(episode.evidence_ids),
                    }
                    for episode in sorted(member.episodes, key=lambda e: e.fingerprint)
                ],
            }
            for member in uncertainty.members
        ],
        "status": timing.status.value,
        "outcomes": [
            {
                "onset": _iso(o.onset),
                "diagnosis": o.diagnosis_status,
                "drivers": [
                    {"key": d.hypothesis_key, "change": d.change, "reason": d.reason}
                    for d in sorted(o.drivers, key=lambda d: (d.hypothesis_key, d.change))
                ],
            }
            for o in timing.outcomes
        ],
        "claims": [
            {
                "key": claim.hypothesis_key,
                "formation": claim.formation.value,
                "adjudication": claim.adjudication.value,
                "relations": [
                    {
                        "relation": r.relation,
                        "stability": r.stability.value,
                        "values": list(r.values),
                    }
                    for r in claim.relations
                ],
            }
            for claim in sorted(timing.claims, key=lambda c: c.hypothesis_key)
        ],
        "withheld": [
            {"key": w.hypothesis_key, "authority": w.authority, "relations": list(w.relations)}
            for w in sorted(timing.withheld, key=lambda w: (w.hypothesis_key, w.authority))
        ],
    }


@dataclass(frozen=True)
class EpistemicHypothesisEntry:
    """The exact hypothesis fields that participate in an epistemic digest."""

    hypothesis_key: str | None
    hypothesis_id: str
    causal_actor: str | None
    state: str | None
    root_eligible: bool


@dataclass(frozen=True)
class EpistemicEliminationEntry:
    """The exact elimination fields that participate in an epistemic digest."""

    hypothesis_key: str | None
    hypothesis_id: str
    reason: str
    rule_id: str
    rule_version: str
    evidence_ids: tuple[str, ...]
    decisive_evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class EpistemicState:
    """The canonical decision state of one diagnosis."""

    resolution: str
    root_causal_actor: str | None
    hypotheses: tuple[EpistemicHypothesisEntry, ...]
    eliminations: tuple[EpistemicEliminationEntry, ...]
    causal_decision: str | None = None


def epistemic_state(diagnosis: Diagnosis) -> EpistemicState:
    """Project a diagnosis onto its decision-bearing fields.

    The hypothesis set is every hypothesis the diagnosis audits or retains.
    Key and causal actor are ``None`` when the diagnosis retains only the
    audit; state is ``None`` when it retains only the hypothesis. Root
    eligibility is false exactly when a root-ineligibility elimination names
    the hypothesis.
    """
    trace = diagnosis.resolution_trace
    audits = trace.hypothesis_audits if trace is not None else ()
    eliminations = trace.eliminations if trace is not None else ()
    retained: dict[str, Hypothesis] = {}
    for hypothesis in (
        *((diagnosis.hypothesis,) if diagnosis.hypothesis is not None else ()),
        *diagnosis.alternative_hypotheses,
        *diagnosis.ambiguous_hypotheses,
    ):
        retained.setdefault(hypothesis.hypothesis_id, hypothesis)
    states: dict[str, str] = {}
    for audit in audits:
        states.setdefault(audit.hypothesis_id, audit.epistemic_state.value)
    root_ineligible = {
        item.hypothesis_id
        for item in eliminations
        if item.consequence is EliminationConsequence.ROOT_INELIGIBILITY
    }

    def key_of(hypothesis_id: str) -> str | None:
        hypothesis = retained.get(hypothesis_id)
        if hypothesis is None:
            return None
        return hypothesis.hypothesis_key or None

    causal_decision = None
    if trace is not None and trace.semantics_version in {"m21.v2", "m21.v3"}:
        causal_decision = json.dumps(
            {
                **(
                    {
                        "explanations": [r.model_dump(mode="json") for r in trace.explanations],
                        "frontier_answers": [
                            a.model_dump(
                                mode="json",
                                exclude={"investigation_state", "blocked_reason"},
                                exclude_none=True,
                            )
                            for a in trace.frontier_answers
                        ],
                        "mechanism_verified": sorted(trace.mechanism_verified_hypotheses),
                        "independent_causes": sorted(trace.independent_mechanism_causes),
                        # Family competition: identity, state, exact members and how far the
                        # viable instances are resolved. `representative` is rank-derived
                        # display and never enters the digest.
                        "families": [
                            {
                                "id": f.family_id,
                                "actor": f.actor.canonical,
                                "mechanism_family": f.mechanism_family,
                                "state": f.state.value,
                                "instance_resolution": f.instance_resolution.value,
                                "members": sorted(f.members),
                                "supported": sorted(f.supported_members),
                                "unresolved": sorted(f.unresolved_members),
                                "excluded": sorted(f.excluded_members),
                            }
                            for f in sorted(trace.causal_families, key=lambda item: item.family_id)
                        ],
                        **(
                            {"timing": _timing_document(trace.timing)}
                            if trace.timing is not None
                            else {}
                        ),
                    }
                    if trace.semantics_version == "m21.v3"
                    else {}
                ),
                "version": trace.semantics_version,
                "diagnosis": trace.diagnosis_status,
                "claim_level": trace.claim_level,
                "admitted": sorted(trace.admitted_hypotheses),
                "context": sorted(trace.context_hypotheses),
                "material_frontier": sorted(trace.material_frontier_ids),
                "frontier_bindings": sorted(trace.frontier_bindings),
                "claims": [
                    {
                        "id": audit.hypothesis_id,
                        **(
                            {"family": audit.causal_family_id}
                            if trace.semantics_version == "m21.v3"
                            else {}
                        ),
                        "admission": audit.admission,
                        "reasons": sorted(audit.admission_reasons),
                        "support": [
                            record.model_dump(mode="json") for record in audit.root_support
                        ],
                    }
                    for audit in sorted(audits, key=lambda item: item.hypothesis_id)
                ],
                "frontier": [
                    {
                        "id": item.alternative_id,
                        "actor": item.actor.canonical,
                        "role": item.role,
                        "affected": sorted(entity.canonical for entity in item.affected_entities),
                        "dimensions": sorted(
                            dimension.value for dimension in item.queryable_dimensions
                        ),
                    }
                    for item in sorted(
                        diagnosis.structural_alternatives, key=lambda item: item.alternative_id
                    )
                    if item.alternative_id in trace.material_frontier_ids
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    return EpistemicState(
        causal_decision=causal_decision,
        resolution=diagnosis.resolution.value,
        root_causal_actor=(
            diagnosis.root_cause.canonical if diagnosis.root_cause is not None else None
        ),
        hypotheses=tuple(
            EpistemicHypothesisEntry(
                hypothesis_key=key_of(hypothesis_id),
                hypothesis_id=hypothesis_id,
                causal_actor=(
                    retained[hypothesis_id].causal_actor.canonical
                    if hypothesis_id in retained
                    else None
                ),
                state=states.get(hypothesis_id),
                root_eligible=hypothesis_id not in root_ineligible,
            )
            for hypothesis_id in {*states, *retained}
        ),
        eliminations=tuple(
            EpistemicEliminationEntry(
                hypothesis_key=key_of(item.hypothesis_id),
                hypothesis_id=item.hypothesis_id,
                reason=item.code.value,
                rule_id=item.rule_id,
                rule_version=item.rule_version,
                evidence_ids=item.evidence_ids,
                decisive_evidence_ids=item.decisive_evidence_ids,
            )
            for item in eliminations
        ),
    )


def compute_epistemic_digest(state: EpistemicState) -> str:
    """Hash canonical JSON of an epistemic state, hypotheses ordered by ID."""
    hypotheses = [
        [
            entry.hypothesis_key,
            entry.hypothesis_id,
            entry.causal_actor,
            entry.state,
            entry.root_eligible,
        ]
        for entry in sorted(state.hypotheses, key=lambda item: item.hypothesis_id)
    ]
    rows = [
        [
            entry.hypothesis_key,
            entry.hypothesis_id,
            entry.reason,
            entry.rule_id,
            entry.rule_version,
            sorted(entry.evidence_ids),
            sorted(entry.decisive_evidence_ids),
        ]
        for entry in state.eliminations
    ]
    # The key may be None, so order on the always-present fields first.
    eliminations = sorted(rows, key=lambda row: (row[1:], row[0] or ""))
    canonical = json.dumps(
        {
            "resolution": state.resolution,
            "root_causal_actor": state.root_causal_actor,
            "hypotheses": hypotheses,
            "eliminations": eliminations,
            **(
                {"causal_decision": json.loads(state.causal_decision)}
                if state.causal_decision is not None
                else {}
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def diagnosis_epistemic_digest(diagnosis: Diagnosis) -> str:
    """Compute the epistemic digest of one diagnosis."""
    return compute_epistemic_digest(epistemic_state(diagnosis))


__all__ = [
    "EpistemicEliminationEntry",
    "EpistemicHypothesisEntry",
    "EpistemicState",
    "compute_epistemic_digest",
    "diagnosis_epistemic_digest",
    "epistemic_state",
]

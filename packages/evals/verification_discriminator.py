"""Ground-truth-blind verification discriminator counterfactuals.

This module evaluates frozen, non-production rules against the real RCA
hypotheses and verification traces.  It never changes resolution semantics.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, cast

from packages.rca.engine import Case, diagnose_case
from packages.rca.hypotheses import hypothesis_candidate
from packages.rca.model import Confidence, Diagnosis, Hypothesis, PredicateStatus, Resolution
from packages.rca.ranking import RankingConfig, verification_trace

CANDIDATE_RULE_IDS = (
    "V1_UNIQUE_VERIFIED",
    "V2_VERIFIED_OVER_HARD_FAILURES",
    "V3_VERIFIED_OVER_ONSET_FAILURE",
)
_HARD_FAILURES = frozenset(
    {"late_change_contradiction", "initiating_signal_required", "candidate_linked_to_symptom"}
)
_ONSET_FAILURES = _HARD_FAILURES | {"initiating_evidence_near_onset"}


def _json_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _json_value(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda x: str(x[0]))
        }
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_json_value(item) for item in value]
    return value


@dataclass(frozen=True)
class PredicateSnapshot:
    name: str
    status: str
    evidence_ids: tuple[str, ...]
    detail: str


@dataclass(frozen=True)
class HypothesisVerificationSnapshot:
    hypothesis_id: str
    causal_actor: str
    members: tuple[str, ...]
    decision: str
    predicates: tuple[PredicateSnapshot, ...]


@dataclass(frozen=True)
class VerificationRuleResult:
    rule_id: str
    triggered: bool
    selected_hypothesis_id: str | None
    selected_actor: str | None
    abstention_reason: str | None


@dataclass(frozen=True)
class VerificationDiscriminatorAudit:
    incident_id: str
    current_resolution: str
    plausible_hypothesis_ids: tuple[str, ...]
    hypothesis_verifications: tuple[HypothesisVerificationSnapshot, ...]
    rule_results: tuple[VerificationRuleResult, ...]

    def as_dict(self) -> dict[str, object]:
        return cast(dict[str, object], _json_value(asdict(self)))


def _snapshot(hypothesis: Hypothesis, trace: Any) -> HypothesisVerificationSnapshot:
    return HypothesisVerificationSnapshot(
        hypothesis_id=hypothesis.hypothesis_id,
        causal_actor=hypothesis.causal_actor.canonical,
        members=tuple(sorted(item.canonical for item in hypothesis.members)),
        decision=trace.decision.value,
        predicates=tuple(
            PredicateSnapshot(
                name=item.name,
                status=item.status.value,
                evidence_ids=tuple(item.evidence_ids),
                detail=item.detail,
            )
            for item in trace.predicates
        ),
    )


def _abstain(rule_id: str, reason: str) -> VerificationRuleResult:
    return VerificationRuleResult(
        rule_id=rule_id,
        triggered=False,
        selected_hypothesis_id=None,
        selected_actor=None,
        abstention_reason=reason,
    )


def evaluate_rules(
    current_resolution: str | Resolution,
    plausible_hypotheses: Sequence[Hypothesis],
    verification_traces: Mapping[str, Any],
) -> tuple[VerificationRuleResult, ...]:
    """Evaluate the three frozen counterfactual rules without ranking inputs."""
    resolution = (
        current_resolution.value
        if isinstance(current_resolution, Resolution)
        else current_resolution
    )
    hypotheses = tuple(sorted(plausible_hypotheses, key=lambda item: item.hypothesis_id))
    traces = verification_traces
    if resolution != Resolution.AMBIGUOUS.value:
        return tuple(_abstain(rule_id, "NOT_AMBIGUOUS") for rule_id in CANDIDATE_RULE_IDS)
    if len(hypotheses) < 2:
        return tuple(
            _abstain(rule_id, "FEWER_THAN_TWO_PLAUSIBLE") for rule_id in CANDIDATE_RULE_IDS
        )
    if any(hypothesis.hypothesis_id not in traces for hypothesis in hypotheses):
        return tuple(
            _abstain(rule_id, "MISSING_VERIFICATION_TRACE") for rule_id in CANDIDATE_RULE_IDS
        )
    verified = tuple(
        hypothesis
        for hypothesis in hypotheses
        if traces[hypothesis.hypothesis_id].decision is Confidence.VERIFIED
    )
    if not verified:
        return tuple(_abstain(rule_id, "NO_VERIFIED_HYPOTHESIS") for rule_id in CANDIDATE_RULE_IDS)
    if len(verified) > 1:
        return tuple(
            _abstain(rule_id, "MULTIPLE_VERIFIED_HYPOTHESES") for rule_id in CANDIDATE_RULE_IDS
        )

    selected = verified[0]
    competitors = tuple(item for item in hypotheses if item.hypothesis_id != selected.hypothesis_id)

    def result(
        rule_id: str, allowed_failures: frozenset[str], reason: str
    ) -> VerificationRuleResult:
        if not all(
            any(
                predicate.name in allowed_failures and predicate.status is PredicateStatus.FAIL
                for predicate in traces[item.hypothesis_id].predicates
            )
            for item in competitors
        ):
            return _abstain(rule_id, reason)
        return VerificationRuleResult(
            rule_id=rule_id,
            triggered=True,
            selected_hypothesis_id=selected.hypothesis_id,
            selected_actor=selected.causal_actor.canonical,
            abstention_reason=None,
        )

    v1 = VerificationRuleResult(
        rule_id="V1_UNIQUE_VERIFIED",
        triggered=True,
        selected_hypothesis_id=selected.hypothesis_id,
        selected_actor=selected.causal_actor.canonical,
        abstention_reason=None,
    )
    return (
        v1,
        result("V2_VERIFIED_OVER_HARD_FAILURES", _HARD_FAILURES, "COMPETITOR_HAS_NO_HARD_FAILURE"),
        result(
            "V3_VERIFIED_OVER_ONSET_FAILURE", _ONSET_FAILURES, "COMPETITOR_HAS_NO_ONSET_FAILURE"
        ),
    )


def audit_case(
    case: Case,
    diagnosis: Diagnosis | None = None,
    *,
    ranking: RankingConfig | None = None,
) -> VerificationDiscriminatorAudit:
    """Recompute complete plausible-hypothesis traces with no runner-up."""
    diagnosis = diagnosis or diagnose_case(case)
    ranking = ranking or RankingConfig()
    trace = diagnosis.resolution_trace
    plausible_ids = tuple(sorted(trace.plausible_hypotheses if trace else ()))
    by_id = {item.hypothesis_id: item for item in case.hypotheses}
    plausible = tuple(by_id[item] for item in plausible_ids if item in by_id)
    verification = {
        hypothesis.hypothesis_id: verification_trace(
            hypothesis_candidate(hypothesis), case.context, ranking, runner_up=None
        )
        for hypothesis in plausible
    }
    return VerificationDiscriminatorAudit(
        incident_id=case.incident_id,
        current_resolution=diagnosis.resolution.value,
        plausible_hypothesis_ids=plausible_ids,
        hypothesis_verifications=tuple(
            _snapshot(hypothesis, verification[hypothesis.hypothesis_id])
            for hypothesis in plausible
        ),
        rule_results=evaluate_rules(diagnosis.resolution, plausible, verification),
    )


__all__ = [
    "CANDIDATE_RULE_IDS",
    "HypothesisVerificationSnapshot",
    "PredicateSnapshot",
    "VerificationDiscriminatorAudit",
    "VerificationRuleResult",
    "audit_case",
    "evaluate_rules",
]

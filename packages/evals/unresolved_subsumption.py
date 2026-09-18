"""Ground-truth-blind audit of unresolved manifestation blockers.

The audit observes the production FULL ``Case`` and ``Diagnosis`` only.  It
does not change epistemic state, resolution, grouping, topology, or
investigation behaviour.  S1--S3 are deliberately pairwise, structural
counterfactuals: a manifestation may stop blocking a supported episode only
when the evidence and causal direction make that interpretation explicit.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, cast

from packages.rca.engine import Case, build_case, diagnose_case
from packages.rca.model import Finding, FindingKind, Hypothesis
from packages.rca.ranking import RankingConfig
from packages.rca.source import ObservationSource
from packages.rca.temporal import causal_time, temporal_contradiction_certainty

RULE_IDS = (
    "S1_EXISTING_EPISODE_CONTAINMENT",
    "S2_PATH_CONTAINED_MANIFESTATION",
    "S3_DIRECTED_DOWNSTREAM_MANIFESTATION",
)
_FORBIDDEN_BLIND_KEYS = frozenset(
    {"ground_truth", "expected", "correct", "root_cause", "answer", "label"}
)
MANIFESTATION_KINDS = frozenset(
    {
        FindingKind.CONTAINER_FAILURE,
        FindingKind.RESOURCE_PRESSURE,
        FindingKind.DEPENDENCY_ERRORS,
        FindingKind.FAILURE_EVENT,
    }
)
INITIATING_KINDS = frozenset(
    {
        FindingKind.CONFIG_CHANGE,
        FindingKind.SPEC_CHANGE,
        FindingKind.IMAGE_CHANGE,
        FindingKind.SCALE_CHANGE,
        FindingKind.ROLLOUT_RESTART,
        FindingKind.OBJECT_CREATED,
        FindingKind.FAULT_INJECTION,
        FindingKind.FAULT_SCHEDULE,
        FindingKind.POLICY_CREATED,
        FindingKind.NETWORK_RESTRICTION,
        FindingKind.AUTOSCALING_FAILURE,
        FindingKind.TRAFFIC_INCREASE,
    }
)


def _json_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _json_value(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(k): _json_value(v) for k, v in sorted(value.items(), key=lambda x: str(x[0]))}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_json_value(item) for item in value]
    return value


def forbidden_blind_keys(value: Any, path: tuple[str, ...] = ()) -> tuple[str, ...]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            text = str(key).casefold()
            permitted = text == "ground_truth_loaded" and child is False
            if (text in _FORBIDDEN_BLIND_KEYS or text.startswith("gt_")) and not permitted:
                found.append(".".join((*path, str(key))))
            found.extend(forbidden_blind_keys(child, (*path, str(key))))
    elif isinstance(value, (tuple, list)):
        for index, child in enumerate(value):
            found.extend(forbidden_blind_keys(child, (*path, str(index))))
    return tuple(sorted(found))


def _canonical(value: Any) -> str:
    return value.canonical if hasattr(value, "canonical") else str(value)


def _kind(canonical: str) -> str:
    parts = canonical.split("/", 2)
    return parts[1] if len(parts) == 3 else "UNKNOWN"


def _finding_keys(findings: Sequence[Finding]) -> tuple[str, ...]:
    # Import lazily so this module remains an evaluator-only observer of the
    # production identity contract.
    from packages.evals.causal_semantics import finding_key

    return tuple(sorted(finding_key(item) for item in findings))


def _path_shape(paths: Sequence[Sequence[Any]]) -> tuple[tuple[tuple[str, str, str], ...], ...]:
    return tuple(
        tuple((_canonical(hop.source), hop.relation, _canonical(hop.target)) for hop in path)
        for path in paths
    )


def _path_kinds(paths: Sequence[Sequence[Any]]) -> tuple[tuple[tuple[str, str, str], ...], ...]:
    return tuple(
        tuple((hop.source.kind, hop.relation, hop.target.kind) for hop in path) for path in paths
    )


def _one_path_shape(path: Sequence[Any]) -> tuple[tuple[str, str, str], ...]:
    return tuple((_canonical(hop.source), hop.relation, _canonical(hop.target)) for hop in path)


def _times(findings: Sequence[Finding]) -> tuple[str, ...]:
    return tuple(
        sorted(item.isoformat() for item in (causal_time(finding) for finding in findings) if item)
    )


def _time_values(findings: Sequence[Finding]) -> tuple[datetime, ...]:
    return tuple(
        item for item in (causal_time(finding) for finding in findings) if item is not None
    )


def _temporal_order(supported: Sequence[Finding], unresolved: Sequence[Finding]) -> str:
    left = _time_values(supported)
    right = _time_values(unresolved)
    if not left or not right:
        return "OVERLAPPING_OR_UNKNOWN"
    if max(left) < min(right):
        return "SUPPORTED_PRECEDES_MANIFESTATION"
    if max(right) < min(left):
        return "MANIFESTATION_PRECEDES_SUPPORTED"
    return "OVERLAPPING_OR_UNKNOWN"


def blocker_class(hypothesis: Hypothesis) -> str:
    """Classify an unresolved episode without using actor kind or GT."""
    findings = tuple(hypothesis.findings)
    if not findings:
        return "NO_FINDING_CLASSIFICATION"
    kinds = {finding.kind for finding in findings}
    has_manifestation = bool(kinds & MANIFESTATION_KINDS)
    has_initiating_kind = bool(kinds & INITIATING_KINDS)
    if not hypothesis.initiating_findings and kinds <= MANIFESTATION_KINDS:
        return "MANIFESTATION_ONLY"
    if has_manifestation and has_initiating_kind:
        return "MIXED_INITIATING_AND_MANIFESTATION"
    if has_initiating_kind:
        certainty = tuple(
            temporal_contradiction_certainty(finding, RankingConfig().verification_onset_grace)
            for finding in findings
            if finding.kind in INITIATING_KINDS
        )
        if any(value.value in {"INTERVAL_UNCERTAIN", "UNKNOWN"} for value in certainty):
            return "INITIATING_KIND_INTERVAL_UNCERTAIN"
    return "OTHER_UNRESOLVED"


@dataclass(frozen=True)
class SupportedEpisodeSnapshot:
    hypothesis_id: str
    actor: str
    members: tuple[str, ...]
    manifestations: tuple[str, ...]
    linked_symptoms: tuple[str, ...]
    finding_kinds: tuple[str, ...]
    finding_keys: tuple[str, ...]
    initiating_times: tuple[str, ...]
    causal_path_shape: tuple[tuple[tuple[str, str, str], ...], ...]


@dataclass(frozen=True)
class UnresolvedBlockerSnapshot:
    hypothesis_id: str
    actor: str
    actor_kind: str
    evidence_class: str
    finding_kinds: tuple[str, ...]
    temporal_roles: tuple[str, ...]
    reason_codes: tuple[str, ...]
    linked_symptoms: tuple[str, ...]
    supported_upstream_count: int
    supported_downstream_count: int
    episode_membership_relation: str
    path_contained_relation: str
    directed_downstream_relation: str
    independent_initiating_evidence: bool
    fan_in: int
    fan_out: int
    infrastructure_class: str


@dataclass(frozen=True)
class SupportedUnresolvedPair:
    supported_id: str
    supported_actor: str
    unresolved_id: str
    unresolved_actor: str
    unresolved_class: str
    forward_path: tuple[tuple[str, str, str], ...]
    reverse_path: tuple[tuple[str, str, str], ...]
    unresolved_on_supported_path: bool
    unresolved_member_of_supported: bool
    unresolved_manifestation_of_supported: bool
    shared_linked_symptoms: tuple[str, ...]
    supported_initiating_times: tuple[str, ...]
    unresolved_manifestation_times: tuple[str, ...]
    temporal_order: str
    independent_initiating_evidence: bool
    s1_eligible: bool
    s2_eligible: bool
    s3_eligible: bool


@dataclass(frozen=True)
class SubsumptionCounterfactual:
    rule_id: str
    supported_ids: tuple[str, ...]
    unresolved_ids: tuple[str, ...]
    contradicted_ids: tuple[str, ...]
    subsumed_ids: tuple[str, ...]
    counterfactual_resolution: str
    counterfactual_selected_id: str | None


@dataclass(frozen=True)
class SubsumptionRuleResult:
    rule_id: str
    subsumed_ids: tuple[str, ...]
    pair_keys: tuple[str, ...]
    counterfactual: SubsumptionCounterfactual


@dataclass(frozen=True)
class UnresolvedSubsumptionBlindAudit:
    scenario_id: str
    production_resolution: str
    supported_episodes: tuple[SupportedEpisodeSnapshot, ...]
    unresolved_blockers: tuple[UnresolvedBlockerSnapshot, ...]
    pairwise_relations: tuple[SupportedUnresolvedPair, ...]
    rule_results: tuple[SubsumptionRuleResult, ...]
    counterfactuals: tuple[SubsumptionCounterfactual, ...]
    recurrence_diagnostics: tuple[tuple[str, int], ...]
    shared_infrastructure_diagnostics: tuple[tuple[str, str], ...]

    def as_dict(self) -> dict[str, object]:
        return cast(dict[str, object], _json_value(asdict(self)))


def _state_by_id(case: Case, diagnosis: Any) -> dict[str, str]:
    trace = diagnosis.resolution_trace
    result: dict[str, str] = {}
    if trace is None:
        return result
    result.update({identifier: "SUPPORTED" for identifier in trace.plausible_hypotheses})
    result.update({identifier: "UNRESOLVED" for identifier in trace.unresolved_hypotheses})
    result.update({identifier: "CONTRADICTED" for identifier in trace.eliminated_hypotheses})
    for hypothesis in case.hypotheses:
        audit = next(
            (
                item
                for item in trace.hypothesis_audits
                if item.hypothesis_id == hypothesis.hypothesis_id
            ),
            None,
        )
        if audit is not None:
            result[hypothesis.hypothesis_id] = audit.epistemic_state.value
    return result


def _reason_by_id(diagnosis: Any) -> dict[str, tuple[str, ...]]:
    trace = diagnosis.resolution_trace
    if trace is None:
        return {}
    return {
        item.hypothesis_id: tuple(sorted(reason.value for reason in item.plausibility_reasons))
        for item in trace.hypothesis_audits
    }


def _diagnostic_reasons(
    hypothesis: Hypothesis, reasons: Mapping[str, tuple[str, ...]]
) -> tuple[str, ...]:
    """Use only the per-hypothesis reason codes retained by production."""
    return reasons.get(hypothesis.hypothesis_id, ())


def _fan_counts(case: Case, actor: Any) -> tuple[int, int]:
    reachable = case.topology.causal_reachable(actor, max_depth=6)
    fan_out = len(reachable) - 1
    incoming = 0
    for candidate in case.hypotheses:
        if candidate.causal_actor != actor and case.topology.causal_path(
            candidate.causal_actor, {actor}, max_depth=6
        ):
            incoming += 1
    return incoming, fan_out


def _infrastructure_class(fan_in: int, fan_out: int) -> str:
    if fan_in + fan_out >= 3:
        return "HIGH_FAN_IN_SHARED_INFRASTRUCTURE"
    if fan_in or fan_out:
        return "INCIDENT_LOCAL"
    return "UNKNOWN"


def _pair_eligibility(
    supported: Hypothesis,
    unresolved: Hypothesis,
    case: Any,
) -> SupportedUnresolvedPair:
    forward = (
        case.topology.causal_path(supported.causal_actor, {unresolved.causal_actor}, max_depth=6)
        or ()
    )
    reverse = (
        case.topology.causal_path(unresolved.causal_actor, {supported.causal_actor}, max_depth=6)
        or ()
    )
    u_actor = _canonical(unresolved.causal_actor)
    s_members = {_canonical(item) for item in supported.members}
    s_manifestations = {_canonical(item) for item in supported.manifestations}
    on_stored_path = any(
        u_actor in {_canonical(hop.source), _canonical(hop.target)}
        for path in supported.causal_paths
        for hop in path
    )
    shared = tuple(sorted(set(supported.linked_symptoms).intersection(unresolved.linked_symptoms)))
    manifestation_findings = tuple(
        finding for finding in unresolved.findings if finding.kind in MANIFESTATION_KINDS
    )
    independent = bool(unresolved.initiating_findings)
    base = (
        unresolved.causal_explanation in {"PATH", "DIRECT"}
        and not independent
        and not reverse
        and unresolved.findings
        and blocker_class(unresolved) == "MANIFESTATION_ONLY"
    )
    temporal = _temporal_order(supported.initiating_findings, manifestation_findings)
    pair = SupportedUnresolvedPair(
        supported_id=supported.hypothesis_id,
        supported_actor=_canonical(supported.causal_actor),
        unresolved_id=unresolved.hypothesis_id,
        unresolved_actor=u_actor,
        unresolved_class=blocker_class(unresolved),
        forward_path=_one_path_shape(forward) if forward else (),
        reverse_path=_one_path_shape(reverse) if reverse else (),
        unresolved_on_supported_path=on_stored_path,
        unresolved_member_of_supported=u_actor in s_members,
        unresolved_manifestation_of_supported=u_actor in s_manifestations,
        shared_linked_symptoms=shared,
        supported_initiating_times=_times(supported.initiating_findings),
        unresolved_manifestation_times=_times(manifestation_findings),
        temporal_order=temporal,
        independent_initiating_evidence=independent,
        s1_eligible=bool(base and (u_actor in s_members or u_actor in s_manifestations)),
        s2_eligible=bool(
            base and on_stored_path and shared and temporal == "SUPPORTED_PRECEDES_MANIFESTATION"
        ),
        s3_eligible=bool(
            base and forward and shared and temporal == "SUPPORTED_PRECEDES_MANIFESTATION"
        ),
    )
    return pair


def _counterfactual(
    rule_id: str,
    supported: Sequence[Hypothesis],
    unresolved: Sequence[Hypothesis],
    contradicted: Sequence[Hypothesis],
    subsumed_ids: set[str],
    diagnosis: Any,
) -> SubsumptionCounterfactual:
    supported_ids = tuple(sorted(item.hypothesis_id for item in supported))
    unresolved_ids = tuple(
        sorted(item.hypothesis_id for item in unresolved if item.hypothesis_id not in subsumed_ids)
    )
    contradicted_ids = tuple(sorted(item.hypothesis_id for item in contradicted))
    selected: str | None = None
    if unresolved_ids:
        resolution = "AMBIGUOUS" if supported_ids else "INSUFFICIENT_EVIDENCE"
    elif len(supported_ids) == 1:
        selected = supported_ids[0]
        resolution = "RESOLVED_COUNTERFACTUAL"
    elif len(supported_ids) > 1:
        trace = diagnosis.resolution_trace
        relations = trace.dominance_relations if trace is not None else ()
        winners = tuple(
            item
            for item in supported_ids
            if all(
                item == other
                or any(
                    relation.stronger_hypothesis_id == item
                    and relation.weaker_hypothesis_id == other
                    for relation in relations
                )
                for other in supported_ids
            )
        )
        if len(winners) == 1:
            selected = winners[0]
            resolution = "RESOLVED_COUNTERFACTUAL"
        else:
            resolution = "AMBIGUOUS"
    else:
        resolution = "INSUFFICIENT_EVIDENCE"
    return SubsumptionCounterfactual(
        rule_id=rule_id,
        supported_ids=supported_ids,
        unresolved_ids=unresolved_ids,
        contradicted_ids=contradicted_ids,
        subsumed_ids=tuple(sorted(subsumed_ids)),
        counterfactual_resolution=resolution,
        counterfactual_selected_id=selected,
    )


def build_blind_audit(source: ObservationSource) -> UnresolvedSubsumptionBlindAudit:
    """Build one deterministic FULL-source subsumption audit."""
    case = build_case(source)
    diagnosis = diagnose_case(case)
    states = _state_by_id(case, diagnosis)
    reasons = _reason_by_id(diagnosis)
    supported = tuple(
        sorted(
            (item for item in case.hypotheses if states.get(item.hypothesis_id) == "SUPPORTED"),
            key=lambda item: item.hypothesis_id,
        )
    )
    unresolved = tuple(
        sorted(
            (item for item in case.hypotheses if states.get(item.hypothesis_id) == "UNRESOLVED"),
            key=lambda item: item.hypothesis_id,
        )
    )
    contradicted = tuple(
        sorted(
            (item for item in case.hypotheses if states.get(item.hypothesis_id) == "CONTRADICTED"),
            key=lambda item: item.hypothesis_id,
        )
    )
    supported_snapshots = tuple(
        SupportedEpisodeSnapshot(
            hypothesis_id=item.hypothesis_id,
            actor=_canonical(item.causal_actor),
            members=tuple(sorted(_canonical(value) for value in item.members)),
            manifestations=tuple(sorted(_canonical(value) for value in item.manifestations)),
            linked_symptoms=tuple(sorted(item.linked_symptoms)),
            finding_kinds=tuple(sorted(finding.kind.value for finding in item.findings)),
            finding_keys=_finding_keys(item.findings),
            initiating_times=_times(item.initiating_findings),
            causal_path_shape=_path_kinds(item.causal_paths),
        )
        for item in supported
    )
    pairs = tuple(_pair_eligibility(s, u, case) for s in supported for u in unresolved)
    blocker_snapshots: list[UnresolvedBlockerSnapshot] = []
    for item in unresolved:
        actor = _canonical(item.causal_actor)
        related = [pair for pair in pairs if pair.unresolved_id == item.hypothesis_id]
        upstream = sum(bool(pair.forward_path) for pair in related)
        downstream = sum(bool(pair.reverse_path) for pair in related)
        membership = (
            "SUPPORTED_EPISODE_MEMBER"
            if any(
                pair.unresolved_member_of_supported or pair.unresolved_manifestation_of_supported
                for pair in related
            )
            else "NONE"
        )
        stored = (
            "ON_SUPPORTED_PATH"
            if any(pair.unresolved_on_supported_path for pair in related)
            else "NONE"
        )
        directed = "DIRECTED_DOWNSTREAM" if upstream else "NONE"
        fan_in, fan_out = _fan_counts(case, item.causal_actor)
        blocker_snapshots.append(
            UnresolvedBlockerSnapshot(
                hypothesis_id=item.hypothesis_id,
                actor=actor,
                actor_kind=item.causal_actor.kind,
                evidence_class=blocker_class(item),
                finding_kinds=tuple(sorted(finding.kind.value for finding in item.findings)),
                temporal_roles=tuple(
                    sorted(finding.temporal_role.value for finding in item.findings)
                ),
                reason_codes=_diagnostic_reasons(item, reasons),
                linked_symptoms=tuple(sorted(item.linked_symptoms)),
                supported_upstream_count=upstream,
                supported_downstream_count=downstream,
                episode_membership_relation=membership,
                path_contained_relation=stored,
                directed_downstream_relation=directed,
                independent_initiating_evidence=bool(item.initiating_findings),
                fan_in=fan_in,
                fan_out=fan_out,
                infrastructure_class=_infrastructure_class(fan_in, fan_out),
            )
        )
    rule_results: list[SubsumptionRuleResult] = []
    counterfactuals: list[SubsumptionCounterfactual] = []
    for rule_id, attr in zip(RULE_IDS, ("s1_eligible", "s2_eligible", "s3_eligible"), strict=True):
        eligible = [pair for pair in pairs if getattr(pair, attr)]
        subsumed = {pair.unresolved_id for pair in eligible}
        result = SubsumptionRuleResult(
            rule_id=rule_id,
            subsumed_ids=tuple(sorted(subsumed)),
            pair_keys=tuple(
                sorted(f"{pair.supported_id}->{pair.unresolved_id}" for pair in eligible)
            ),
            counterfactual=_counterfactual(
                rule_id, supported, unresolved, contradicted, subsumed, diagnosis
            ),
        )
        rule_results.append(result)
        counterfactuals.append(result.counterfactual)
    recurrence = tuple(sorted(Counter(item.actor for item in blocker_snapshots).items()))
    signatures = Counter(
        ";".join(
            f"{kind}:{role}"
            for kind, role in zip(item.finding_kinds, item.temporal_roles, strict=False)
        )
        for item in blocker_snapshots
    )
    recurrence_items = list(recurrence)
    recurrence_items.extend(
        (f"manifestation_signature:{key}", value) for key, value in signatures.items()
    )
    recurrence = tuple(sorted(recurrence_items))
    infra = tuple(sorted((item.actor, item.infrastructure_class) for item in blocker_snapshots))
    return UnresolvedSubsumptionBlindAudit(
        scenario_id=case.incident_id,
        production_resolution=diagnosis.resolution.value,
        supported_episodes=supported_snapshots,
        unresolved_blockers=tuple(blocker_snapshots),
        pairwise_relations=tuple(
            sorted(pairs, key=lambda item: (item.supported_id, item.unresolved_id))
        ),
        rule_results=tuple(rule_results),
        counterfactuals=tuple(counterfactuals),
        recurrence_diagnostics=recurrence,
        shared_infrastructure_diagnostics=infra,
    )


def _tuple(value: Any) -> tuple[Any, ...]:
    return tuple(value or ())


def blind_audit_from_dict(value: Mapping[str, Any]) -> UnresolvedSubsumptionBlindAudit:
    def supported(item: Mapping[str, Any]) -> SupportedEpisodeSnapshot:
        return SupportedEpisodeSnapshot(
            hypothesis_id=str(item["hypothesis_id"]),
            actor=str(item["actor"]),
            members=tuple(item.get("members", ())),
            manifestations=tuple(item.get("manifestations", ())),
            linked_symptoms=tuple(item.get("linked_symptoms", ())),
            finding_kinds=tuple(item.get("finding_kinds", ())),
            finding_keys=tuple(item.get("finding_keys", ())),
            initiating_times=tuple(item.get("initiating_times", ())),
            causal_path_shape=tuple(
                tuple(tuple(hop) for hop in path) for path in item.get("causal_path_shape", ())
            ),
        )

    def blocker(item: Mapping[str, Any]) -> UnresolvedBlockerSnapshot:
        return UnresolvedBlockerSnapshot(
            hypothesis_id=str(item["hypothesis_id"]),
            actor=str(item["actor"]),
            actor_kind=str(item["actor_kind"]),
            evidence_class=str(item["evidence_class"]),
            finding_kinds=tuple(item.get("finding_kinds", ())),
            temporal_roles=tuple(item.get("temporal_roles", ())),
            reason_codes=tuple(item.get("reason_codes", ())),
            linked_symptoms=tuple(item.get("linked_symptoms", ())),
            supported_upstream_count=int(item.get("supported_upstream_count", 0)),
            supported_downstream_count=int(item.get("supported_downstream_count", 0)),
            episode_membership_relation=str(item.get("episode_membership_relation", "NONE")),
            path_contained_relation=str(item.get("path_contained_relation", "NONE")),
            directed_downstream_relation=str(item.get("directed_downstream_relation", "NONE")),
            independent_initiating_evidence=bool(
                item.get("independent_initiating_evidence", False)
            ),
            fan_in=int(item.get("fan_in", 0)),
            fan_out=int(item.get("fan_out", 0)),
            infrastructure_class=str(item.get("infrastructure_class", "UNKNOWN")),
        )

    def pair(item: Mapping[str, Any]) -> SupportedUnresolvedPair:
        return SupportedUnresolvedPair(
            supported_id=str(item["supported_id"]),
            supported_actor=str(item["supported_actor"]),
            unresolved_id=str(item["unresolved_id"]),
            unresolved_actor=str(item["unresolved_actor"]),
            unresolved_class=str(item["unresolved_class"]),
            forward_path=tuple(tuple(hop) for hop in item.get("forward_path", ())),
            reverse_path=tuple(tuple(hop) for hop in item.get("reverse_path", ())),
            unresolved_on_supported_path=bool(item.get("unresolved_on_supported_path", False)),
            unresolved_member_of_supported=bool(item.get("unresolved_member_of_supported", False)),
            unresolved_manifestation_of_supported=bool(
                item.get("unresolved_manifestation_of_supported", False)
            ),
            shared_linked_symptoms=tuple(item.get("shared_linked_symptoms", ())),
            supported_initiating_times=tuple(item.get("supported_initiating_times", ())),
            unresolved_manifestation_times=tuple(item.get("unresolved_manifestation_times", ())),
            temporal_order=str(item.get("temporal_order", "OVERLAPPING_OR_UNKNOWN")),
            independent_initiating_evidence=bool(
                item.get("independent_initiating_evidence", False)
            ),
            s1_eligible=bool(item.get("s1_eligible", False)),
            s2_eligible=bool(item.get("s2_eligible", False)),
            s3_eligible=bool(item.get("s3_eligible", False)),
        )

    def cf(item: Mapping[str, Any]) -> SubsumptionCounterfactual:
        return SubsumptionCounterfactual(
            rule_id=str(item["rule_id"]),
            supported_ids=tuple(item.get("supported_ids", ())),
            unresolved_ids=tuple(item.get("unresolved_ids", ())),
            contradicted_ids=tuple(item.get("contradicted_ids", ())),
            subsumed_ids=tuple(item.get("subsumed_ids", ())),
            counterfactual_resolution=str(item["counterfactual_resolution"]),
            counterfactual_selected_id=item.get("counterfactual_selected_id"),
        )

    def rr(item: Mapping[str, Any]) -> SubsumptionRuleResult:
        return SubsumptionRuleResult(
            rule_id=str(item["rule_id"]),
            subsumed_ids=tuple(item.get("subsumed_ids", ())),
            pair_keys=tuple(item.get("pair_keys", ())),
            counterfactual=cf(cast(Mapping[str, Any], item["counterfactual"])),
        )

    return UnresolvedSubsumptionBlindAudit(
        scenario_id=str(value["scenario_id"]),
        production_resolution=str(value["production_resolution"]),
        supported_episodes=tuple(supported(item) for item in value.get("supported_episodes", ())),
        unresolved_blockers=tuple(blocker(item) for item in value.get("unresolved_blockers", ())),
        pairwise_relations=tuple(pair(item) for item in value.get("pairwise_relations", ())),
        rule_results=tuple(rr(item) for item in value.get("rule_results", ())),
        counterfactuals=tuple(cf(item) for item in value.get("counterfactuals", ())),
        recurrence_diagnostics=tuple(
            (str(key), int(count)) for key, count in value.get("recurrence_diagnostics", ())
        ),
        shared_infrastructure_diagnostics=tuple(
            (str(key), str(classification))
            for key, classification in value.get("shared_infrastructure_diagnostics", ())
        ),
    )


__all__ = [
    "MANIFESTATION_KINDS",
    "RULE_IDS",
    "SupportedEpisodeSnapshot",
    "UnresolvedBlockerSnapshot",
    "SupportedUnresolvedPair",
    "SubsumptionCounterfactual",
    "SubsumptionRuleResult",
    "UnresolvedSubsumptionBlindAudit",
    "blind_audit_from_dict",
    "blocker_class",
    "build_blind_audit",
    "forbidden_blind_keys",
]

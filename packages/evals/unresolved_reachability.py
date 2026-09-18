"""Ground-truth-blind reachability diagnostics for unresolved RCA episodes.

This module only observes the existing full, bounded, and exhaustive-active
production paths.  It deliberately keeps the evaluator overlay out of the
blind builders so a query policy cannot be tuned against benchmark answers.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, cast

from packages.evals.causal_semantics import finding_key
from packages.rca.engine import Case, build_case, diagnose_case
from packages.rca.investigation.control_matrix import (
    _NORMALIZER_FINDING_KINDS_BY_CAPABILITY,
    PromotionAudit,
    semantic_finding_key,
)
from packages.rca.investigation.environment import initial_view, investigation_backend
from packages.rca.investigation.multi_step_search import legal_query_choices, query_templates
from packages.rca.investigation.state import InvestigationTool
from packages.rca.investigation.tools import default_tools
from packages.rca.model import (
    Diagnosis,
    Finding,
    GapOutcomeKind,
    GapResolvability,
    Hypothesis,
)
from packages.rca.source import ObservationSource

_JSON_FORBIDDEN = frozenset(
    {"ground_truth", "expected", "correct", "root_cause", "answer", "label"}
)
_STATE_VALUES = frozenset({"SUPPORTED", "UNRESOLVED", "CONTRADICTED"})


def _json_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _json_value(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_json_value(item) for item in value]
    return value


def forbidden_blind_keys(value: Any, path: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Find evaluator-looking keys in a serialized blind payload."""
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            text = str(key).casefold()
            permitted_metadata = text == "ground_truth_loaded" and child is False
            if (text in _JSON_FORBIDDEN or text.startswith("gt_")) and not permitted_metadata:
                found.append(".".join((*path, str(key))))
            found.extend(forbidden_blind_keys(child, (*path, str(key))))
    elif isinstance(value, (tuple, list)):
        for index, child in enumerate(value):
            found.extend(forbidden_blind_keys(child, (*path, str(index))))
    return tuple(sorted(found))


@dataclass(frozen=True)
class EpisodeProjection:
    causal_actor: str


@dataclass(frozen=True)
class EpisodeStateSnapshot:
    actor: str
    hypothesis_id: str
    members: tuple[str, ...]
    manifestations: tuple[str, ...]
    epistemic_state: str
    reason_codes: tuple[str, ...]
    verification_decision: str | None
    finding_kinds: tuple[str, ...]
    finding_keys: tuple[str, ...]
    initiating_finding_keys: tuple[str, ...]
    supporting_finding_keys: tuple[str, ...]
    contradictory_finding_keys: tuple[str, ...]
    causal_path_shape: tuple[tuple[tuple[str, str, str], ...], ...]


@dataclass(frozen=True)
class DiagnosisEpistemicSnapshot:
    resolution: str
    supported_actors: tuple[str, ...]
    unresolved_actors: tuple[str, ...]
    contradicted_actors: tuple[str, ...]
    leading_actors: tuple[str, ...]
    episodes: tuple[EpisodeStateSnapshot, ...]


@dataclass(frozen=True)
class EpisodeReachability:
    actor: str
    seed_state: str | None
    active_state: str | None
    full_state: str | None
    reachability_class: str


@dataclass(frozen=True)
class QueryEpistemicEffect:
    observation_identity: str
    gap_id: str
    dimension: str
    capability: str
    target: str
    query_template: str
    raw_records: int
    returned_refs: tuple[str, ...]
    new_refs: tuple[str, ...]
    normalized_findings: tuple[str, ...]
    new_findings: tuple[str, ...]
    episode_states_before: tuple[tuple[str, str], ...]
    episode_states_after: tuple[tuple[str, str], ...]
    transitions: tuple[str, ...]
    effect_class: str
    prebuild_outcome: str


@dataclass(frozen=True)
class PolicyContextAudit:
    gap_hypothesis_ids: tuple[str, ...]
    policy_visible_hypothesis_ids: tuple[str, ...]
    missing_hypothesis_ids: tuple[str, ...]
    classification: str


@dataclass(frozen=True)
class GapContractAudit:
    gap_id: str
    dimension: str
    hypothesis_ids: tuple[str, ...]
    discriminating_outcomes: tuple[str, ...]
    authorized_queries: tuple[tuple[str, str], ...]
    declared_support_path: bool
    declared_contradiction_path: bool
    resolvable: bool


@dataclass(frozen=True)
class ParityAudit:
    full_findings: tuple[str, ...]
    active_findings: tuple[str, ...]
    shared_findings: tuple[str, ...]
    full_only_findings: tuple[str, ...]
    active_only_findings: tuple[str, ...]
    full_only_reasons: tuple[tuple[str, str], ...]
    finding_kind_counts: tuple[tuple[str, int, int], ...]


@dataclass(frozen=True)
class UnresolvedReachabilityBlindAudit:
    scenario_id: str
    full: DiagnosisEpistemicSnapshot
    seed: DiagnosisEpistemicSnapshot
    exhaustive_active: DiagnosisEpistemicSnapshot
    episode_reachability: tuple[EpisodeReachability, ...]
    query_effects: tuple[QueryEpistemicEffect, ...]
    policy_context_audit: PolicyContextAudit
    gap_contract_audit: tuple[GapContractAudit, ...]
    parity_audit: ParityAudit
    exhaustive_fixed_point: bool
    fixed_point_reason: str
    fixed_point_legal_unseen: int
    full_blockers: tuple[tuple[str, str, str], ...]

    def as_dict(self) -> dict[str, object]:
        return cast(dict[str, object], _json_value(asdict(self)))


def _finding_keys(items: Sequence[Finding]) -> tuple[str, ...]:
    return tuple(sorted(finding_key(item) for item in items))


def _path_shape(hypothesis: Hypothesis) -> tuple[tuple[tuple[str, str, str], ...], ...]:
    return tuple(
        tuple((hop.source.kind, hop.relation, hop.target.kind) for hop in path)
        for path in hypothesis.causal_paths
    )


def _trace_state(diagnosis: Diagnosis, hypothesis_id: str) -> str:
    trace = diagnosis.resolution_trace
    if trace is None:
        return "UNKNOWN"
    if hypothesis_id in trace.plausible_hypotheses:
        return "SUPPORTED"
    if hypothesis_id in trace.unresolved_hypotheses:
        return "UNRESOLVED"
    if hypothesis_id in trace.eliminated_hypotheses:
        return "CONTRADICTED"
    audit = next(
        (item for item in trace.hypothesis_audits if item.hypothesis_id == hypothesis_id), None
    )
    return audit.epistemic_state.value if audit is not None else "UNKNOWN"


def _audit_for(diagnosis: Diagnosis, hypothesis_id: str) -> Any | None:
    trace = diagnosis.resolution_trace
    if trace is None:
        return None
    return next(
        (item for item in trace.hypothesis_audits if item.hypothesis_id == hypothesis_id), None
    )


def diagnosis_snapshot(case: Case, diagnosis: Diagnosis) -> DiagnosisEpistemicSnapshot:
    """Capture the production epistemic partition, never recomputing it."""
    episodes: list[EpisodeStateSnapshot] = []
    for hypothesis in sorted(case.hypotheses, key=lambda item: item.hypothesis_id):
        audit = _audit_for(diagnosis, hypothesis.hypothesis_id)
        verification = audit.verification.decision.value if audit and audit.verification else None
        episodes.append(
            EpisodeStateSnapshot(
                actor=hypothesis.causal_actor.canonical,
                hypothesis_id=hypothesis.hypothesis_id,
                members=tuple(sorted(item.canonical for item in hypothesis.members)),
                manifestations=tuple(sorted(item.canonical for item in hypothesis.manifestations)),
                epistemic_state=_trace_state(diagnosis, hypothesis.hypothesis_id),
                reason_codes=tuple(sorted(reason.value for reason in audit.plausibility_reasons))
                if audit
                else (),
                verification_decision=verification,
                finding_kinds=tuple(sorted(item.kind.value for item in hypothesis.findings)),
                finding_keys=_finding_keys(hypothesis.findings),
                initiating_finding_keys=_finding_keys(hypothesis.initiating_findings),
                supporting_finding_keys=_finding_keys(hypothesis.supporting_findings),
                contradictory_finding_keys=_finding_keys(hypothesis.contradictory_findings),
                causal_path_shape=_path_shape(hypothesis),
            )
        )
    by_state: dict[str, list[str]] = {state: [] for state in _STATE_VALUES}
    for episode in episodes:
        if episode.epistemic_state in by_state:
            by_state[episode.epistemic_state].append(episode.actor)
    leading_ids = (
        diagnosis.resolution_trace.leading_hypothesis_ids if diagnosis.resolution_trace else ()
    )
    leading = tuple(sorted({item.actor for item in episodes if item.hypothesis_id in leading_ids}))
    return DiagnosisEpistemicSnapshot(
        resolution=diagnosis.resolution.value,
        supported_actors=tuple(sorted(by_state["SUPPORTED"])),
        unresolved_actors=tuple(sorted(by_state["UNRESOLVED"])),
        contradicted_actors=tuple(sorted(by_state["CONTRADICTED"])),
        leading_actors=leading,
        episodes=tuple(episodes),
    )


def _actor_map(snapshot: DiagnosisEpistemicSnapshot) -> dict[str, tuple[EpisodeStateSnapshot, ...]]:
    result: dict[str, list[EpisodeStateSnapshot]] = {}
    for item in snapshot.episodes:
        result.setdefault(item.actor, []).append(item)
    return {actor: tuple(values) for actor, values in result.items()}


def _related_projection(
    actor: str, source: DiagnosisEpistemicSnapshot, target: DiagnosisEpistemicSnapshot
) -> bool:
    source_items = _actor_map(source).get(actor, ())
    source_entities = {actor, *(entity for item in source_items for entity in item.members)}
    return any(
        bool(source_entities.intersection({item.actor, *item.members})) for item in target.episodes
    )


def _reachability_class(seed: str, active: str | None, full: str | None) -> str:
    if full is None:
        return "EPISODE_NOT_COMPARABLE"
    if full == "UNRESOLVED":
        return (
            "ACTIVE_EXCEEDS_FULL_CONTROL"
            if active in {"SUPPORTED", "CONTRADICTED"}
            else "FULL_SEMANTIC_CEILING_UNRESOLVED"
        )
    if active is None:
        if full == "SUPPORTED":
            return "ACTIVE_BELOW_FULL_SUPPORTED"
        if full == "CONTRADICTED":
            return "ACTIVE_BELOW_FULL_CONTRADICTED"
        return "EPISODE_NOT_COMPARABLE"
    if active == full:
        return f"ACTIVE_REACHES_FULL_{full}"
    if full == "SUPPORTED":
        return "ACTIVE_BELOW_FULL_SUPPORTED"
    if full == "CONTRADICTED":
        return "ACTIVE_BELOW_FULL_CONTRADICTED"
    return "ACTIVE_DIVERGES_FROM_FULL"


def episode_reachability(
    seed: DiagnosisEpistemicSnapshot,
    active: DiagnosisEpistemicSnapshot,
    full: DiagnosisEpistemicSnapshot,
) -> tuple[EpisodeReachability, ...]:
    result: list[EpisodeReachability] = []
    # Keep full/active unresolved actors even when the bounded seed omitted
    # them; otherwise the semantic ceiling would disappear from the audit.
    actors = tuple(
        sorted(
            set(seed.unresolved_actors)
            | set(active.unresolved_actors)
            | set(full.unresolved_actors)
        )
    )
    for actor in actors:
        seed_items = _actor_map(seed).get(actor, ())
        active_items = _actor_map(active).get(actor, ())
        full_items = _actor_map(full).get(actor, ())
        if len(seed_items) > 1 or len(active_items) > 1 or len(full_items) > 1:
            result.append(
                EpisodeReachability(
                    actor,
                    seed_items[0].epistemic_state if seed_items else None,
                    None,
                    None,
                    "PROJECTION_COLLISION",
                )
            )
            continue
        active_item = active_items[0] if active_items else None
        full_item = full_items[0] if full_items else None
        if active_item is None and _related_projection(actor, seed, active):
            active_class = "EPISODE_REPROJECTED"
        else:
            active_class = None
        if full_item is None and _related_projection(actor, seed, full):
            full_class = "EPISODE_REPROJECTED"
        else:
            full_class = None
        if full_item is None and full_class is None:
            result.append(
                EpisodeReachability(
                    actor,
                    seed_items[0].epistemic_state if seed_items else None,
                    active_item.epistemic_state if active_item else None,
                    None,
                    "FULL_SOURCE_EPISODE_ABSENT",
                )
            )
            continue
        if active_class or full_class:
            result.append(
                EpisodeReachability(
                    actor,
                    seed_items[0].epistemic_state if seed_items else None,
                    active_item.epistemic_state if active_item else active_class,
                    full_item.epistemic_state if full_item else full_class,
                    "EPISODE_REPROJECTED",
                )
            )
            continue
        active_state = active_item.epistemic_state if active_item else None
        full_state = full_item.epistemic_state if full_item else None
        result.append(
            EpisodeReachability(
                actor,
                seed_items[0].epistemic_state if seed_items else None,
                active_state,
                full_state,
                _reachability_class(
                    seed_items[0].epistemic_state if seed_items else "UNRESOLVED",
                    active_state,
                    full_state,
                ),
            )
        )
    return tuple(result)


def _transitions(
    before: tuple[tuple[str, str], ...], after: tuple[tuple[str, str], ...]
) -> tuple[str, ...]:
    left = dict(before)
    right = dict(after)
    if len(before) != len(left) or len(after) != len(right):
        return ("PROJECTION_COLLISION",)
    transitions: list[str] = []
    for actor in sorted(set(left) | set(right)):
        previous, current = left.get(actor), right.get(actor)
        if previous is None:
            transitions.append("EPISODE_CREATED")
        elif current is None:
            transitions.append("EPISODE_DISAPPEARED")
        elif previous == current == "UNRESOLVED":
            transitions.append("UNRESOLVED_STAYS_UNRESOLVED")
        elif previous == "UNRESOLVED" and current == "SUPPORTED":
            transitions.append("UNRESOLVED_TO_SUPPORTED")
        elif previous == "UNRESOLVED" and current == "CONTRADICTED":
            transitions.append("UNRESOLVED_TO_CONTRADICTED")
        elif previous == "SUPPORTED" and current == "CONTRADICTED":
            transitions.append("SUPPORTED_TO_CONTRADICTED")
        elif previous == current == "SUPPORTED":
            transitions.append("SUPPORTED_STAYS_SUPPORTED")
        elif previous == current == "CONTRADICTED":
            transitions.append("CONTRADICTED_STAYS_CONTRADICTED")
        elif previous != current:
            transitions.append("EPISODE_REPROJECTED")
    return tuple(sorted(set(transitions)))


def _effect_class(audit: PromotionAudit, transitions: tuple[str, ...]) -> str:
    if audit.audit_defect or audit.attempt_outcome == "TOOL_ERROR":
        return "TOOL_ERROR"
    if audit.raw_records == 0:
        return "NO_RAW_RECORDS"
    if not audit.new_refs:
        return "ALREADY_KNOWN_RAW"
    if not audit.new_finding_core_keys:
        return "NOVEL_RAW_NO_FINDING"
    if "UNRESOLVED_TO_SUPPORTED" in transitions:
        return "UNRESOLVED_TO_SUPPORTED"
    if "UNRESOLVED_TO_CONTRADICTED" in transitions:
        return "UNRESOLVED_TO_CONTRADICTED"
    if any(
        item in transitions
        for item in {
            "EPISODE_CREATED",
            "EPISODE_DISAPPEARED",
            "EPISODE_REPROJECTED",
            "PROJECTION_COLLISION",
        }
    ):
        return "EPISODE_MODEL_CHANGED"
    return "FINDING_NO_EPISTEMIC_EFFECT"


def query_effects(audits: Sequence[PromotionAudit]) -> tuple[QueryEpistemicEffect, ...]:
    effects: list[QueryEpistemicEffect] = []
    for audit in audits:
        transitions = _transitions(audit.episode_states_before, audit.episode_states_after)
        effects.append(
            QueryEpistemicEffect(
                observation_identity=audit.observation_identity,
                gap_id=audit.gap_id,
                dimension=audit.dimension,
                capability=audit.capability,
                target=audit.target,
                query_template=audit.query_template,
                raw_records=audit.raw_records,
                returned_refs=audit.returned_refs,
                new_refs=audit.new_refs,
                normalized_findings=audit.normalized_finding_core_keys,
                new_findings=audit.new_finding_core_keys,
                episode_states_before=audit.episode_states_before,
                episode_states_after=audit.episode_states_after,
                transitions=transitions,
                effect_class=_effect_class(audit, transitions),
                prebuild_outcome=audit.observation_outcome,
            )
        )
    return tuple(effects)


def policy_context_audit(diagnosis: Diagnosis) -> PolicyContextAudit:
    gaps = tuple(
        gap
        for gap in diagnosis.information_gaps
        if gap.resolvability is GapResolvability.RESOLVABLE and gap.authorized_queries
    )
    required = tuple(sorted({item for gap in gaps for item in gap.hypothesis_ids}))
    visible_hypotheses = diagnosis.ambiguous_hypotheses or diagnosis.alternative_hypotheses[:8]
    visible = tuple(sorted(item.hypothesis_id for item in visible_hypotheses))
    missing = tuple(sorted(set(required) - set(visible)))
    classification = (
        "POLICY_CONTEXT_MISSING_GAP_HYPOTHESIS" if missing else "POLICY_CONTEXT_COMPLETE"
    )
    return PolicyContextAudit(required, visible, missing, classification)


def gap_contract_audit(diagnosis: Diagnosis) -> tuple[GapContractAudit, ...]:
    return tuple(
        GapContractAudit(
            gap_id=gap.gap_id,
            dimension=gap.dimension.value,
            hypothesis_ids=tuple(sorted(gap.hypothesis_ids)),
            discriminating_outcomes=tuple(
                sorted(item.kind.value for item in gap.discriminating_outcomes)
            ),
            authorized_queries=tuple(
                sorted((item.capability, item.target.canonical) for item in gap.authorized_queries)
            ),
            declared_support_path=any(
                item.kind is GapOutcomeKind.SUPPORTS for item in gap.discriminating_outcomes
            ),
            declared_contradiction_path=any(
                item.kind is GapOutcomeKind.CONTRADICTS for item in gap.discriminating_outcomes
            ),
            resolvable=(
                gap.resolvability is GapResolvability.RESOLVABLE and bool(gap.authorized_queries)
            ),
        )
        for gap in sorted(diagnosis.information_gaps, key=lambda item: item.gap_id)
    )


def parity_audit(full_case: Case, active_case: Case, active_refs: set[str]) -> ParityAudit:
    full_map = {semantic_finding_key(item): item for item in full_case.findings}
    active_map = {semantic_finding_key(item): item for item in active_case.findings}
    full_keys, active_keys = set(full_map), set(active_map)
    full_only = tuple(sorted(full_keys - active_keys))
    active_only = tuple(sorted(active_keys - full_keys))
    reasons: list[tuple[str, str]] = []
    for key in full_only:
        finding = full_map[key]
        if set(finding.evidence_ids) <= active_refs:
            reason = "RAW_RETURNED_NORMALIZATION_GAP"
        else:
            families = _NORMALIZER_FINDING_KINDS_BY_CAPABILITY
            exposed = any(finding.kind in values for values in families.values())
            reason = "RAW_RECORD_NOT_RETURNED" if exposed else "CAPABILITY_NOT_EXPOSED"
        reasons.append((key, reason))
    full_counts = Counter(item.kind.value for item in full_case.findings)
    active_counts = Counter(item.kind.value for item in active_case.findings)
    kind_counts = tuple(
        (kind, full_counts.get(kind, 0), active_counts.get(kind, 0))
        for kind in sorted(set(full_counts) | set(active_counts))
    )
    return ParityAudit(
        full_findings=tuple(sorted(full_keys)),
        active_findings=tuple(sorted(active_keys)),
        shared_findings=tuple(sorted(full_keys & active_keys)),
        full_only_findings=full_only,
        active_only_findings=active_only,
        full_only_reasons=tuple(sorted(reasons)),
        finding_kind_counts=kind_counts,
    )


def full_blockers(snapshot: DiagnosisEpistemicSnapshot) -> tuple[tuple[str, str, str], ...]:
    result: list[tuple[str, str, str]] = []
    for episode in snapshot.episodes:
        if episode.epistemic_state != "UNRESOLVED":
            continue
        reasons = set(episode.reason_codes)
        if "NO_ONSET_CAPABLE_INITIATING_EVIDENCE" in reasons:
            category = "MISSING_INITIATING_PROOF"
        elif "EXPLICIT_TEMPORAL_CONTRADICTION" in reasons:
            category = "NON_HARD_TEMPORAL_UNCERTAINTY"
        else:
            category = "OTHER_UNRESOLVED_EPISTEMIC_REASON"
        result.append((episode.actor, category, ",".join(episode.reason_codes)))
    return tuple(sorted(result))


def _fixed_point(
    source: ObservationSource,
    diagnosis: Diagnosis,
    attempted: Sequence[str],
) -> tuple[bool, int]:
    backend = investigation_backend(source)
    tools: Mapping[str, InvestigationTool] = default_tools(backend)
    choices = legal_query_choices(diagnosis, tools, query_templates(source), set(attempted))
    return not choices, len(choices)


def build_blind_audit(
    source: ObservationSource,
    *,
    max_rounds: int = 16,
    max_observations: int = 4096,
) -> UnresolvedReachabilityBlindAudit:
    full_case = build_case(source)
    full_diagnosis = diagnose_case(full_case)
    bounded_case = build_case(initial_view(source))
    seed_diagnosis = diagnose_case(bounded_case)
    from packages.rca.investigation.control_matrix import exhaustive_active_closure

    exhaustive = exhaustive_active_closure(
        source, max_rounds=max_rounds, max_observations=max_observations
    )
    fixed, legal_unseen = _fixed_point(
        source, exhaustive.diagnosis, exhaustive.attempted_observations
    )
    full_snapshot = diagnosis_snapshot(full_case, full_diagnosis)
    seed_snapshot = diagnosis_snapshot(bounded_case, seed_diagnosis)
    active_snapshot = diagnosis_snapshot(exhaustive.case, exhaustive.diagnosis)
    reachability = episode_reachability(seed_snapshot, active_snapshot, full_snapshot)
    active_refs = {ref for finding in bounded_case.findings for ref in finding.evidence_ids} | set(
        exhaustive.unique_returned_refs
    )
    return UnresolvedReachabilityBlindAudit(
        scenario_id=source.incident_id(),
        full=full_snapshot,
        seed=seed_snapshot,
        exhaustive_active=active_snapshot,
        episode_reachability=reachability,
        query_effects=query_effects(exhaustive.audits),
        policy_context_audit=policy_context_audit(exhaustive.diagnosis),
        gap_contract_audit=gap_contract_audit(exhaustive.diagnosis),
        parity_audit=parity_audit(full_case, exhaustive.case, active_refs),
        exhaustive_fixed_point=fixed and not exhaustive.truncated,
        fixed_point_reason=exhaustive.termination_reason,
        fixed_point_legal_unseen=legal_unseen,
        full_blockers=full_blockers(full_snapshot),
    )


def blind_audit_from_dict(value: Mapping[str, Any]) -> UnresolvedReachabilityBlindAudit:
    """Deserialize the deterministic blind artifact without source access."""

    def episode(item: Mapping[str, Any]) -> EpisodeStateSnapshot:
        return EpisodeStateSnapshot(
            actor=str(item["actor"]),
            hypothesis_id=str(item["hypothesis_id"]),
            members=tuple(item.get("members", ())),
            manifestations=tuple(item.get("manifestations", ())),
            epistemic_state=str(item["epistemic_state"]),
            reason_codes=tuple(item.get("reason_codes", ())),
            verification_decision=item.get("verification_decision"),
            finding_kinds=tuple(item.get("finding_kinds", ())),
            finding_keys=tuple(item.get("finding_keys", ())),
            initiating_finding_keys=tuple(item.get("initiating_finding_keys", ())),
            supporting_finding_keys=tuple(item.get("supporting_finding_keys", ())),
            contradictory_finding_keys=tuple(item.get("contradictory_finding_keys", ())),
            causal_path_shape=tuple(
                tuple(tuple(hop) for hop in path) for path in item.get("causal_path_shape", ())
            ),
        )

    def snapshot(item: Mapping[str, Any]) -> DiagnosisEpistemicSnapshot:
        return DiagnosisEpistemicSnapshot(
            resolution=str(item["resolution"]),
            supported_actors=tuple(item.get("supported_actors", ())),
            unresolved_actors=tuple(item.get("unresolved_actors", ())),
            contradicted_actors=tuple(item.get("contradicted_actors", ())),
            leading_actors=tuple(item.get("leading_actors", ())),
            episodes=tuple(episode(child) for child in item.get("episodes", ())),
        )

    def reach(item: Mapping[str, Any]) -> EpisodeReachability:
        return EpisodeReachability(
            actor=str(item["actor"]),
            seed_state=item.get("seed_state"),
            active_state=item.get("active_state"),
            full_state=item.get("full_state"),
            reachability_class=str(item["reachability_class"]),
        )

    def effect(item: Mapping[str, Any]) -> QueryEpistemicEffect:
        return QueryEpistemicEffect(
            **{
                **item,
                "returned_refs": tuple(item.get("returned_refs", ())),
                "new_refs": tuple(item.get("new_refs", ())),
                "normalized_findings": tuple(item.get("normalized_findings", ())),
                "new_findings": tuple(item.get("new_findings", ())),
                "episode_states_before": tuple(
                    tuple(x) for x in item.get("episode_states_before", ())
                ),
                "episode_states_after": tuple(
                    tuple(x) for x in item.get("episode_states_after", ())
                ),
                "transitions": tuple(item.get("transitions", ())),
            }
        )

    policy = value["policy_context_audit"]
    parity = value["parity_audit"]
    gaps = tuple(
        GapContractAudit(
            gap_id=str(item["gap_id"]),
            dimension=str(item["dimension"]),
            hypothesis_ids=tuple(item.get("hypothesis_ids", ())),
            discriminating_outcomes=tuple(item.get("discriminating_outcomes", ())),
            authorized_queries=tuple(tuple(x) for x in item.get("authorized_queries", ())),
            declared_support_path=bool(item["declared_support_path"]),
            declared_contradiction_path=bool(item["declared_contradiction_path"]),
            resolvable=bool(item["resolvable"]),
        )
        for item in value.get("gap_contract_audit", ())
    )
    return UnresolvedReachabilityBlindAudit(
        scenario_id=str(value["scenario_id"]),
        full=snapshot(value["full"]),
        seed=snapshot(value["seed"]),
        exhaustive_active=snapshot(value["exhaustive_active"]),
        episode_reachability=tuple(reach(item) for item in value.get("episode_reachability", ())),
        query_effects=tuple(effect(item) for item in value.get("query_effects", ())),
        policy_context_audit=PolicyContextAudit(
            tuple(policy.get("gap_hypothesis_ids", ())),
            tuple(policy.get("policy_visible_hypothesis_ids", ())),
            tuple(policy.get("missing_hypothesis_ids", ())),
            str(policy["classification"]),
        ),
        gap_contract_audit=gaps,
        parity_audit=ParityAudit(
            tuple(parity.get("full_findings", ())),
            tuple(parity.get("active_findings", ())),
            tuple(parity.get("shared_findings", ())),
            tuple(parity.get("full_only_findings", ())),
            tuple(parity.get("active_only_findings", ())),
            tuple(tuple(x) for x in parity.get("full_only_reasons", ())),
            tuple(tuple(x) for x in parity.get("finding_kind_counts", ())),
        ),
        exhaustive_fixed_point=bool(value["exhaustive_fixed_point"]),
        fixed_point_reason=str(value["fixed_point_reason"]),
        fixed_point_legal_unseen=int(value["fixed_point_legal_unseen"]),
        full_blockers=tuple(tuple(x) for x in value.get("full_blockers", ())),
    )


__all__ = [
    "DiagnosisEpistemicSnapshot",
    "EpisodeProjection",
    "EpisodeReachability",
    "EpisodeStateSnapshot",
    "GapContractAudit",
    "ParityAudit",
    "PolicyContextAudit",
    "QueryEpistemicEffect",
    "UnresolvedReachabilityBlindAudit",
    "blind_audit_from_dict",
    "build_blind_audit",
    "diagnosis_snapshot",
    "forbidden_blind_keys",
    "gap_contract_audit",
    "policy_context_audit",
    "query_effects",
]

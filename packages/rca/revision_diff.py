"""Pure, deterministic comparisons of two persisted diagnosis revisions."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from packages.rca.hypotheses import matchable_by_key
from packages.rca.model import Diagnosis, Hypothesis, ResolutionElimination


class ManifestDiffStatus(StrEnum):
    """What two persisted manifest digests establish."""

    UNCHANGED = "UNCHANGED"
    CHANGED = "CHANGED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class PersistedDiagnosisRevision:
    """The persisted diagnosis document and revision metadata used by a diff."""

    document: Mapping[str, Any]
    manifest_digest: str | None


@dataclass(frozen=True)
class ResolutionTransition:
    previous: str
    current: str

    @property
    def changed(self) -> bool:
        return self.previous != self.current


@dataclass(frozen=True)
class ManifestDiff:
    previous_digest: str | None
    current_digest: str | None
    status: ManifestDiffStatus


@dataclass(frozen=True)
class HypothesisSnapshot:
    """Revision-local description retained for unmatched/change references."""

    hypothesis_key: str | None
    hypothesis_id: str
    causal_actor: str | None
    state: str | None
    root_eligible: bool


@dataclass(frozen=True)
class HypothesisChange:
    hypothesis_key: str
    previous: HypothesisSnapshot
    current: HypothesisSnapshot


@dataclass(frozen=True)
class NewElimination:
    """Canonical semantic projection of a current persisted elimination."""

    hypothesis_key: str | None
    hypothesis_id: str
    code: str
    rule_id: str
    rule_version: str
    consequence: str | None
    targets: tuple[str, ...]
    mechanism: str
    evidence_ids: tuple[str, ...]
    observation_ids: tuple[str, ...]
    time_basis: tuple[str, ...]
    coverage_basis: str
    preconditions: tuple[str, ...]
    decisive_evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class RevisionDiff:
    resolution_transition: ResolutionTransition
    hypothesis_changes: tuple[HypothesisChange, ...]
    new_eliminations: tuple[NewElimination, ...]
    new_decisive_evidence_ids: tuple[str, ...]
    appeared: tuple[HypothesisSnapshot, ...]
    disappeared: tuple[HypothesisSnapshot, ...]
    manifest_diff: ManifestDiff


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _revision_diagnosis(revision: PersistedDiagnosisRevision) -> Diagnosis:
    if not isinstance(revision.document, Mapping):
        raise TypeError("persisted diagnosis document must be a mapping")
    return Diagnosis.model_validate(dict(revision.document))


@dataclass(frozen=True)
class _Hypothesis:
    hypothesis_id: str
    hypothesis_key: str | None
    causal_actor: str | None
    state: str | None
    root_eligible: bool


def _project_hypotheses(diagnosis: Diagnosis) -> tuple[_Hypothesis, ...]:
    # Build the same union as epistemic_state while retaining no explanatory
    # or score fields. hypothesis_id is only a within-document join key here.
    retained: dict[str, Hypothesis] = {}
    for hypothesis in (
        *((diagnosis.hypothesis,) if diagnosis.hypothesis is not None else ()),
        *diagnosis.alternative_hypotheses,
        *diagnosis.ambiguous_hypotheses,
    ):
        old = retained.get(hypothesis.hypothesis_id)
        if old is not None and old != hypothesis:
            raise ValueError(f"conflicting hypothesis records for id {hypothesis.hypothesis_id!r}")
        retained[hypothesis.hypothesis_id] = hypothesis

    trace = diagnosis.resolution_trace
    states: dict[str, str] = {}
    if trace is not None:
        for audit in trace.hypothesis_audits:
            state = audit.epistemic_state.value
            previous_state = states.get(audit.hypothesis_id)
            if previous_state is not None and previous_state != state:
                raise ValueError(f"conflicting hypothesis states for id {audit.hypothesis_id!r}")
            states[audit.hypothesis_id] = state
    ineligible = {
        item.hypothesis_id
        for item in (trace.eliminations if trace is not None else ())
        if item.consequence is not None and item.consequence.value == "ROOT_INELIGIBILITY"
    }
    return tuple(
        _Hypothesis(
            hypothesis_id=identifier,
            hypothesis_key=(retained[identifier].hypothesis_key or None)
            if identifier in retained
            else None,
            causal_actor=retained[identifier].causal_actor.canonical
            if identifier in retained
            else None,
            state=states.get(identifier),
            root_eligible=identifier not in ineligible,
        )
        for identifier in sorted(set(retained) | set(states))
    )


def _snapshot(hypothesis: _Hypothesis) -> HypothesisSnapshot:
    return HypothesisSnapshot(
        hypothesis_key=hypothesis.hypothesis_key,
        hypothesis_id=hypothesis.hypothesis_id,
        causal_actor=hypothesis.causal_actor,
        state=hypothesis.state,
        root_eligible=hypothesis.root_eligible,
    )


def _sorted_strings(items: tuple[str, ...]) -> tuple[str, ...]:
    # Canonicalize order without discarding any persisted multiplicity.
    return tuple(sorted(items))


def _elimination_identity(item: ResolutionElimination) -> tuple[str, ...]:
    """Semantic identity, excluding revision-local ID, detail, and decisive IDs.

    The rule, reason, consequence, target/mechanism, ordinary evidence,
    observations, temporal/coverage basis and preconditions jointly identify
    an elimination. decisive_evidence_ids are excluded so their additions can
    be reported separately; hypothesis_id is local to one revision.
    """
    time_basis = sorted(_canonical(value.model_dump(mode="json")) for value in item.time_basis)
    preconditions = sorted(
        _canonical(value.model_dump(mode="json")) for value in item.preconditions
    )
    return (
        item.code.value,
        item.rule_id,
        item.rule_version,
        item.consequence.value if item.consequence is not None else "",
        _canonical(sorted(item.targets)),
        item.mechanism,
        _canonical(sorted(item.evidence_ids)),
        _canonical(sorted(item.observation_ids)),
        _canonical(time_basis),
        item.coverage_basis,
        _canonical(preconditions),
    )


def _elimination_output(item: ResolutionElimination, key: str | None) -> NewElimination:
    return NewElimination(
        hypothesis_key=key,
        hypothesis_id=item.hypothesis_id,
        code=item.code.value,
        rule_id=item.rule_id,
        rule_version=item.rule_version,
        consequence=item.consequence.value if item.consequence is not None else None,
        targets=_sorted_strings(item.targets),
        mechanism=item.mechanism,
        evidence_ids=_sorted_strings(item.evidence_ids),
        observation_ids=_sorted_strings(item.observation_ids),
        time_basis=tuple(
            sorted(_canonical(value.model_dump(mode="json")) for value in item.time_basis)
        ),
        coverage_basis=item.coverage_basis,
        preconditions=tuple(
            sorted(_canonical(value.model_dump(mode="json")) for value in item.preconditions)
        ),
        decisive_evidence_ids=_sorted_strings(item.decisive_evidence_ids),
    )


def _manifest_diff(previous: str | None, current: str | None) -> ManifestDiff:
    if previous is None or current is None:
        status = ManifestDiffStatus.UNKNOWN
    elif previous == current:
        status = ManifestDiffStatus.UNCHANGED
    else:
        status = ManifestDiffStatus.CHANGED
    return ManifestDiff(previous, current, status)


def diff_revisions(
    previous: PersistedDiagnosisRevision,
    current: PersistedDiagnosisRevision,
) -> RevisionDiff:
    """Describe factual changes between two persisted revision inputs.

    This function has no storage, live-system, clock, or environment access.
    Cross-revision hypothesis correspondence is exclusively by a unique,
    non-empty hypothesis_key in both documents.
    """
    before = _revision_diagnosis(previous)
    after = _revision_diagnosis(current)
    before_hypotheses = _project_hypotheses(before)
    after_hypotheses = _project_hypotheses(after)

    # Adapt the exact model instances through the established M19-1.9 helper:
    # duplicate and empty keys remain unmatchable on either side.
    before_models = tuple(
        {
            item.hypothesis_id: item
            for item in (
                *((before.hypothesis,) if before.hypothesis is not None else ()),
                *before.alternative_hypotheses,
                *before.ambiguous_hypotheses,
            )
        }.values()
    )
    after_models = tuple(
        {
            item.hypothesis_id: item
            for item in (
                *((after.hypothesis,) if after.hypothesis is not None else ()),
                *after.alternative_hypotheses,
                *after.ambiguous_hypotheses,
            )
        }.values()
    )
    before_matchable = matchable_by_key(before_models)
    after_matchable = matchable_by_key(after_models)
    before_by_id = {item.hypothesis_id: item for item in before_hypotheses}
    after_by_id = {item.hypothesis_id: item for item in after_hypotheses}

    matched_keys = sorted(before_matchable.keys() & after_matchable.keys())
    changes: list[HypothesisChange] = []
    for key in matched_keys:
        left = before_by_id[before_matchable[key].hypothesis_id]
        right = after_by_id[after_matchable[key].hypothesis_id]
        left_snapshot, right_snapshot = _snapshot(left), _snapshot(right)
        if (
            left.causal_actor != right.causal_actor
            or left.state != right.state
            or left.root_eligible != right.root_eligible
        ):
            changes.append(HypothesisChange(key, left_snapshot, right_snapshot))

    matched_before_ids = {before_matchable[key].hypothesis_id for key in matched_keys}
    matched_after_ids = {after_matchable[key].hypothesis_id for key in matched_keys}
    appeared = tuple(
        sorted(
            (
                _snapshot(item)
                for item in after_hypotheses
                if item.hypothesis_id not in matched_after_ids
            ),
            key=lambda item: (
                item.hypothesis_key or "",
                item.hypothesis_id,
                item.causal_actor or "",
                item.state or "",
            ),
        )
    )
    disappeared = tuple(
        sorted(
            (
                _snapshot(item)
                for item in before_hypotheses
                if item.hypothesis_id not in matched_before_ids
            ),
            key=lambda item: (
                item.hypothesis_key or "",
                item.hypothesis_id,
                item.causal_actor or "",
                item.state or "",
            ),
        )
    )

    before_trace = before.resolution_trace
    after_trace = after.resolution_trace
    before_eliminations = before_trace.eliminations if before_trace is not None else ()
    after_eliminations = after_trace.eliminations if after_trace is not None else ()
    before_key_by_id = {item.hypothesis_id: item.hypothesis_key for item in before_hypotheses}
    after_key_by_id = {item.hypothesis_id: item.hypothesis_key for item in after_hypotheses}
    previous_buckets: dict[tuple[str, tuple[str, ...]], list[ResolutionElimination]] = defaultdict(
        list
    )
    for item in before_eliminations:
        elimination_key = before_key_by_id.get(item.hypothesis_id)
        if (
            elimination_key is not None
            and elimination_key in before_matchable
            and elimination_key in after_matchable
        ):
            previous_buckets[(elimination_key, _elimination_identity(item))].append(item)
    for bucket in previous_buckets.values():
        bucket.sort(key=lambda item: tuple(sorted(item.decisive_evidence_ids)))

    seen_current: Counter[tuple[str, tuple[str, ...]]] = Counter()
    new_eliminations: list[NewElimination] = []
    new_decisive: set[str] = set()
    for item in sorted(
        after_eliminations,
        key=lambda value: (
            after_key_by_id.get(value.hypothesis_id) or "",
            value.hypothesis_id,
            _elimination_identity(value),
            tuple(sorted(value.decisive_evidence_ids)),
        ),
    ):
        elimination_key = after_key_by_id.get(item.hypothesis_id)
        safe_parent = (
            elimination_key is not None
            and elimination_key in before_matchable
            and elimination_key in after_matchable
        )
        identity = _elimination_identity(item)
        bucket_key = (
            (elimination_key, identity) if safe_parent and elimination_key is not None else None
        )
        prior_items = previous_buckets.get(bucket_key, []) if bucket_key is not None else []
        occurrence = seen_current[bucket_key] if bucket_key is not None else 0
        if bucket_key is not None:
            seen_current[bucket_key] += 1
        if bucket_key is None or occurrence >= len(prior_items):
            new_eliminations.append(_elimination_output(item, elimination_key))
        old_ids = {
            evidence_id
            for previous_item in prior_items
            for evidence_id in previous_item.decisive_evidence_ids
        }
        new_decisive.update(set(item.decisive_evidence_ids) - old_ids)

    new_eliminations.sort(
        key=lambda item: (
            item.hypothesis_key or "",
            item.hypothesis_id,
            item.code,
            item.rule_id,
            item.rule_version,
            item.consequence or "",
            item.targets,
            item.mechanism,
            item.evidence_ids,
            item.observation_ids,
            item.time_basis,
            item.coverage_basis,
            item.preconditions,
            item.decisive_evidence_ids,
        )
    )
    changes.sort(key=lambda item: item.hypothesis_key)
    return RevisionDiff(
        resolution_transition=ResolutionTransition(before.resolution.value, after.resolution.value),
        hypothesis_changes=tuple(changes),
        new_eliminations=tuple(new_eliminations),
        new_decisive_evidence_ids=tuple(sorted(new_decisive)),
        appeared=appeared,
        disappeared=disappeared,
        manifest_diff=_manifest_diff(previous.manifest_digest, current.manifest_digest),
    )


__all__ = [
    "HypothesisChange",
    "HypothesisSnapshot",
    "ManifestDiff",
    "ManifestDiffStatus",
    "NewElimination",
    "PersistedDiagnosisRevision",
    "ResolutionTransition",
    "RevisionDiff",
    "diff_revisions",
]

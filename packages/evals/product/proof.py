"""Transition proofs and safety counters of the product-resolution harness (M19-7.P1).

Pure functions over the product's persisted decisions. They verify what R1,
R_early and R2 recorded — resolution, hypotheses, eliminations, decisive
evidence, leader — and the provenance sets ``U(Rn)``; they never rerun RCA
logic, never produce a hypothesis and never compute a ``hypothesis_key``.
Inputs are collected read-only before teardown (``proof_inputs``); this module
knows no database, HTTP or Kubernetes.

``H_x`` is selected from the persisted R1 hypotheses with the scenario's
``TargetHypothesisRef``; exactly one match is required, and its persisted
``hypothesis_key`` is then followed through R_early and R2. Anything that
cannot be established makes the proof FAIL. A safety counter that cannot be
measured exactly is ``None`` (never a made-up zero).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from packages.evals.product.actions import ActionReceipt, DeletionReceipt, ReadinessReceipt
from packages.evals.product.spec import (
    Expectation,
    ProofId,
    RevisionCheckpoint,
    TargetHypothesisRef,
    TimelineRef,
)
from packages.rca.model import (
    Diagnosis,
    EliminationConsequence,
    Hypothesis,
    HypothesisEpistemicState,
    PreconditionStatus,
    Resolution,
    ResolutionElimination,
)

PASS, FAIL = "PASS", "FAIL"
# Evidence served from provider reads: it must be on the run's committed tape.
TAPE_PREFIXES = ("prometheus:", "loki:", "tempo:")


@dataclass(frozen=True, slots=True)
class RevisionFacts:
    """One persisted revision and its provenance, as collected before teardown."""

    number: int
    trigger: str
    diagnosis: Diagnosis
    universe: frozenset[str]  # U(Rn): base evidence records + committed tape evidence
    tape_ids: frozenset[str]  # committed tape evidence of Rn's run
    evidence_uids: Mapping[str, str] = field(default_factory=dict)  # id -> exact instance UID
    synthetic_ids: frozenset[str] = frozenset()  # evidence from synthetic sources
    persisted_digest: str | None = None
    replay_digest: str | None = None  # None: replay did not produce a digest
    # NOT_ATTEMPTED | UNSUPPORTED | PASS | DIVERGED | ERROR (M20.1b); None: derived
    # from the digests (a missing digest is ERROR, never a silent divergence).
    replay_status: str | None = None

    @property
    def effective_replay_status(self) -> str:
        if self.replay_status is not None:
            return self.replay_status
        if self.replay_digest is None:
            return "ERROR"
        return "PASS" if self.replay_digest == self.persisted_digest else "DIVERGED"


@dataclass(frozen=True, slots=True)
class ProofInput:
    expectation: Expectation
    receipts: Mapping[TimelineRef, ActionReceipt]
    revisions: tuple[RevisionFacts, ...]  # R1, R_early, R2


@dataclass(frozen=True, slots=True)
class Selection:
    hypothesis_key: str | None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class Transition:
    hypothesis_key: str
    rule_id: str
    rule_version: str
    decisive_evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Evaluation:
    proof: dict[ProofId, str]
    negatives: dict[ProofId, str]
    safety: dict[str, int | None]
    transition: Transition | None
    reasons: dict[str, str]  # proof id or counter -> why it failed / is unmeasured


SAFETY_COUNTERS = (
    "false_resolved",
    "wrong_actor",
    "uid_misbinding",
    "missing_to_contradiction",
    "fabricated",
    "replay_divergence",
    "unresolved_tape_evidence_id",
    "synthetic_cluster_evidence",
)


# --- persisted views ----------------------------------------------------------------------


def persisted_hypotheses(diagnosis: Diagnosis) -> dict[str, Hypothesis]:
    """Every hypothesis the revision persisted with its findings, by id."""
    found: dict[str, Hypothesis] = {}
    for item in (
        *((diagnosis.hypothesis,) if diagnosis.hypothesis is not None else ()),
        *diagnosis.alternative_hypotheses,
        *diagnosis.ambiguous_hypotheses,
    ):
        found.setdefault(item.hypothesis_id, item)
    return found


def _receipt_uid(receipt: ActionReceipt | None) -> str | None:
    if isinstance(receipt, ReadinessReceipt):
        return receipt.uid_before if receipt.uid_before == receipt.uid_after else None
    if isinstance(receipt, DeletionReceipt):
        return receipt.old.uid
    return None


def select_target(
    ref: TargetHypothesisRef | None,
    r1: Diagnosis,
    receipts: Mapping[TimelineRef, ActionReceipt],
) -> Selection:
    """The persisted ``hypothesis_key`` of the unique R1 hypothesis ``ref`` names.

    The match runs over R1's FULL persisted inventory (every hypothesis's actor,
    mechanism class and instance UIDs), never over the persisted subset.
    """
    if ref is None:
        return Selection(None, "no target hypothesis reference")
    inventory = r1.hypothesis_inventory
    if not inventory or any(
        item.causal_actor is None or item.mechanism_class is None or item.instance_uids is None
        for item in inventory
    ):
        return Selection(None, "R1's inventory lacks identity metadata (legacy run)")
    if ref.source == "action_receipt":
        assert ref.action is not None
        uid = _receipt_uid(receipts.get(ref.action))
        if uid is None:
            return Selection(None, f"no verified receipt UID for {ref.action}")
        matches = [
            item
            for item in inventory
            if item.causal_actor is not None
            and item.causal_actor.kind == "Pod"
            and set(item.instance_uids or ()) == {uid}
            and frozenset(item.mechanism_class or ()) == ref.mechanism
        ]
    else:
        matches = [
            item
            for item in inventory
            if item.causal_actor is not None
            and item.causal_actor.canonical == ref.actor
            and frozenset(item.mechanism_class or ()) == ref.mechanism
        ]
    if len(matches) != 1:
        return Selection(None, f"{len(matches)} R1 inventory hypotheses match the reference")
    key = matches[0].hypothesis_key
    if not key:
        return Selection(None, "the matching hypothesis has no hypothesis_key")
    return Selection(key)


def hypothesis_id_for(diagnosis: Diagnosis, key: str) -> str | None:
    """The revision-local id of the hypothesis carrying ``key``, if exactly one does."""
    ids = [
        item.hypothesis_id for item in diagnosis.hypothesis_inventory if item.hypothesis_key == key
    ]
    return ids[0] if len(ids) == 1 else None


def _eliminations(diagnosis: Diagnosis) -> tuple[ResolutionElimination, ...]:
    trace = diagnosis.resolution_trace
    return tuple(trace.eliminations) if trace is not None else ()


def _target_elimination(
    diagnosis: Diagnosis, hypothesis_id: str | None, expectation: Expectation
) -> ResolutionElimination | None:
    if hypothesis_id is None or expectation.target_rule is None:
        return None
    rule_id, rule_version = expectation.target_rule
    matches = [
        item
        for item in _eliminations(diagnosis)
        if item.hypothesis_id == hypothesis_id
        and (item.rule_id, item.rule_version) == (rule_id, rule_version)
    ]
    return matches[0] if len(matches) == 1 else None


def _uses_rule(diagnosis: Diagnosis, rule: tuple[str, str] | None) -> bool:
    return rule is not None and any(
        (item.rule_id, item.rule_version) == rule for item in _eliminations(diagnosis)
    )


def _plausible(diagnosis: Diagnosis) -> set[str]:
    """Not CONTRADICTED and root-eligible, from the persisted audits and eliminations."""
    trace = diagnosis.resolution_trace
    if trace is None:
        return set()
    ineligible = {
        item.hypothesis_id
        for item in trace.eliminations
        if item.consequence is EliminationConsequence.ROOT_INELIGIBILITY
    }
    return {
        audit.hypothesis_id
        for audit in trace.hypothesis_audits
        if audit.plausible
        and audit.epistemic_state is not HypothesisEpistemicState.CONTRADICTED
        and audit.hypothesis_id not in ineligible
    }


# --- positive proofs ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Context:
    facts: ProofInput
    key: str | None
    selection_reason: str

    @property
    def r1(self) -> RevisionFacts:
        return self.facts.revisions[0]

    @property
    def r_early(self) -> RevisionFacts:
        return self.facts.revisions[1]

    @property
    def r2(self) -> RevisionFacts:
        return self.facts.revisions[2]

    def hid(self, revision: RevisionFacts) -> str | None:
        return hypothesis_id_for(revision.diagnosis, self.key) if self.key else None

    def target(self, revision: RevisionFacts) -> ResolutionElimination | None:
        return _target_elimination(revision.diagnosis, self.hid(revision), self.facts.expectation)


def _t1(ctx: _Context) -> str:
    r1 = ctx.r1.diagnosis
    if r1.resolution is not Resolution.AMBIGUOUS:
        return f"R1 is {r1.resolution.value}, not AMBIGUOUS"
    plausible = _plausible(r1)
    if len(plausible) < 2:
        return f"R1 has {len(plausible)} plausible hypotheses"
    hid = ctx.hid(ctx.r1)
    if hid is None or hid not in plausible:
        return "H_x is not a plausible R1 hypothesis"
    return ""


def _decisive(ctx: _Context) -> tuple[str, ...]:
    target = ctx.target(ctx.r2)
    return target.decisive_evidence_ids if target is not None else ()


def _t2(ctx: _Context) -> str:
    decisive = set(_decisive(ctx))
    if not decisive:
        return "D is empty (no R2 target elimination with decisive evidence)"
    if decisive & ctx.r1.universe:
        return f"D ∩ U(R1) = {sorted(decisive & ctx.r1.universe)}"
    if not decisive <= ctx.r2.universe:
        return f"D ⊄ U(R2): {sorted(decisive - ctx.r2.universe)}"
    return ""


def _instance_targets(elimination: ResolutionElimination) -> set[str]:
    return {item.target for item in elimination.time_basis if item.target}


def _t3(ctx: _Context) -> str:
    expectation = ctx.facts.expectation
    ref = expectation.target_hypothesis_ref
    target = ctx.target(ctx.r2)
    if target is None or ref is None:
        return "no unique R2 elimination of H_x by the expected rule"
    if target.consequence is None or target.consequence.value != expectation.target_consequence:
        return f"consequence {target.consequence} is not {expectation.target_consequence}"
    if not target.preconditions or not all(item.passed for item in target.preconditions):
        return "not every precondition passed"
    entry = next(
        (
            i
            for i in ctx.r2.diagnosis.hypothesis_inventory
            if i.hypothesis_id == target.hypothesis_id
        ),
        None,
    )
    actor = entry.causal_actor.canonical if entry and entry.causal_actor else None
    if actor is None or not target.targets or target.targets[0] != actor:
        return f"targets {target.targets} do not start with H_x's actor {actor}"
    if ref.source == "action_receipt":
        assert ref.action is not None
        uid = _receipt_uid(ctx.facts.receipts.get(ref.action))
        if _instance_targets(target) != {f"{actor}@{uid}"}:
            return f"instance binding {_instance_targets(target)} is not {actor}@{uid}"
        decisive_uids = {ctx.r2.evidence_uids.get(item) for item in target.decisive_evidence_ids}
        if decisive_uids != {uid}:
            return f"decisive evidence binds UIDs {decisive_uids}, not {uid}"
        if ref.episode_basis is not None and {item.certainty for item in target.time_basis} != {
            ref.episode_basis
        }:
            return f"episode basis is not {ref.episode_basis}"
    elif not target.decisive_evidence_ids or not set(target.decisive_evidence_ids) <= (
        ctx.r2.tape_ids | ctx.r2.universe
    ):
        return "resource binding: decisive coverage is empty or outside U(R2)"
    return ""


def _t4(ctx: _Context) -> str:
    r2 = ctx.r2.diagnosis
    if r2.resolution is not Resolution.RESOLVED:
        return f"R2 is {r2.resolution.value}"
    root = ctx.facts.expectation.root_actor
    leader = r2.root_cause.canonical if r2.root_cause is not None else None
    if root is None or leader != root:
        return f"leader {leader} is not the protocol root actor {root}"
    return ""


# --- negative proofs ------------------------------------------------------------------------


def _n0(ctx: _Context) -> str:
    early = ctx.r_early.diagnosis
    if ctx.key is None:
        return "H_x not selected"
    if ctx.hid(ctx.r_early) is None:
        return "H_x not found in R_early"
    if ctx.target(ctx.r_early) is not None:
        return "R_early already eliminates H_x by the target rule"
    decisive = set(_decisive(ctx))
    if not decisive:
        return "D is empty"
    if decisive & ctx.r_early.universe:
        return f"D ∩ U(R_early) = {sorted(decisive & ctx.r_early.universe)}"
    if early.resolution is Resolution.RESOLVED and _uses_rule(
        early, ctx.facts.expectation.target_rule
    ):
        return "R_early is RESOLVED with the target rule"
    return ""


def _n1(ctx: _Context) -> str:
    """H_B is not root-ineligible by RECOVERED; its continuity precondition is DISQUALIFIED."""
    hid = ctx.hid(ctx.r2)
    r2 = ctx.r2.diagnosis
    if hid is None or r2.resolution_trace is None:
        return "H_x not found in R2"
    rule = ctx.facts.expectation.target_rule
    for item in _eliminations(r2):
        if item.hypothesis_id == hid and (item.rule_id, item.rule_version) == rule:
            if any(basis.certainty == "RECOVERED" for basis in item.time_basis):
                return "H_x is root-ineligible by RECOVERED"
    audits = [a for a in r2.resolution_trace.hypothesis_audits if a.hypothesis_id == hid]
    statuses = {
        audit.result.status
        for entry in audits
        for audit in entry.precondition_audit
        if rule is not None and audit.rule_id == rule[0] and audit.result is not None
    }
    if PreconditionStatus.DISQUALIFIED not in statuses:
        return f"continuity precondition statuses {sorted(s.value for s in statuses)}"
    return ""


def _not_contradicted_by_rule(ctx: _Context) -> str:
    """N2/N3: H_x is not contradicted by the target (A2) rule in any revision."""
    if ctx.key is None:
        return "H_x not selected"
    for revision in ctx.facts.revisions:
        hid = ctx.hid(revision)
        if hid is None:
            return f"H_x not found in revision {revision.number}"
        target = _target_elimination(revision.diagnosis, hid, ctx.facts.expectation)
        if target is not None and target.consequence is EliminationConsequence.CONTRADICTION:
            return f"revision {revision.number} contradicts H_x by the target rule"
    return ""


_POSITIVE = {ProofId.T1: _t1, ProofId.T2: _t2, ProofId.T3: _t3, ProofId.T4: _t4}
_NEGATIVE = {
    ProofId.N0: _n0,
    ProofId.N1: _n1,
    ProofId.N2: _not_contradicted_by_rule,
    ProofId.N3: _not_contradicted_by_rule,
}


# --- safety -----------------------------------------------------------------------------------


def _referenced(diagnosis: Diagnosis) -> set[str]:
    """Every evidence id the revision's persisted decision cites."""
    ids: set[str] = set()
    for hypothesis in persisted_hypotheses(diagnosis).values():
        for finding in hypothesis.findings:
            ids.update(finding.evidence_ids)
    for finding in diagnosis.evidence:
        ids.update(finding.evidence_ids)
    for item in _eliminations(diagnosis):
        ids.update(item.evidence_ids)
        ids.update(item.decisive_evidence_ids)
        for basis in item.time_basis:
            ids.update(basis.evidence_ids)
    return ids


CHECKPOINTS = (RevisionCheckpoint.R1, RevisionCheckpoint.R_EARLY, RevisionCheckpoint.R2)


def resolved_state_is_internally_consistent(diagnosis: Diagnosis) -> bool:
    """Diagnostic only (not false_resolved): a RESOLVED revision has one leader that is
    not eliminated, is plausible, and is the named root cause."""
    trace = diagnosis.resolution_trace
    if diagnosis.resolution is not Resolution.RESOLVED:
        return True
    if trace is None or len(trace.leading_hypothesis_ids) != 1:
        return False
    (leader,) = trace.leading_hypothesis_ids
    if any(item.hypothesis_id == leader for item in trace.eliminations):
        return False
    if leader not in _plausible(diagnosis):
        return False
    entry = next((i for i in diagnosis.hypothesis_inventory if i.hypothesis_id == leader), None)
    return entry is not None and diagnosis.root_cause == entry.causal_actor


def _false_resolved(facts: ProofInput) -> int | None:
    """RESOLVED revisions at checkpoints the expectation does not allow RESOLVED."""
    allowed = facts.expectation.resolved_allowed
    if allowed is None:
        return None
    return sum(
        revision.diagnosis.resolution is Resolution.RESOLVED and checkpoint not in allowed
        for checkpoint, revision in zip(CHECKPOINTS, facts.revisions, strict=True)
    )


def _uid_misbinding(revision: RevisionFacts) -> int | None:
    """Instance-bound eliminations whose decisive/basis evidence names another UID."""
    count = 0
    for item in _eliminations(revision.diagnosis):
        bound = {basis.target.rsplit("@", 1)[1] for basis in item.time_basis if "@" in basis.target}
        if not bound:
            continue
        cited = {*item.decisive_evidence_ids, *(i for b in item.time_basis for i in b.evidence_ids)}
        uids = [revision.evidence_uids.get(evidence) for evidence in cited]
        if any(uid is None for uid in uids):
            return None  # an instance binding we cannot resolve exactly
        count += sum(uid not in bound for uid in uids)
    return count


def _missing_to_contradiction(revision: RevisionFacts) -> int:
    return sum(
        1
        for item in _eliminations(revision.diagnosis)
        if item.consequence is EliminationConsequence.CONTRADICTION
        and (
            not (item.evidence_ids or item.decisive_evidence_ids)
            or not all(precondition.passed for precondition in item.preconditions)
        )
    )


def _sum(values: Iterable[int | None]) -> int | None:
    total = 0
    for value in values:
        if value is None:
            return None
        total += value
    return total


REPLAY_STATUSES = frozenset({"NOT_ATTEMPTED", "UNSUPPORTED", "PASS", "DIVERGED", "ERROR"})


def _replay_divergence(revisions: Sequence[RevisionFacts], reasons: dict[str, str]) -> int | None:
    """Revisions whose replay actually DIVERGED; unmeasured (None) unless every one replayed."""
    statuses = [revision.effective_replay_status for revision in revisions]
    unknown = sorted({item for item in statuses if item not in REPLAY_STATUSES})
    if unknown:
        raise ValueError(f"unknown replay status(es) {unknown}")
    if any(status != "PASS" for status in statuses):
        reasons["replay_status"] = ", ".join(
            f"{revision.number}:{status}"
            for revision, status in zip(revisions, statuses, strict=True)
        )
    if any(status not in ("PASS", "DIVERGED") for status in statuses):
        reasons["replay_divergence"] = "not every revision was replayed"
        return None
    return sum(status == "DIVERGED" for status in statuses)


def safety_counters(facts: ProofInput) -> tuple[dict[str, int | None], dict[str, str]]:
    revisions = facts.revisions
    reasons: dict[str, str] = {}
    if facts.expectation.resolved_allowed is None:
        reasons["false_resolved"] = "no checkpoint resolution policy in the expectation"
    inconsistent = [
        checkpoint.value
        for checkpoint, revision in zip(CHECKPOINTS, revisions, strict=True)
        if not resolved_state_is_internally_consistent(revision.diagnosis)
    ]
    if inconsistent:
        reasons["resolved_consistency"] = (
            f"RESOLVED state not internally consistent: {inconsistent}"
        )
    root = facts.expectation.root_actor
    resolved = [r for r in revisions if r.diagnosis.resolution is Resolution.RESOLVED]
    if resolved and root is None:
        wrong: int | None = None
        reasons["wrong_actor"] = "RESOLVED revisions but no protocol root actor"
    else:
        wrong = sum(
            (r.diagnosis.root_cause.canonical if r.diagnosis.root_cause else None) != root
            for r in resolved
        )
    uid = _sum(_uid_misbinding(r) for r in revisions)
    if uid is None:
        reasons["uid_misbinding"] = "an instance-bound citation has no resolvable UID"
    counters: dict[str, int | None] = {
        "false_resolved": _false_resolved(facts),
        "wrong_actor": wrong,
        "uid_misbinding": uid,
        "missing_to_contradiction": sum(_missing_to_contradiction(r) for r in revisions),
        "fabricated": sum(len(_referenced(r.diagnosis) - r.universe) for r in revisions),
        "replay_divergence": _replay_divergence(revisions, reasons),
        "unresolved_tape_evidence_id": sum(
            len(
                {
                    item
                    for item in _referenced(r.diagnosis)
                    if item.startswith(TAPE_PREFIXES) and item not in r.tape_ids
                }
            )
            for r in revisions
        ),
        "synthetic_cluster_evidence": sum(
            len(_referenced(r.diagnosis) & r.synthetic_ids) for r in revisions
        ),
    }
    return counters, reasons


# --- evaluation ---------------------------------------------------------------------------------


def evaluate(facts: ProofInput) -> Evaluation:
    """Applicable proofs (the scenario's own ids) and every safety counter."""
    if len(facts.revisions) != 3:
        raise ValueError("proofs need R1, R_early and R2")
    expectation = facts.expectation
    selection = select_target(
        expectation.target_hypothesis_ref, facts.revisions[0].diagnosis, facts.receipts
    )
    ctx = _Context(facts, selection.hypothesis_key, selection.reason)
    reasons: dict[str, str] = {}

    def run(proof: ProofId, check: Mapping[ProofId, object]) -> str:
        if ctx.key is None:
            reasons[proof.value] = f"H_x not selected: {selection.reason}"
            return FAIL
        why = check[proof](ctx)  # type: ignore[operator]
        if why:
            reasons[proof.value] = why
        return FAIL if why else PASS

    proof = {p: run(p, _POSITIVE) for p in expectation.proofs if p in _POSITIVE}
    negatives = {p: run(p, _NEGATIVE) for p in expectation.proofs if p in _NEGATIVE}
    safety, safety_reasons = safety_counters(facts)
    reasons.update(safety_reasons)
    target = ctx.target(ctx.r2) if ctx.key else None
    transition = (
        Transition(ctx.key, target.rule_id, target.rule_version, target.decisive_evidence_ids)
        if target is not None and ctx.key is not None
        else None
    )
    return Evaluation(proof, negatives, safety, transition, reasons)


def receipts_of(timeline: Sequence[object]) -> dict[TimelineRef, ActionReceipt]:
    """Verified receipts by timeline reference, from the runner's internal records."""
    found: dict[TimelineRef, ActionReceipt] = {}
    for item in timeline:
        receipt = getattr(item, "receipt", None)
        if receipt is not None:
            found[TimelineRef(item.offset, item.index)] = receipt  # type: ignore[attr-defined]
    return found


__all__ = [
    "FAIL",
    "PASS",
    "REPLAY_STATUSES",
    "SAFETY_COUNTERS",
    "TAPE_PREFIXES",
    "Evaluation",
    "ProofInput",
    "RevisionFacts",
    "Selection",
    "Transition",
    "evaluate",
    "hypothesis_id_for",
    "persisted_hypotheses",
    "receipts_of",
    "resolved_state_is_internally_consistent",
    "safety_counters",
    "select_target",
]

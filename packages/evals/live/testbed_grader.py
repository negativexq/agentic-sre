"""Scoring of a testbed run (testbed contract §7).

Scored from the engine's stored diagnosis and the run's chain and timeline; nothing here feeds back
into the engine. Identity is matched by actor (the chain names the fault kind, never the engine's
vocabulary). An invalid run is not scored. A metric the chain does not define for a run is ``None``,
so an aggregate never counts a link the world did not have.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict

from packages.evals.live.ground_truth import RunRecord
from packages.rca.model import Diagnosis, Resolution, RootSupportStatus


class RunScore(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario_id: str
    repeat: int
    family: str
    tier: str
    valid: bool
    execution_witness: bool | None = None
    effect_link: bool | None = None
    propagation_link: bool | None = None
    causes_total: int = 0
    causes_named: int = 0
    instances_total: int = 0
    instances_named: int = 0
    false_strong_authority: int = 0
    false_resolved: bool = False
    false_elimination: int = 0
    abstained: bool | None = None
    reads: int = 0
    time_to_diagnosis_seconds: float | None = None


def _identity(diagnosis: Diagnosis) -> dict[str, tuple[str, frozenset[str]]]:
    """hypothesis id to (actor, exact instance uids), from the diagnosis inventory."""
    return {
        entry.hypothesis_id: (
            entry.causal_actor.canonical if entry.causal_actor is not None else "",
            frozenset(entry.instance_uids or ()),
        )
        for entry in diagnosis.hypothesis_inventory
    }


def _fired_witnesses(diagnosis: Diagnosis) -> list[Any]:
    trace = diagnosis.resolution_trace
    if trace is None:
        return []
    return [
        witness
        for audit in trace.hypothesis_audits
        for record in audit.root_support
        if record.status is RootSupportStatus.FIRED
        for witness in record.witnesses
    ]


def score_run(record: RunRecord, diagnosis: Diagnosis, *, tier: str) -> RunScore:
    if not record.valid:
        return RunScore(
            scenario_id=record.scenario_id,
            repeat=record.repeat,
            family=record.family,
            tier=tier,
            valid=False,
        )
    chain, trace = record.chain, diagnosis.resolution_trace
    identity = _identity(diagnosis)
    chain_actors = chain.actors()
    causes = chain.of_role("cause")
    cause_actors = {link.actor for link in causes}
    execution_actors = {link.actor for link in chain.of_role("execution")} | cause_actors
    supported = set(trace.plausible_hypotheses) if trace is not None else set()
    strong = set(trace.mechanism_verified_hypotheses) if trace is not None else set()
    named_actors = {identity[h][0] for h in supported if h in identity}
    if diagnosis.root_cause is not None:
        named_actors.add(diagnosis.root_cause.canonical)

    witnesses = _fired_witnesses(diagnosis)

    def carried(link_role: str, matches: Any) -> bool | None:
        links = chain.of_role(link_role)
        if not links:
            return None
        return any(matches(w, link) for w in witnesses for link in links)

    execution = None
    if any(link.evidence_class == "execution" for link in chain.links):
        execution_links = {link.actor for link in chain.links if link.evidence_class == "execution"}
        execution = any(
            w.origin.entity.canonical in execution_links or w.actor.canonical in execution_links
            for w in witnesses
        )
    instances_total = sum(1 for link in causes if link.knowable)
    instances_named = sum(
        1
        for link in causes
        if link.knowable
        and any(
            identity[h][0] == link.actor and link.instance_uid in identity[h][1]
            for h in supported
            if h in identity
        )
    )
    false_resolved = diagnosis.resolution is Resolution.RESOLVED and (
        diagnosis.root_cause is None or diagnosis.root_cause.canonical not in cause_actors
    )
    eliminated = (
        set(trace.eliminated_hypotheses) - set(trace.explained_hypotheses)
        if trace is not None
        else set()
    )
    finished, alerted = record.diagnosis_completed_at, record.timeline.alert_fired_at
    return RunScore(
        scenario_id=record.scenario_id,
        repeat=record.repeat,
        family=record.family,
        tier=tier,
        valid=True,
        execution_witness=execution,
        effect_link=carried("target_effect", lambda w, link: w.symptom.canonical == link.actor),
        propagation_link=carried(
            "propagation",
            lambda w, link: any(
                link.actor in (hop.source.canonical, hop.target.canonical) for hop in w.path
            ),
        ),
        causes_total=len(causes),
        causes_named=sum(1 for actor in cause_actors if actor in named_actors),
        instances_total=instances_total,
        instances_named=instances_named,
        false_strong_authority=sum(
            1 for h in strong if identity.get(h, ("", frozenset()))[0] not in chain_actors
        ),
        false_resolved=false_resolved,
        false_elimination=sum(
            1 for h in eliminated if identity.get(h, ("", frozenset()))[0] in execution_actors
        ),
        abstained=(
            diagnosis.root_cause is None and not strong
            if record.family == "negative-control"
            else None
        ),
        reads=len(diagnosis.steps),
        time_to_diagnosis_seconds=(
            (finished - alerted.at).total_seconds()
            if finished is not None and alerted is not None
            else None
        ),
    )


def _rate(values: Iterable[bool | None]) -> float | None:
    defined = [v for v in values if v is not None]
    return sum(defined) / len(defined) if defined else None


def _median(values: Iterable[float | None]) -> float | None:
    present = sorted(v for v in values if v is not None)
    return present[len(present) // 2] if present else None


def aggregate(scores: Sequence[RunScore]) -> dict[str, dict[str, Any]]:
    """Per ``tier/family``: valid-run count and the contract's metrics (never merged across tiers)."""
    groups: dict[str, list[RunScore]] = defaultdict(list)
    for score in scores:
        groups[f"{score.tier}/{score.family}"].append(score)
    out: dict[str, dict[str, Any]] = {}
    for key, items in sorted(groups.items()):
        valid = [s for s in items if s.valid]
        out[key] = {
            "runs": len(items),
            "valid_runs": len(valid),
            "execution_witness_recall": _rate(s.execution_witness for s in valid),
            "effect_link_recall": _rate(s.effect_link for s in valid),
            "propagation_link_recall": _rate(s.propagation_link for s in valid),
            "cause_recall": (
                sum(s.causes_named for s in valid) / total
                if (total := sum(s.causes_total for s in valid))
                else None
            ),
            "instance_recall": (
                sum(s.instances_named for s in valid) / total
                if (total := sum(s.instances_total for s in valid))
                else None
            ),
            "false_strong_authority": sum(s.false_strong_authority for s in valid),
            "false_resolved": sum(1 for s in valid if s.false_resolved),
            "false_elimination": sum(s.false_elimination for s in valid),
            "abstention_correctness": _rate(s.abstained for s in valid),
            "median_reads": sorted(s.reads for s in valid)[len(valid) // 2] if valid else None,
            "median_time_to_diagnosis_seconds": _median(s.time_to_diagnosis_seconds for s in valid),
        }
    return out

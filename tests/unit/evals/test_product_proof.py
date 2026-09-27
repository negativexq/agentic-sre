"""M19-7.P1: proofs verify the product's persisted decisions; safety counters are exact."""

from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from packages.evals.product.actions import DeletionReceipt, PodIdentity, ReadinessReceipt
from packages.evals.product.artifact import ProviderTape, build_artifact
from packages.evals.product.proof import (
    FAIL,
    PASS,
    SAFETY_COUNTERS,
    ProofInput,
    RevisionFacts,
    evaluate,
    receipts_of,
    safety_counters,
    select_target,
)
from packages.evals.product.runner import TimelineRecord
from packages.evals.product.spec import (
    MANIFESTATION_ONLY,
    Expectation,
    ProductScenario,
    ProofId,
    RevisionCheckpoint,
    TargetHypothesisRef,
    TimelineRef,
)
from packages.rca.model import (
    Confidence,
    Diagnosis,
    EliminationConsequence,
    EliminationPrecondition,
    EliminationTimeBasis,
    EntityInstanceRef,
    EntityRef,
    Finding,
    FindingKind,
    Hypothesis,
    HypothesisEpistemicState,
    HypothesisInventoryEntry,
    HypothesisResolutionAudit,
    HypothesisSignature,
    PreconditionResult,
    PreconditionStatus,
    Resolution,
    ResolutionElimination,
    ResolutionReasonCode,
    ResolutionTrace,
    RulePreconditionAudit,
    Symptoms,
)
from packages.rca.requirements import hypothesis_inventory

R2_ONLY = frozenset({RevisionCheckpoint.R2})
NS = "sre-demo"
T0 = datetime(2026, 9, 26, 12, tzinfo=UTC)
A1 = ("m16.ended-manifestation-episode", "v1")
A2 = ("m16.resource-mechanism", "v1")
POD_B = EntityRef(kind="Pod", name="order-service-abc-xyz", namespace=NS)
POD_B2 = EntityRef(kind="Pod", name="order-service-abc-other", namespace=NS)
ROOT = EntityRef(kind="Deployment", name="payment-service", namespace=NS)
ORDER = EntityRef(kind="Deployment", name="order-service", namespace=NS)
UID_B = "uid-b"
READINESS_REF = TimelineRef(timedelta(minutes=-10))
DECISIVE = "lifecycle:sre-demo:Pod:uid-b:9"


def finding(
    entity: EntityRef, uid: str | None, *ids: str, kind: FindingKind = FindingKind.FAILURE_EVENT
) -> Finding:
    return Finding(
        kind=kind,
        entity=entity,
        entity_instance=EntityInstanceRef(entity=entity, uid=uid) if uid else None,
        at=T0,
        summary="s",
        evidence_ids=ids,
    )


def hyp(
    hid: str, actor: EntityRef, key: str, *findings: Finding, initiating: tuple[Finding, ...] = ()
) -> Hypothesis:
    return Hypothesis(
        hypothesis_id=hid,
        hypothesis_key=key,
        causal_actor=actor,
        findings=(*findings, *initiating),
        initiating_findings=initiating,
    )


def _result(status: PreconditionStatus) -> PreconditionResult:
    if status is PreconditionStatus.DISQUALIFIED:
        return PreconditionResult(status=status, reason="POST_ONSET_READY_FALSE")
    if status is PreconditionStatus.PENDING:
        return PreconditionResult(status=status, not_before=T0 + timedelta(minutes=15))
    return PreconditionResult(status=status)


def audit(
    hid: str,
    *,
    plausible: bool = True,
    state: HypothesisEpistemicState = HypothesisEpistemicState.SUPPORTED,
    statuses: tuple[PreconditionStatus, ...] = (),
) -> HypothesisResolutionAudit:
    return HypothesisResolutionAudit(
        hypothesis_id=hid,
        signature=HypothesisSignature(),
        epistemic_state=state,
        plausible=plausible,
        precondition_audit=tuple(
            RulePreconditionAudit(rule_id=A1[0], result=_result(status)) for status in statuses
        ),
    )


def a1(
    hid: str,
    *,
    uid: str = UID_B,
    decisive: tuple[str, ...] = (DECISIVE,),
    basis: str = "RECOVERED",
    passed: bool = True,
    rule: tuple[str, str] = A1,
    actor: EntityRef = POD_B,
) -> ResolutionElimination:
    return ResolutionElimination(
        hypothesis_id=hid,
        code=ResolutionReasonCode.MANIFESTATION_EPISODE_ENDED_BEFORE_ONSET,
        evidence_ids=decisive,
        rule_id=rule[0],
        rule_version=rule[1],
        consequence=EliminationConsequence.ROOT_INELIGIBILITY,
        targets=(actor.canonical,),
        time_basis=(
            EliminationTimeBasis(
                evidence_ids=decisive[:1], certainty=basis, target=f"{actor.canonical}@{uid}"
            ),
        ),
        preconditions=(EliminationPrecondition(name="continuity", passed=passed),),
        decisive_evidence_ids=decisive,
    )


def contradiction(
    hid: str, *ids: str, passed: bool = True, rule: tuple[str, str] = A2
) -> ResolutionElimination:
    return ResolutionElimination(
        hypothesis_id=hid,
        code=ResolutionReasonCode.OBSERVED_NORMAL_MECHANISM_MISMATCH,
        evidence_ids=ids,
        rule_id=rule[0],
        rule_version=rule[1],
        consequence=EliminationConsequence.CONTRADICTION,
        targets=(ORDER.canonical,),
        preconditions=(EliminationPrecondition(name="coverage", passed=passed),),
        decisive_evidence_ids=ids,
    )


def diagnosis(
    state: Resolution,
    hypotheses: tuple[Hypothesis, ...],
    audits: tuple[HypothesisResolutionAudit, ...],
    *,
    eliminations: tuple[ResolutionElimination, ...] = (),
    leaders: tuple[str, ...] = (),
    root: EntityRef | None = None,
    inventory: tuple[HypothesisInventoryEntry, ...] | None = None,
) -> Diagnosis:
    return Diagnosis(
        incident_id="i1",
        root_cause=root if root is not None else hypotheses[0].causal_actor,
        confidence=Confidence.LIKELY,
        resolution=state,
        summary="s",
        symptoms=Symptoms(onset=T0, last_seen=T0, services=(), namespaces=(NS,), alert_names=()),
        hypothesis=hypotheses[0],
        alternative_hypotheses=hypotheses[1:],
        resolution_trace=ResolutionTrace(
            state=state,
            leading_hypothesis_ids=leaders,
            eliminations=eliminations,
            hypothesis_audits=audits,
        ),
        hypothesis_inventory=inventory
        if inventory is not None
        else hypothesis_inventory(hypotheses),
    )


def spec_change(*ids: str) -> Finding:
    return finding(ROOT, None, *ids, kind=FindingKind.SPEC_CHANGE)


def world(**overrides: Any) -> dict[str, Any]:
    """A PR-01-shaped run: H_B (receipt UID, manifestation-only) and H_root (payment)."""
    n = overrides.pop
    b1 = hyp("h1-b", POD_B, "hkey:b", finding(POD_B, UID_B, "event:1"))
    r1root = hyp("h1-root", ROOT, "hkey:root", initiating=(spec_change("journal:5"),))
    r1 = diagnosis(
        Resolution.AMBIGUOUS,
        (r1root, b1),
        (audit("h1-root"), audit("h1-b")),
        leaders=("h1-root", "h1-b"),
    )
    early = diagnosis(
        Resolution.AMBIGUOUS,
        (
            hyp("h2-root", ROOT, "hkey:root", initiating=(spec_change("journal:5"),)),
            hyp("h2-b", POD_B, "hkey:b", finding(POD_B, UID_B, "event:1")),
        ),
        (audit("h2-root"), audit("h2-b")),
        leaders=("h2-root", "h2-b"),
    )
    r2 = diagnosis(
        Resolution.RESOLVED,
        (
            hyp("h3-root", ROOT, "hkey:root", initiating=(spec_change("journal:5"),)),
            hyp("h3-b", POD_B, "hkey:b", finding(POD_B, UID_B, "event:1")),
        ),
        (audit("h3-root"), audit("h3-b", plausible=False)),
        eliminations=(a1("h3-b"),),
        leaders=("h3-root",),
        root=ROOT,
    )
    base = frozenset({"event:1", "journal:5"})
    state: dict[str, Any] = {
        "r1": n("r1", r1),
        "early": n("early", early),
        "r2": n("r2", r2),
        "u1": n("u1", base),
        "u_early": n("u_early", base | {"event:2"}),
        "u2": n("u2", base | {"event:2", DECISIVE}),
        "uids": n("uids", {DECISIVE: UID_B, "event:1": UID_B}),
        "receipt": n("receipt", ReadinessReceipt(T0, T0, UID_B, UID_B)),
        "ref": n(
            "ref",
            TargetHypothesisRef(
                source="action_receipt",
                mechanism=frozenset({MANIFESTATION_ONLY}),
                action=READINESS_REF,
                episode_basis="RECOVERED",
            ),
        ),
        "expectation": n("expectation", None),
        "tape": n("tape", frozenset()),
        "replayed": n("replayed", ("d", "d", "d")),
    }
    assert not overrides, overrides
    return state


def facts(state: dict[str, Any] | None = None, **overrides: Any) -> ProofInput:
    s = state or world(**overrides)
    expectation = s["expectation"] or Expectation(
        proofs=(ProofId.T1, ProofId.T2, ProofId.T3, ProofId.T4, ProofId.N0),
        target_hypothesis_ref=s["ref"],
        target_rule=A1,
        target_consequence="ROOT_INELIGIBILITY",
        root_actor=ROOT.canonical,
        resolved_allowed=R2_ONLY,
    )
    revisions = tuple(
        RevisionFacts(
            number=number,
            trigger=trigger,
            diagnosis=document,
            universe=universe,
            tape_ids=s["tape"],
            evidence_uids=s["uids"],
            persisted_digest="d",
            replay_digest=replayed,
        )
        for (number, trigger, document, universe), replayed in zip(
            (
                (1, "INITIAL", s["r1"], s["u1"]),
                (2, "MANUAL", s["early"], s["u_early"]),
                (3, "EVIDENCE_DEADLINE", s["r2"], s["u2"]),
            ),
            s["replayed"],
            strict=True,
        )
    )
    return ProofInput(expectation, {READINESS_REF: s["receipt"]}, revisions)


# --- the positive PR-01 world ------------------------------------------------------------------


def test_the_positive_world_passes_every_proof_with_zero_safety_counters() -> None:
    result = evaluate(facts())
    assert result.proof == {ProofId.T1: PASS, ProofId.T2: PASS, ProofId.T3: PASS, ProofId.T4: PASS}
    assert result.negatives == {ProofId.N0: PASS}
    assert result.safety == dict.fromkeys(SAFETY_COUNTERS, 0)
    assert result.transition is not None
    assert (result.transition.hypothesis_key, result.transition.rule_id) == ("hkey:b", A1[0])
    assert result.transition.decisive_evidence_ids == (DECISIVE,)
    assert result.reasons == {}


def test_only_the_scenarios_own_proofs_are_evaluated() -> None:
    s = world()
    s["expectation"] = Expectation(
        proofs=(ProofId.T4,), target_hypothesis_ref=s["ref"], root_actor=ROOT.canonical
    )
    result = evaluate(facts(s))
    assert result.proof == {ProofId.T4: PASS} and result.negatives == {}


# --- H_x selection (frozen A1 rule) ------------------------------------------------------------


def _key(**overrides: Any) -> str | None:
    s = world(**overrides)
    return select_target(s["ref"], s["r1"], {READINESS_REF: s["receipt"]}).hypothesis_key


def _r1(
    *hypotheses: Hypothesis, inventory: tuple[HypothesisInventoryEntry, ...] | None = None
) -> Diagnosis:
    return diagnosis(
        Resolution.AMBIGUOUS,
        hypotheses,
        tuple(audit(h.hypothesis_id) for h in hypotheses),
        inventory=inventory,
    )


ROOT_H = hyp("h1-root", ROOT, "hkey:root", initiating=(spec_change("journal:5"),))


@pytest.mark.parametrize(
    ("r1", "expected"),
    [
        pytest.param(
            _r1(ROOT_H, hyp("h1-b", POD_B, "hkey:b", finding(POD_B, UID_B, "e"))),
            "hkey:b",
            id="exact",
        ),
        pytest.param(
            _r1(
                ROOT_H,
                hyp("h1-b", POD_B, "hkey:b", finding(POD_B, UID_B, "e"), finding(POD_B, None, "f")),
            ),
            "hkey:b",
            id="uid-less-finding-neither-joins-nor-disqualifies",
        ),
        pytest.param(
            _r1(ROOT_H, hyp("h1-b", POD_B, "hkey:b", finding(POD_B, None, "f"))),
            None,
            id="uid-less-alone",
        ),
        pytest.param(
            _r1(
                ROOT_H,
                hyp(
                    "h1-b",
                    POD_B,
                    "hkey:b",
                    finding(POD_B, UID_B, "e"),
                    finding(POD_B, "uid-x", "f"),
                ),
            ),
            None,
            id="another-uid",
        ),
        pytest.param(
            _r1(ROOT_H, hyp("h1-b", POD_B, "hkey:b", finding(POD_B, "uid-x", "e"))),
            None,
            id="no-match",
        ),
        pytest.param(
            _r1(
                ROOT_H,
                hyp(
                    "h1-b",
                    POD_B,
                    "hkey:b",
                    initiating=(finding(POD_B, UID_B, "e", kind=FindingKind.SPEC_CHANGE),),
                ),
            ),
            None,
            id="mechanism-mismatch",
        ),
        pytest.param(
            _r1(
                ROOT_H,
                hyp(
                    "h1-b",
                    EntityRef(kind="Deployment", name="x", namespace=NS),
                    "hkey:b",
                    finding(POD_B, UID_B, "e"),
                ),
            ),
            None,
            id="actor-not-a-pod",
        ),
        pytest.param(
            _r1(
                ROOT_H,
                hyp("h1-b", POD_B, "hkey:b", finding(POD_B, UID_B, "e")),
                hyp("h1-c", POD_B, "hkey:c", finding(POD_B, UID_B, "g")),
            ),
            None,
            id="two-matches",
        ),
        pytest.param(
            _r1(
                ROOT_H,
                hyp("h1-b", POD_B, "hkey:b", finding(POD_B, UID_B, "e")),
                inventory=(
                    *hypothesis_inventory(
                        (ROOT_H, hyp("h1-b", POD_B, "hkey:b", finding(POD_B, UID_B, "e")))
                    ),
                    # Outside the persisted subset: another Pod actor, same UID and class, other key.
                    *hypothesis_inventory(
                        (hyp("h1-z", POD_B2, "hkey:z", finding(POD_B2, UID_B, "g")),)
                    ),
                ),
            ),
            None,
            id="unpersisted-second-semantic-match-in-full-inventory",
        ),
        pytest.param(
            _r1(
                ROOT_H,
                hyp("h1-b", POD_B, "hkey:b", finding(POD_B, UID_B, "e")),
                inventory=(
                    HypothesisInventoryEntry(hypothesis_id="h1-root", hypothesis_key="hkey:root"),
                    HypothesisInventoryEntry(hypothesis_id="h1-b", hypothesis_key="hkey:b"),
                ),
            ),
            None,
            id="legacy-inventory-without-metadata",
        ),
        pytest.param(
            _r1(
                ROOT_H,
                hyp(
                    "h1-b",
                    POD_B,
                    "hkey:b",
                    finding(POD_B, UID_B, "e"),
                    initiating=(finding(POD_B, UID_B, "s", kind=FindingKind.SPEC_CHANGE),),
                ),
            ),
            None,
            id="same-uid-extra-mechanism",
        ),
    ],
)
def test_the_receipt_reference_selects_exactly_one_persisted_hypothesis(
    r1: Diagnosis, expected: str | None
) -> None:
    assert _key(r1=r1) == expected


def test_a_receipt_that_changed_uid_selects_nothing() -> None:
    assert _key(receipt=ReadinessReceipt(T0, T0, UID_B, "uid-new")) is None


def test_a_deletion_receipt_names_the_deleted_instance() -> None:
    s = world(receipt=DeletionReceipt(T0, T0, PodIdentity("p", UID_B), PodIdentity("q", "uid-q")))
    assert (
        select_target(s["ref"], s["r1"], {READINESS_REF: s["receipt"]}).hypothesis_key == "hkey:b"
    )


def test_a_static_actor_reference_uses_the_exact_actor_and_class() -> None:
    ref = TargetHypothesisRef(
        source="static_actor", mechanism=frozenset({"SPEC_CHANGE"}), actor=ROOT.canonical
    )
    r1 = world()["r1"]
    assert select_target(ref, r1, {}).hypothesis_key == "hkey:root"
    wrong = TargetHypothesisRef(
        source="static_actor",
        mechanism=frozenset({"SPEC_CHANGE", "IMAGE_CHANGE"}),
        actor=ROOT.canonical,
    )
    assert select_target(wrong, r1, {}).hypothesis_key is None  # exact set, no subset match


def test_without_a_selected_target_every_applicable_proof_fails() -> None:
    result = evaluate(facts(receipt=ReadinessReceipt(T0, T0, "uid-x", "uid-x")))
    assert set(result.proof.values()) == {FAIL} and set(result.negatives.values()) == {FAIL}
    assert "0 R1 inventory hypotheses" in result.reasons["T1"]
    assert result.transition is None


def test_missing_metadata_fails_instead_of_passing() -> None:
    s = world()
    s["expectation"] = Expectation(proofs=(ProofId.T3, ProofId.T4), target_hypothesis_ref=s["ref"])
    result = evaluate(facts(s))
    assert result.proof == {ProofId.T3: FAIL, ProofId.T4: FAIL}


# --- per-predicate negatives --------------------------------------------------------------------


def _with(state: dict[str, Any], key: str, **changes: Any) -> dict[str, Any]:
    state[key] = state[key].model_copy(update=changes)
    return state


def _trace(state: dict[str, Any], key: str, **changes: Any) -> dict[str, Any]:
    document = state[key]
    return _with(state, key, resolution_trace=document.resolution_trace.model_copy(update=changes))


NEGATIVES: list[tuple[str, ProofId, Callable[[dict[str, Any]], Any]]] = [
    ("r1-resolved", ProofId.T1, lambda s: _with(s, "r1", resolution=Resolution.RESOLVED)),
    (
        "r1-one-plausible",
        ProofId.T1,
        lambda s: _trace(
            s, "r1", hypothesis_audits=(audit("h1-root"), audit("h1-b", plausible=False))
        ),
    ),
    (
        "r1-hx-contradicted",
        ProofId.T1,
        lambda s: _trace(
            s,
            "r1",
            hypothesis_audits=(
                audit("h1-root"),
                audit("h1-b", state=HypothesisEpistemicState.CONTRADICTED),
            ),
        ),
    ),
    ("d-empty", ProofId.T2, lambda s: _trace(s, "r2", eliminations=(a1("h3-b", decisive=()),))),
    ("d-in-u-r1", ProofId.T2, lambda s: s.update(u1=s["u1"] | {DECISIVE})),
    ("d-outside-u-r2", ProofId.T2, lambda s: s.update(u2=s["u2"] - {DECISIVE})),
    (
        "wrong-rule-version",
        ProofId.T3,
        lambda s: _trace(s, "r2", eliminations=(a1("h3-b", rule=(A1[0], "v2")),)),
    ),
    (
        "precondition-failed",
        ProofId.T3,
        lambda s: _trace(s, "r2", eliminations=(a1("h3-b", passed=False),)),
    ),
    (
        "instance-binding-other-uid",
        ProofId.T3,
        lambda s: _trace(s, "r2", eliminations=(a1("h3-b", uid="uid-x"),)),
    ),
    ("decisive-other-uid", ProofId.T3, lambda s: s.update(uids={**s["uids"], DECISIVE: "uid-x"})),
    (
        "terminated-not-recovered",
        ProofId.T3,
        lambda s: _trace(s, "r2", eliminations=(a1("h3-b", basis="TERMINATED"),)),
    ),
    (
        "targets-other-actor",
        ProofId.T3,
        lambda s: _trace(s, "r2", eliminations=(a1("h3-b", actor=ORDER),)),
    ),
    ("r2-ambiguous", ProofId.T4, lambda s: _with(s, "r2", resolution=Resolution.AMBIGUOUS)),
    ("r2-other-leader", ProofId.T4, lambda s: _with(s, "r2", root_cause=ORDER)),
    (
        "early-already-eliminates",
        ProofId.N0,
        lambda s: _trace(s, "early", eliminations=(a1("h2-b"),)),
    ),
    ("d-in-u-early", ProofId.N0, lambda s: s.update(u_early=s["u_early"] | {DECISIVE})),
    (
        "hx-missing-in-early",
        ProofId.N0,
        lambda s: _with(
            s,
            "early",
            hypothesis_inventory=(
                HypothesisInventoryEntry(hypothesis_id="h2-root", hypothesis_key="hkey:root"),
            ),
        ),
    ),
]


@pytest.mark.parametrize(
    ("name", "proof", "mutate"), NEGATIVES, ids=[item[0] for item in NEGATIVES]
)
def test_each_predicate_fails_on_its_own_counterexample(
    name: str, proof: ProofId, mutate: Callable[[dict[str, Any]], Any]
) -> None:
    state = world()
    mutate(state)
    result = evaluate(facts(state))
    verdicts = {**result.proof, **result.negatives}
    assert verdicts[proof] == FAIL, (name, result.reasons)
    assert proof.value in result.reasons


def test_n0_fails_when_r_early_resolves_with_the_target_rule() -> None:
    s = world()
    other = hyp("h2-z", POD_B, "hkey:z", finding(POD_B, "uid-z", "event:9"))
    _with(s, "early", resolution=Resolution.RESOLVED)
    _trace(s, "early", eliminations=(a1("h2-z", uid="uid-z", decisive=("event:9",), actor=POD_B),))
    s["early"] = s["early"].model_copy(
        update={"alternative_hypotheses": (*s["early"].alternative_hypotheses, other)}
    )
    assert evaluate(facts(s)).negatives[ProofId.N0] == FAIL


# --- N1 / N2 / N3 -----------------------------------------------------------------------------


def _n1_world(
    r2_eliminations: tuple[ResolutionElimination, ...], statuses: tuple[PreconditionStatus, ...]
) -> ProofInput:
    s = world()
    _with(s, "r2", resolution=Resolution.AMBIGUOUS)
    _trace(
        s,
        "r2",
        eliminations=r2_eliminations,
        hypothesis_audits=(audit("h3-root"), audit("h3-b", statuses=statuses)),
        leading_hypothesis_ids=("h3-root", "h3-b"),
    )
    s["expectation"] = Expectation(
        proofs=(ProofId.N1,), target_hypothesis_ref=s["ref"], target_rule=A1
    )
    return facts(s)


def test_n1_passes_when_continuity_is_disqualified_and_h_b_stays_eligible() -> None:
    assert evaluate(_n1_world((), (PreconditionStatus.DISQUALIFIED,))).negatives == {
        ProofId.N1: PASS
    }


@pytest.mark.parametrize(
    ("eliminations", "statuses"),
    [
        pytest.param((a1("h3-b"),), (PreconditionStatus.DISQUALIFIED,), id="recovered-elimination"),
        pytest.param((), (PreconditionStatus.PENDING,), id="only-pending"),
        pytest.param((), (), id="no-audit"),
    ],
)
def test_n1_fails_otherwise(
    eliminations: tuple[ResolutionElimination, ...], statuses: tuple[PreconditionStatus, ...]
) -> None:
    assert evaluate(_n1_world(eliminations, statuses)).negatives == {ProofId.N1: FAIL}


def _a2_world(r2_eliminations: tuple[ResolutionElimination, ...], proof: ProofId) -> ProofInput:
    cpu = hyp(
        "h{n}-cpu",
        ORDER,
        "hkey:cpu",
        initiating=(finding(ORDER, None, "journal:7", kind=FindingKind.SPEC_CHANGE),),
    )
    s = world()
    for key, n in (("r1", 1), ("early", 2), ("r2", 3)):
        item = cpu.model_copy(update={"hypothesis_id": f"h{n}-cpu"})
        document = s[key]
        s[key] = document.model_copy(
            update={
                "alternative_hypotheses": (*document.alternative_hypotheses, item),
                "hypothesis_inventory": (
                    *document.hypothesis_inventory,
                    *hypothesis_inventory((item,)),
                ),
            }
        )
    _trace(s, "r2", eliminations=r2_eliminations)
    s["expectation"] = Expectation(
        proofs=(proof,),
        target_hypothesis_ref=TargetHypothesisRef(
            source="static_actor", mechanism=frozenset({"SPEC_CHANGE"}), actor=ORDER.canonical
        ),
        target_rule=A2,
    )
    s["u2"] = s["u2"] | {"prometheus:cov"}
    s["tape"] = frozenset({"prometheus:cov"})
    return facts(s)


@pytest.mark.parametrize("proof", [ProofId.N2, ProofId.N3])
def test_n2_n3_pass_without_an_a2_contradiction_and_fail_with_one(proof: ProofId) -> None:
    assert evaluate(_a2_world((), proof)).negatives == {proof: PASS}
    failing = _a2_world((contradiction("h3-cpu", "prometheus:cov"),), proof)
    assert evaluate(failing).negatives == {proof: FAIL}


def test_t3_accepts_an_a2_contradiction_with_tape_coverage() -> None:
    s = _a2_world((contradiction("h3-cpu", "prometheus:cov"),), ProofId.T3)
    expectation = replace(s.expectation, proofs=(ProofId.T3,), target_consequence="CONTRADICTION")
    assert evaluate(replace(s, expectation=expectation)).proof == {ProofId.T3: PASS}


# --- safety counters -------------------------------------------------------------------------------


def _counters(state: dict[str, Any]) -> dict[str, int | None]:
    return safety_counters(facts(state))[0]


SAFETY: list[tuple[str, str, Callable[[dict[str, Any]], Any], int | None]] = [
    ("leader-not-root-actor", "wrong_actor", lambda s: _with(s, "r2", root_cause=ORDER), 1),
    (
        "decisive-other-uid",
        "uid_misbinding",
        lambda s: s.update(uids={**s["uids"], DECISIVE: "uid-x"}),
        1,
    ),
    ("decisive-uid-unknown", "uid_misbinding", lambda s: s.update(uids={"event:1": UID_B}), None),
    (
        "contradiction-without-evidence",
        "missing_to_contradiction",
        lambda s: _trace(s, "r2", eliminations=(a1("h3-b"), contradiction("h3-root"))),
        1,
    ),
    (
        "contradiction-failed-precondition",
        "missing_to_contradiction",
        lambda s: _trace(
            s, "r2", eliminations=(a1("h3-b"), contradiction("h3-root", "event:1", passed=False))
        ),
        1,
    ),
    ("cited-outside-universe", "fabricated", lambda s: s.update(u1=s["u1"] - {"event:1"}), 1),
    ("replay-differs", "replay_divergence", lambda s: s.update(replayed=("d", "x", "d")), 1),
    ("replay-failed", "replay_divergence", lambda s: s.update(replayed=(None, "d", None)), None),
    (
        "tape-id-not-on-tape",
        "unresolved_tape_evidence_id",
        lambda s: (
            _with(s, "r1", evidence=(finding(ROOT, None, "prometheus:x"),))
            and s.update(u1=s["u1"] | {"prometheus:x"})
        ),
        1,
    ),
]


@pytest.mark.parametrize(
    ("name", "counter", "mutate", "expected"), SAFETY, ids=[item[0] for item in SAFETY]
)
def test_each_safety_counter_detects_its_integrity_violation(
    name: str, counter: str, mutate: Callable[[dict[str, Any]], Any], expected: int | None
) -> None:
    state = world()
    assert _counters(state)[counter] == 0
    mutate(state)
    assert _counters(state)[counter] == expected, name


def test_a_cited_synthetic_tombstone_is_counted() -> None:
    state = world()
    _trace(state, "r2", eliminations=(a1("h3-b", decisive=(DECISIVE, "cluster:missing")),))
    state["u2"] = state["u2"] | {"cluster:missing"}
    state["uids"] = {**state["uids"], "cluster:missing": UID_B}
    base = facts(state)
    marked = replace(
        base,
        revisions=(
            *base.revisions[:2],
            replace(base.revisions[2], synthetic_ids=frozenset({"cluster:missing"})),
        ),
    )
    assert safety_counters(marked)[0]["synthetic_cluster_evidence"] == 1


def test_wrong_actor_is_unmeasured_without_a_root_actor_only_when_something_resolved() -> None:
    s = world()
    s["expectation"] = Expectation(target_hypothesis_ref=s["ref"])
    counters, reasons = safety_counters(facts(s))
    assert counters["wrong_actor"] is None and "wrong_actor" in reasons
    _with(s, "r2", resolution=Resolution.AMBIGUOUS)
    assert safety_counters(facts(s))[0]["wrong_actor"] == 0


# --- timeline receipts, artifact, purity -----------------------------------------------------------


def test_receipts_come_from_the_verified_timeline_records() -> None:
    from packages.evals.product.actions import SetReadiness  # noqa: PLC0415

    receipt = ReadinessReceipt(T0, T0, UID_B, UID_B)
    action = SetReadiness("order-service", True, timedelta(seconds=40))
    records = [
        TimelineRecord(timedelta(minutes=-10), action, T0, receipt, 0),
        TimelineRecord(timedelta(0), action, T0),
    ]
    assert receipts_of(records) == {READINESS_REF: receipt}


def test_the_artifact_carries_the_evaluation() -> None:
    result = evaluate(facts())
    s = world()
    scenario = ProductScenario(
        "PR-01", (), Expectation(proofs=(ProofId.T1, ProofId.N0)), tier="DEV"
    )
    summaries = [
        {
            "diagnosis_id": 10 + n,
            "revision_number": n,
            "trigger": trigger,
            "run_id": f"run-{n}",
            "window_end": T0.isoformat(),
            "manifest_digest": "m",
            "tape_digest": "t",
            "epistemic_digest": "e",
            "engine_version": "1",
            "config_digest": "c",
            "resolution": "AMBIGUOUS",
        }
        for n, trigger in ((1, "INITIAL"), (2, "MANUAL"), (3, "EVIDENCE_DEADLINE"))
    ]
    document = build_artifact(
        scenario=scenario,
        commit="a" * 40,
        protocol_commit="b" * 40,
        incident_id="i1",
        onset=T0,
        activations=[],
        timeline=[],
        summaries=summaries,
        requirements={},
        provider_tape=ProviderTape(
            reads=0, capture_reads=0, engine_reads=0, investigation_reads=0, errors=0
        ),
        evaluation=result,
    ).document()
    assert document["proof"] == {"T1": "PASS", "T2": "PASS", "T3": "PASS", "T4": "PASS"}
    assert document["negatives"] == {"N0": "PASS"}
    assert document["safety"] == dict.fromkeys(SAFETY_COUNTERS, 0)
    assert document["transition"]["hypothesis_key"] == "hkey:b"
    del s


def test_the_proof_module_knows_no_database_http_or_cluster() -> None:
    source = (Path(__file__).resolve().parents[3] / "packages/evals/product/proof.py").read_text()
    modules = {
        alias.name if isinstance(node, ast.Import) else (node.module or "")
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }
    forbidden = (
        "sqlalchemy",
        "urllib",
        "http",
        "socket",
        "subprocess",
        "kubernetes",
        "packages.storage",
        "packages.evals.product.live",
    )
    assert not [m for m in modules if m.startswith(forbidden)]


# --- read-only collection before teardown ------------------------------------------------------------


class _FakeSource:
    def __init__(self) -> None:
        from packages.rca.model import (  # noqa: PLC0415
            ClusterEvent,
            ObjectVersion,
            PodStatusObservation,
        )

        self._versions = {
            POD_B: [
                ObjectVersion(
                    entity=POD_B, uid=UID_B, observed_at=T0, body={}, evidence_id="journal:1"
                ),
                ObjectVersion(
                    entity=POD_B, uid=None, observed_at=T0, body={}, evidence_id="cluster:missing"
                ),
            ]
        }
        self._events = [ClusterEvent.model_construct(evidence_id="event:1", involved_uid=UID_B)]
        self._status = [PodStatusObservation.model_construct(evidence_id=DECISIVE, uid=UID_B)]

    def object_history(self) -> Any:
        return self._versions

    def events(self) -> Any:
        return self._events

    def pod_status_observations(self) -> Any:
        return self._status

    def error_logs(self) -> Any:
        return []

    def traffic_observations(self) -> Any:
        return []

    def trace_observations(self) -> Any:
        return []


def _stored_revision(factory: Any, document: Diagnosis) -> int:
    from uuid import uuid4  # noqa: PLC0415

    from packages.storage.models import DiagnosisRow, InvestigationReadRow  # noqa: PLC0415

    with factory() as session:
        row = DiagnosisRow(
            incident_id=uuid4(),
            created_at=T0,
            root_cause=None,
            confidence="LIKELY",
            mode="deterministic",
            run_id="run-1",
            document=document.model_dump(mode="json"),
            revision_number=1,
            trigger="INITIAL",
            epistemic_digest="d",
        )
        session.add(row)
        for sequence, committed in ((0, T0), (1, None)):
            session.add(
                InvestigationReadRow(
                    run_id="run-1",
                    sequence=sequence,
                    caller_class="ENGINE",
                    capability="c",
                    query_key=f"q{sequence}",
                    query_descriptor={},
                    started_at=T0,
                    finished_at=T0,
                    committed_at=committed,
                    status="SUCCESS",
                    observation=None,
                    evidence_ids=[f"prometheus:{sequence}"],
                    error_type=None,
                    error_message=None,
                )
            )
        session.commit()
        return row.diagnosis_id


def test_collection_reads_documents_sources_tape_and_replay_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy import create_engine, func, select  # noqa: PLC0415

    from packages.evals.product import proof_inputs  # noqa: PLC0415
    from packages.rca.replay import ReplaySource  # noqa: PLC0415
    from packages.storage.database import create_session_factory  # noqa: PLC0415
    from packages.storage.models import Base  # noqa: PLC0415

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    diagnosis_id = _stored_revision(factory, world()["r1"])
    monkeypatch.setattr(
        ReplaySource,
        "from_run",
        classmethod(lambda cls, run_id, session_factory: _FakeSource()),
    )

    def counts() -> dict[str, int]:
        with factory() as session:
            return {
                t.name: session.scalar(select(func.count()).select_from(t)) or 0
                for t in Base.metadata.sorted_tables
            }

    before = counts()
    revision = proof_inputs.revision_facts(diagnosis_id, factory, replay=lambda run, sf: "d")
    assert counts() == before
    assert revision.universe == {
        "journal:1",
        "cluster:missing",
        "event:1",
        DECISIVE,
        "prometheus:0",
    }
    assert revision.tape_ids == {"prometheus:0"}  # uncommitted reads are not tape
    assert revision.evidence_uids == {"journal:1": UID_B, "event:1": UID_B, DECISIVE: UID_B}
    assert revision.synthetic_ids == {"cluster:missing"}
    assert (revision.persisted_digest, revision.replay_digest) == ("d", "d")
    assert revision.diagnosis == world()["r1"]

    def broken(run: str, sf: Any) -> str:
        raise RuntimeError("tape mismatch")

    assert proof_inputs.revision_facts(diagnosis_id, factory, replay=broken).replay_digest is None


def test_live_artifacts_carry_the_evaluation_and_a_failed_collection_is_an_error(
    tmp_path: Path,
) -> None:
    from test_product_artifact import SUMMARIES, Git, _live, _result, _scenario  # noqa: PLC0415

    from packages.evals.product.artifact import ArtifactError, load_artifact  # noqa: PLC0415

    seen: list[Any] = []

    def proofs(expectation: Any, timeline: Any, ids: Any) -> Any:
        seen.append(list(ids))
        return evaluate(facts())

    backend = _live(tmp_path, SUMMARIES, Git(), proofs=proofs)
    backend.cluster_up()
    backend.write_artifact(_scenario(ProofId.T1), _result())
    assert seen == [[11, 12, 13]]
    assert backend.artifact_path is not None
    document = load_artifact(backend.artifact_path).document()
    assert document["safety"] == dict.fromkeys(SAFETY_COUNTERS, 0)
    assert document["transition"]["hypothesis_key"] == "hkey:b"

    def failing(expectation: Any, timeline: Any, ids: Any) -> Any:
        raise RuntimeError("replay source missing")

    other = _live(tmp_path / "x", SUMMARIES, Git(), proofs=failing)
    other.cluster_up()
    with pytest.raises(ArtifactError, match="proof inputs"):
        other.write_artifact(_scenario(), _result())


def test_mechanism_is_an_exact_set_not_a_subset_of_the_hypothesis_kinds() -> None:
    both = (
        spec_change("journal:5"),
        finding(ROOT, None, "journal:6", kind=FindingKind.IMAGE_CHANGE),
    )
    r1 = _r1(hyp("h1-root", ROOT, "hkey:root", initiating=both))
    ref = TargetHypothesisRef(
        source="static_actor", mechanism=frozenset({"SPEC_CHANGE"}), actor=ROOT.canonical
    )
    assert select_target(ref, r1, {}).hypothesis_key is None


def test_t1_needs_a_second_plausible_hypothesis_besides_h_x() -> None:
    state = _trace(
        world(), "r1", hypothesis_audits=(audit("h1-root", plausible=False), audit("h1-b"))
    )
    result = evaluate(facts(state))
    assert result.proof[ProofId.T1] == FAIL and "1 plausible" in result.reasons["T1"]


def test_a_receipt_reference_also_needs_the_exact_mechanism_set() -> None:
    kinds = (
        finding(POD_B, UID_B, "e", kind=FindingKind.SPEC_CHANGE),
        finding(POD_B, UID_B, "f", kind=FindingKind.IMAGE_CHANGE),
    )
    r1 = _r1(ROOT_H, hyp("h1-b", POD_B, "hkey:b", initiating=kinds))
    ref = TargetHypothesisRef(
        source="action_receipt", mechanism=frozenset({"SPEC_CHANGE"}), action=READINESS_REF
    )
    receipts = {READINESS_REF: ReadinessReceipt(T0, T0, UID_B, UID_B)}
    assert select_target(ref, r1, receipts).hypothesis_key is None
    exact = TargetHypothesisRef(
        source="action_receipt",
        mechanism=frozenset({"SPEC_CHANGE", "IMAGE_CHANGE"}),
        action=READINESS_REF,
    )
    assert select_target(exact, r1, receipts).hypothesis_key == "hkey:b"


# --- false_resolved: the checkpoint policy (frozen R2 amendment) ---------------------------------


def _resolved(state: dict[str, Any], key: str, leader: str, hid_root: str) -> None:
    """Make revision ``key`` an internally consistent RESOLVED on the payment root."""
    del leader
    document = state[key]
    assert document.resolution_trace is not None
    trace = document.resolution_trace.model_copy(
        update={"state": Resolution.RESOLVED, "leading_hypothesis_ids": (hid_root,)}
    )
    state[key] = document.model_copy(
        update={"resolution": Resolution.RESOLVED, "root_cause": ROOT, "resolution_trace": trace}
    )


@pytest.mark.parametrize(
    ("key", "hid_root"),
    [pytest.param("r1", "h1-root", id="R1"), pytest.param("early", "h2-root", id="R_early")],
)
def test_an_internally_consistent_early_resolution_is_a_false_resolution(
    key: str, hid_root: str
) -> None:
    from packages.evals.product.proof import (
        resolved_state_is_internally_consistent,  # noqa: PLC0415
    )

    state = world()
    _resolved(state, key, "", hid_root)
    assert resolved_state_is_internally_consistent(state[key])
    assert _counters(state)["false_resolved"] == 1


def test_r2_resolved_or_ambiguous_is_allowed_under_the_r2_policy() -> None:
    state = world()
    assert state["r2"].resolution is Resolution.RESOLVED
    assert _counters(state)["false_resolved"] == 0
    _with(state, "r2", resolution=Resolution.AMBIGUOUS)
    assert _counters(state)["false_resolved"] == 0


def test_without_a_policy_false_resolved_is_unmeasured() -> None:
    state = world()
    state["expectation"] = Expectation(
        target_hypothesis_ref=state["ref"], root_actor=ROOT.canonical
    )
    counters, reasons = safety_counters(facts(state))
    assert counters["false_resolved"] is None and "false_resolved" in reasons


def test_a_negative_scenario_may_resolve_at_r2() -> None:
    state = world()
    state["expectation"] = Expectation(
        proofs=(ProofId.N1,),
        target_hypothesis_ref=state["ref"],
        target_rule=A1,
        root_actor=ROOT.canonical,
        resolved_allowed=R2_ONLY,
    )
    assert state["r2"].resolution is Resolution.RESOLVED
    assert _counters(state)["false_resolved"] == 0


def test_internal_inconsistency_is_a_diagnostic_not_a_false_resolution() -> None:
    state = _trace(world(), "r2", leading_hypothesis_ids=("h3-root", "h3-b"))
    counters, reasons = safety_counters(facts(state))
    assert counters["false_resolved"] == 0
    assert "R2" in reasons["resolved_consistency"]


def test_a_legacy_inventory_fails_every_evaluated_proof() -> None:
    state = world()
    legacy = tuple(
        HypothesisInventoryEntry(
            hypothesis_id=item.hypothesis_id, hypothesis_key=item.hypothesis_key
        )
        for item in state["r1"].hypothesis_inventory
    )
    _with(state, "r1", hypothesis_inventory=legacy)
    result = evaluate(facts(state))
    assert set(result.proof.values()) == {FAIL} and set(result.negatives.values()) == {FAIL}
    assert "legacy" in result.reasons["T1"]


# --- the persisted inventory metadata (product change gates) -------------------------------------


def test_the_inventory_records_every_hypothesis_identity() -> None:
    uid_less = finding(POD_B, None, "f")
    entries = hypothesis_inventory(
        (
            hyp(
                "h-b",
                POD_B,
                "hkey:b",
                finding(POD_B, "u2", "e"),
                finding(POD_B, "u1", "g"),
                uid_less,
            ),
            hyp(
                "h-root",
                ROOT,
                "hkey:root",
                initiating=(
                    spec_change("j"),
                    finding(ROOT, None, "k", kind=FindingKind.IMAGE_CHANGE),
                ),
            ),
        )
    )
    by_id = {item.hypothesis_id: item for item in entries}
    assert by_id["h-b"].causal_actor == POD_B
    assert by_id["h-b"].mechanism_class == (MANIFESTATION_ONLY,)
    assert by_id["h-b"].instance_uids == ("u1", "u2")
    assert by_id["h-root"].mechanism_class == ("IMAGE_CHANGE", "SPEC_CHANGE")
    assert by_id["h-root"].instance_uids == ()


def test_legacy_documents_still_load_and_the_epistemic_digest_is_unchanged() -> None:
    from packages.rca.epistemic_digest import diagnosis_epistemic_digest  # noqa: PLC0415

    document = world()["r2"]
    raw = document.model_dump(mode="json")
    for entry in raw["hypothesis_inventory"]:
        for name in ("causal_actor", "mechanism_class", "instance_uids"):
            entry.pop(name)
    legacy = Diagnosis.model_validate(raw)
    assert all(item.mechanism_class is None for item in legacy.hypothesis_inventory)
    assert diagnosis_epistemic_digest(legacy) == diagnosis_epistemic_digest(document)


def test_no_decision_path_reads_the_inventory_metadata() -> None:
    root = Path(__file__).resolve().parents[3]
    readers = set()
    for directory in ("packages/rca", "apps"):
        for path in (root / directory).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Attribute) and node.attr in (
                    "mechanism_class",
                    "instance_uids",
                ):
                    readers.add(str(path.relative_to(root)))
    assert readers == set()


def test_the_rca_result_and_keys_are_unchanged_on_the_ablation_fixtures() -> None:
    from test_product_ablation import _full, a1_source, a2_source  # noqa: PLC0415

    from packages.rca.engine import EngineConfig, build_case  # noqa: PLC0415

    for make in (a1_source, a2_source):
        diagnosis = _full(make())
        assert diagnosis.resolution is Resolution.RESOLVED
        case = build_case(make(), EngineConfig())
        keys = {item.hypothesis_id: item.hypothesis_key for item in case.hypotheses}
        assert {
            item.hypothesis_id: item.hypothesis_key for item in diagnosis.hypothesis_inventory
        } == {hid: key or None for hid, key in keys.items()}


# --- replay status (M20.1b) ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        pytest.param(("PASS", "PASS", "PASS"), 0, id="all-pass"),
        pytest.param(("PASS", "DIVERGED", "PASS"), 1, id="one-diverged"),
        pytest.param(("PASS", "ERROR", "PASS"), None, id="error-is-unmeasured"),
        pytest.param(("UNSUPPORTED", "PASS", "PASS"), None, id="unsupported-is-unmeasured"),
        pytest.param(("PASS", "PASS", "NOT_ATTEMPTED"), None, id="not-attempted-is-unmeasured"),
    ],
)
def test_replay_divergence_counts_only_actual_divergence(
    statuses: tuple[str, str, str], expected: int | None
) -> None:
    base = facts()
    marked = replace(
        base,
        revisions=tuple(
            replace(revision, replay_status=status)
            for revision, status in zip(base.revisions, statuses, strict=True)
        ),
    )
    counters, reasons = safety_counters(marked)
    assert counters["replay_divergence"] == expected
    if expected is None:
        assert "replay_divergence" in reasons and "replay_status" in reasons


def test_an_unknown_replay_status_is_refused() -> None:
    base = facts()
    marked = replace(
        base, revisions=(replace(base.revisions[0], replay_status="MAYBE"), *base.revisions[1:])
    )
    with pytest.raises(ValueError, match="unknown replay status"):
        safety_counters(marked)

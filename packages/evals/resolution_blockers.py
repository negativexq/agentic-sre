"""Read-only projection of recorded M21 decisions; never a second resolver.

Missing fields are checked before model defaults can turn them into empty sets.
Witness population coverage is not measurement accounting. Indirect coverage
bridges require recorded authority; this module never re-executes their rules.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Status = Literal["SATISFIED", "UNSATISFIED", "UNASSESSED", "NOT_RECORDED"]
CONDITIONS = (
    "UNIQUE_STRONG_CAUSE",
    "WITNESS_SCOPE_COVERED",
    "NO_UNRESOLVED_RIVALS",
    "MATERIAL_FRONTIER_CLOSED",
)


class ConditionAssessment(BaseModel):
    model_config = ConfigDict(frozen=True)
    condition: str
    status: Status
    basis: str
    claim_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    recorded_facts: dict[str, Any] = Field(default_factory=dict)


class EvidenceDeficit(BaseModel):
    model_config = ConfigDict(frozen=True)
    condition: str
    subject_id: str
    code: str
    recorded_reason: str
    evidence_ids: tuple[str, ...] = ()
    gap_ids: tuple[str, ...] = ()


class BlockerReport(BaseModel):
    model_config = ConfigDict(frozen=True)
    report_version: str = "resolution-blockers.v1"
    incident_id: str | None
    resolution: str | None
    run_id: str | None = None
    revision_number: int | None = None
    engine_version: str | None = None
    assessment_kind: Literal["RECORDED_DIAGNOSIS", "OFFLINE_RECALCULATION"] = "RECORDED_DIAGNOSIS"
    conditions: tuple[ConditionAssessment, ...]
    unmet_condition_count: int
    unassessed_condition_count: int
    material_question_count: int | None
    unresolved_claim_count: int | None
    unresolved_claims: tuple[dict[str, Any], ...]
    material_questions: tuple[dict[str, Any], ...]
    evidence_deficits: tuple[EvidenceDeficit, ...]
    recorded_explanations: tuple[dict[str, Any], ...]
    potential: tuple[str, ...]
    measurement_accounting: str = "NOT_ASSESSED_BY_BLOCKER_ANALYSIS"


def _entity(value: Any) -> str | None:
    if not isinstance(value, Mapping) or not value.get("kind") or not value.get("name"):
        return None
    return f"{value.get('namespace', '_cluster')}/{value['kind']}/{value['name']}"


def _refs(findings: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    return tuple(sorted({ref for f in findings for ref in f.get("evidence_ids", [])}))


def _recorded_ids(trace: Mapping[str, Any], field: str) -> bool:
    value = trace.get(field)
    return isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value)


def analyze_resolution_blockers(document: Mapping[str, Any], **metadata: Any) -> BlockerReport:
    """Project one immutable diagnosis document without querying or changing it."""
    document = deepcopy(dict(document))
    trace = document.get("resolution_trace")
    modern = isinstance(trace, Mapping) and trace.get("semantics_version") == "m21.v3"
    trace = trace if isinstance(trace, Mapping) else {}
    assessments: list[ConditionAssessment] = []
    deficits: list[EvidenceDeficit] = []
    supported = (
        set(trace["plausible_hypotheses"])
        if _recorded_ids(trace, "plausible_hypotheses")
        else set()
    )
    strong = (
        set(trace["mechanism_verified_hypotheses"])
        if _recorded_ids(trace, "mechanism_verified_hypotheses")
        else set()
    )
    claims = {
        h["hypothesis_id"]: h
        for h in [
            document.get("hypothesis"),
            *(document.get("alternative_hypotheses") or []),
            *(document.get("ambiguous_hypotheses") or []),
        ]
        if isinstance(h, Mapping) and "hypothesis_id" in h
    }
    audits = {a["hypothesis_id"]: a for a in (trace.get("hypothesis_audits") or [])}

    def condition(name: str, fields: tuple[str, ...], passed: bool, basis: str) -> None:
        status: Status = (
            "UNASSESSED"
            if not modern
            else "NOT_RECORDED"
            if any(f not in trace for f in fields)
            else "UNASSESSED"
            if any(not _recorded_ids(trace, f) for f in fields)
            else "SATISFIED"
            if passed
            else "UNSATISFIED"
        )
        assessments.append(ConditionAssessment(condition=name, status=status, basis=basis))

    condition(
        CONDITIONS[0],
        ("plausible_hypotheses", "mechanism_verified_hypotheses"),
        len(supported) == 1 and strong == supported,
        "Recorded supported and mechanism-verified claim sets; no support rules re-run.",
    )
    condition(
        CONDITIONS[2],
        ("unresolved_hypotheses",),
        not trace.get("unresolved_hypotheses"),
        "Recorded unresolved claim inventory.",
    )
    condition(
        CONDITIONS[3],
        ("material_frontier_ids",),
        not trace.get("material_frontier_ids"),
        "Recorded limiting material frontier IDs, not every OPEN answer.",
    )
    assessments[0] = assessments[0].model_copy(
        update={
            "claim_ids": tuple(sorted(supported)),
            "recorded_facts": {
                "supported_ids": sorted(supported) if "plausible_hypotheses" in trace else None,
                "strong_ids": sorted(strong) if "mechanism_verified_hypotheses" in trace else None,
            },
        }
    )
    if assessments[0].status == "UNSATISFIED":
        deficits.append(
            EvidenceDeficit(
                condition=CONDITIONS[0],
                subject_id="supported_claim_inventory",
                code="UNIQUE_STRONG_CAUSE_NOT_ESTABLISHED",
                recorded_reason=f"Recorded supported count {len(supported)}, strong count {len(strong)}.",
            )
        )
        for hid in sorted(supported - strong):
            for support in audits.get(hid, {}).get("root_support") or []:
                if support.get("support_kind") != "OBSERVED_MECHANISM_CAUSE":
                    continue
                for reason in support.get("reasons", []):
                    deficits.append(
                        EvidenceDeficit(
                            condition=CONDITIONS[0],
                            subject_id=hid,
                            code="RECORDED_SUPPORT_LIMITATION",
                            recorded_reason=reason,
                            evidence_ids=tuple(support.get("decisive_evidence_ids", [])),
                        )
                    )

    # Direct positive coverage is sufficient, but missing direct coverage does
    # not disprove ownership or sole-ready bridges whose inputs are not here.
    scope_status: Status = "UNASSESSED"
    scope_basis = "Final witness-scope result/bridge proof is not recorded separately."
    witness_refs: set[str] = set()
    if not modern:
        scope_basis = "This report interprets recorded m21.v3 conditions only."
    elif "hypothesis_audits" not in trace:
        scope_status = "NOT_RECORDED"
        scope_basis = "Witness audit inventory is missing."
    elif not isinstance(trace["hypothesis_audits"], (list, tuple)):
        scope_basis = "Witness audit inventory is not an assessable list."
    elif supported and strong == supported:
        complete_direct = True
        for hid in supported:
            claim = claims.get(hid)
            audit = audits.get(hid)
            if (
                claim is None
                or audit is None
                or "symptom_entities" not in claim
                or "root_support" not in audit
                or not isinstance(claim.get("symptom_entities"), (list, tuple))
                or not isinstance(audit.get("root_support"), (list, tuple))
            ):
                complete_direct = False
                continue
            declared = {_entity(e) for e in claim["symptom_entities"]}
            witnesses = [
                w
                for r in audit["root_support"]
                if r.get("support_kind") == "OBSERVED_MECHANISM_CAUSE"
                and r.get("status") == "FIRED"
                for w in r.get("witnesses", [])
            ]
            covered = {_entity(w.get("symptom")) for w in witnesses}
            witness_refs.update(e for w in witnesses for e in w.get("evidence_ids", []))
            if None in declared or not declared <= covered:
                complete_direct = False
        if complete_direct:
            scope_status = "SATISFIED"
            scope_basis = (
                "Every declared symptom entity is explicitly named by fired mechanism witnesses."
            )
        elif (
            document.get("resolution") == "RESOLVED"
            and trace.get("state") == "RESOLVED"
            and all(a.status == "SATISFIED" for a in assessments)
        ):
            scope_status = "SATISFIED"
            scope_basis = "Recorded M21 RESOLVED authority certifies scope; bridge details are not reconstructed."
    assessments.append(
        ConditionAssessment(
            condition=CONDITIONS[1],
            status=scope_status,
            basis=scope_basis,
            claim_ids=tuple(sorted(supported)),
            evidence_ids=tuple(sorted(witness_refs)),
        )
    )
    assessments.sort(key=lambda a: CONDITIONS.index(a.condition))

    rivals: list[dict[str, Any]] = []
    for hid in trace.get("unresolved_hypotheses") or []:
        claim, audit = claims.get(hid, {}), audits.get(hid, {})
        evidence = _refs(claim.get("findings", []))
        reasons = audit.get("plausibility_reasons", [])
        rivals.append(
            {
                "claim_id": hid,
                "actor": _entity(claim.get("causal_actor")),
                "reason_codes": reasons,
                "evidence_ids": evidence,
                "findings": claim.get("findings", []),
            }
        )
        for reason in reasons:
            if reason == "NO_ONSET_CAPABLE_INITIATING_EVIDENCE":
                deficits.append(
                    EvidenceDeficit(
                        condition=CONDITIONS[2],
                        subject_id=hid,
                        code="INITIATING_EVIDENCE_NOT_ESTABLISHED",
                        recorded_reason=reason,
                        evidence_ids=evidence,
                    )
                )

    answers = {a["alternative_id"]: a for a in (trace.get("frontier_answers") or [])}
    questions = []
    for aid in trace.get("material_frontier_ids") or []:
        answer = answers.get(aid)
        questions.append({"alternative_id": aid, "answer": answer})
        if answer is None:
            deficits.append(
                EvidenceDeficit(
                    condition=CONDITIONS[3],
                    subject_id=aid,
                    code="ANSWER_NOT_RECORDED",
                    recorded_reason="No answer record for a limiting frontier ID.",
                )
            )
        elif answer.get("blocked_reason"):
            reason = answer["blocked_reason"]
            code = {
                "TRACE_READ_FAILED": "READ_FAILED",
                "NOT_CAPTURED": "OBSERVATION_NOT_CAPTURED",
                "FORM_UNSUPPORTED": "RULE_FORM_UNSUPPORTED",
                "TIMING_UNKNOWN": "TIMING_BINDING_MISSING",
                "NOT_SUFFICIENT": "OBSERVED_EVIDENCE_NOT_SUFFICIENT",
            }.get(reason, "RECORDED_FRONTIER_LIMITATION")
            deficits.append(
                EvidenceDeficit(
                    condition=CONDITIONS[3],
                    subject_id=aid,
                    code=code,
                    recorded_reason=reason,
                    evidence_ids=tuple(answer.get("evidence_ids", [])),
                )
            )

    for gap in document.get("information_gaps") or []:
        for name, subjects in (
            (CONDITIONS[2], gap.get("hypothesis_ids", [])),
            (CONDITIONS[3], gap.get("alternative_ids", [])),
        ):
            active = set(
                trace.get(
                    "unresolved_hypotheses" if name == CONDITIONS[2] else "material_frontier_ids",
                    [],
                )
                or []
            )
            for subject in sorted(active.intersection(subjects)):
                deficits.append(
                    EvidenceDeficit(
                        condition=name,
                        subject_id=subject,
                        code="CURRENT_TOOLS_UNAVAILABLE"
                        if gap.get("resolvability") == "UNRESOLVABLE_WITH_CURRENT_TOOLS"
                        else "RECORDED_INFORMATION_GAP",
                        recorded_reason=gap.get("missing_fact", ""),
                        gap_ids=(gap["gap_id"],),
                        evidence_ids=tuple(gap.get("evidence_refs", [])),
                    )
                )
    if scope_status != "SATISFIED":
        deficits.append(
            EvidenceDeficit(
                condition=CONDITIONS[1],
                subject_id="witness_scope",
                code="COVERAGE_ASSESSMENT_NOT_RECORDED",
                recorded_reason=scope_basis,
            )
        )
    unmet = sum(a.status == "UNSATISFIED" for a in assessments)
    unknown = sum(a.status in {"UNASSESSED", "NOT_RECORDED"} for a in assessments)
    question_count = (
        len(trace["material_frontier_ids"])
        if modern and _recorded_ids(trace, "material_frontier_ids")
        else None
    )
    flags = []
    if unmet == 1:
        flags.append("ONE_CONFIRMED_UNMET_CONDITION")
    if unmet > 1:
        flags.append("MULTIPLE_UNMET_CONDITIONS")
    if unknown:
        flags.append("UNASSESSED_CONDITIONS_PRESENT")
    if rivals:
        flags.append("UNRESOLVED_CLAIMS_PRESENT")
    if question_count == 1 and unmet == 1 and not unknown:
        flags.append("ONE_MATERIAL_QUESTION_REMAINING")
    return BlockerReport(
        incident_id=document.get("incident_id"),
        resolution=document.get("resolution"),
        conditions=tuple(assessments),
        unmet_condition_count=unmet,
        unassessed_condition_count=unknown,
        material_question_count=question_count,
        unresolved_claim_count=len(trace["unresolved_hypotheses"])
        if modern and _recorded_ids(trace, "unresolved_hypotheses")
        else None,
        unresolved_claims=tuple(rivals),
        material_questions=tuple(questions),
        evidence_deficits=tuple(deficits),
        recorded_explanations=tuple(trace.get("explanations", [])),
        potential=tuple(flags),
        **metadata,
    )


def summarize_blockers(reports: Sequence[BlockerReport]) -> dict[str, Any]:
    """Counts are descriptive, never probabilities or independent-blocker claims."""
    combinations = Counter(
        " + ".join(a.condition for a in r.conditions if a.status == "UNSATISFIED")
        or "NONE_CONFIRMED"
        for r in reports
    )
    return {
        "revision_count": len(reports),
        "resolutions": dict(Counter(r.resolution for r in reports)),
        "condition_status_counts": {
            name: dict(
                Counter(a.status for r in reports for a in r.conditions if a.condition == name)
            )
            for name in CONDITIONS
        },
        "unmet_condition_combinations": dict(sorted(combinations.items())),
        "unassessed_revisions": sum(bool(r.unassessed_condition_count) for r in reports),
        "evidence_deficit_codes": dict(
            sorted(Counter(d.code for r in reports for d in r.evidence_deficits).items())
        ),
        "single_material_question_revisions": [
            r.run_id or r.incident_id
            for r in reports
            if "ONE_MATERIAL_QUESTION_REMAINING" in r.potential
        ],
    }

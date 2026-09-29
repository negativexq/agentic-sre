"""M21 D1: `m21.support.change-onset-path.v2` makes the resolver's support predicate explicit.

The actor-specific witness is decision-bearing and included in the digest.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pytest
from rca_builders import at, config_change_source

from packages.rca.engine import build_case, diagnose_case
from packages.rca.epistemic_digest import diagnosis_epistemic_digest
from packages.rca.model import (
    Diagnosis,
    EvidenceTemporalRole,
    Hypothesis,
    HypothesisEpistemicState,
    RootSupportStatus,
)
from packages.rca.resolution import (
    _CHANGE_KINDS,
    CHANGE_ONSET_PATH_RULE,
    assess_hypothesis,
    change_onset_path_support,
)

COVERAGES = [
    pytest.param(at(0), id="covered"),
    pytest.param(None, id="no-coverage"),
    pytest.param(at(13), id="all-pre-existing"),
]


def _case(coverage: datetime | None):  # type: ignore[no-untyped-def]
    return build_case(replace(config_change_source(), alert_coverage_start=coverage))


def _implicit_predicate(hypothesis: Hypothesis) -> bool:
    """The pre-D1 predicate, spelled out independently."""
    return hypothesis.causal_explanation in {"PATH", "DIRECT"} and any(
        finding.temporal_role is EvidenceTemporalRole.INITIATING and finding.kind in _CHANGE_KINDS
        for finding in hypothesis.findings
    )


@pytest.mark.parametrize("coverage", COVERAGES)
def test_fired_is_exactly_the_implicit_support_predicate(coverage: datetime | None) -> None:
    case = _case(coverage)
    assert case.hypotheses
    for hypothesis in case.hypotheses:
        record = change_onset_path_support(hypothesis)
        fired = record.status is RootSupportStatus.FIRED
        assert fired == _implicit_predicate(hypothesis)
        supported = assess_hypothesis(hypothesis).state is HypothesisEpistemicState.SUPPORTED
        assert supported == (fired and not hypothesis.contradictory_findings)


def test_the_three_outcomes_carry_their_evidence_and_reasons() -> None:
    covered = _case(at(0))
    records = {h.causal_actor.canonical: change_onset_path_support(h) for h in covered.hypotheses}
    fired = records["shop/ConfigMap/checkout-flags"]
    assert (fired.rule_id, fired.rule_version) == CHANGE_ONSET_PATH_RULE
    assert fired.status is RootSupportStatus.FIRED
    assert fired.consequence == "ROOT_SUPPORT" and fired.support_kind == "CHANGE_ONSET_PATH"
    assert fired.decisive_evidence_ids and fired.path_shapes
    assert fired.causal_explanation in {"PATH", "DIRECT"}
    not_fired = [
        record for record in records.values() if record.status is RootSupportStatus.NOT_FIRED
    ]
    assert not_fired
    assert all(record.reasons and not record.decisive_evidence_ids for record in not_fired)

    unknown = _case(None)
    assert all(
        change_onset_path_support(hypothesis).status is RootSupportStatus.INAPPLICABLE
        and change_onset_path_support(hypothesis).reasons == ("CAUSAL_ONSET_UNKNOWN",)
        for hypothesis in unknown.hypotheses
    )


def _with_support(diagnosis: Diagnosis, status: RootSupportStatus) -> Diagnosis:
    trace = diagnosis.resolution_trace
    assert trace is not None
    audits = tuple(
        audit.model_copy(
            update={
                "root_support": tuple(
                    record.model_copy(update={"status": status, "decisive_evidence_ids": ("x",)})
                    for record in audit.root_support
                )
            }
        )
        for audit in trace.hypothesis_audits
    )
    return diagnosis.model_copy(
        update={"resolution_trace": trace.model_copy(update={"hypothesis_audits": audits})}
    )


@pytest.mark.parametrize("coverage", COVERAGES)
def test_the_d1_record_is_inside_the_epistemic_digest(coverage: datetime | None) -> None:
    diagnosis = diagnose_case(_case(coverage))
    trace = diagnosis.resolution_trace
    assert trace is not None and all(audit.root_support for audit in trace.hypothesis_audits)
    digest = diagnosis_epistemic_digest(diagnosis)
    for status in RootSupportStatus:
        assert diagnosis_epistemic_digest(_with_support(diagnosis, status)) != digest


def test_the_record_is_persisted_in_the_diagnosis_document() -> None:
    diagnosis = diagnose_case(_case(at(0)))
    restored = Diagnosis.model_validate(diagnosis.model_dump(mode="json"))
    assert restored.resolution_trace == diagnosis.resolution_trace
    trace = restored.resolution_trace
    assert trace is not None
    assert {
        record.rule_id for audit in trace.hypothesis_audits for record in audit.root_support
    } == {CHANGE_ONSET_PATH_RULE[0], "m21.support.observed-quota-rejection"}

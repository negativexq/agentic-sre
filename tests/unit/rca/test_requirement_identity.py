"""M19-5.5: requirement identity, A1/A2 requirement metadata and Diagnosis provenance."""

from __future__ import annotations

import json
from datetime import timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError
from test_episode_end_preconditions import (
    BOUNDARY as A1_BOUNDARY,
)
from test_episode_end_preconditions import (
    ONSET as A1_ONSET,
)
from test_episode_end_preconditions import (
    POD as A1_POD,
)
from test_episode_end_preconditions import (
    _deleted,
    _evaluation,
    _finding,
    _status,
)
from test_resource_mechanism import (
    DEPLOY,
    POD,
    _container,
    _deployment,
    _limit_change,
    _normal,
)
from test_resource_mechanism_preconditions import (
    BOUNDARY as A2_BOUNDARY,
)
from test_resource_mechanism_preconditions import (
    EARLY,
    LATE,
    _engine_source,
    _evaluate,
    _partial,
)

from packages.rca.engine import build_case, diagnose_case
from packages.rca.episode_end import RULE_ID as A1_RULE
from packages.rca.epistemic_digest import diagnosis_epistemic_digest
from packages.rca.model import (
    Diagnosis,
    EvidenceTemporalRole,
    Finding,
    FindingKind,
    Hypothesis,
    PreconditionResult,
    PreconditionStatus,
    RequirementAuditReason,
    RequirementEvaluation,
    RequirementKind,
    RequirementTarget,
)
from packages.rca.requirements import (
    REQUIREMENT_SCHEMA,
    build_requirement_evaluations,
    hypothesis_inventory,
    requirement_key,
    requirement_targets_document,
)
from packages.rca.resource_mechanism import RULE_ID as A2_RULE
from packages.rca.resource_mechanism import ResourceCoverageDisqualificationReason

INCIDENT = UUID("00000000-0000-4000-8000-000000000001")
OTHER_INCIDENT = UUID("00000000-0000-4000-8000-000000000002")


def _a1(uid: str = "u1", **overrides: Any) -> RequirementEvaluation:
    values: dict[str, Any] = {
        "hypothesis_id": "hypothesis:worker",
        "hypothesis_key": "hkey:worker",
        "rule_id": A1_RULE,
        "rule_version": "v1",
        "kind": RequirementKind.STATUS_CONTINUITY,
        "targets": (RequirementTarget(entity="shop/Pod/worker-0", uid=uid),),
        "result": PreconditionResult.pending(A1_BOUNDARY),
    }
    values.update(overrides)
    return RequirementEvaluation(**values)


def _series(*pairs: tuple[str, str]) -> tuple[RequirementTarget, ...]:
    return tuple(
        RequirementTarget(entity=DEPLOY.canonical, container=container, resource=resource)
        for container, resource in pairs
    )


def _a2(**overrides: Any) -> RequirementEvaluation:
    values: dict[str, Any] = {
        "hypothesis_id": "hypothesis:payment-limits",
        "hypothesis_key": "hkey:payment",
        "rule_id": A2_RULE,
        "rule_version": "v1",
        "kind": RequirementKind.RESOURCE_COVERAGE,
        "targets": _series(("app", "memory")),
        "result": PreconditionResult.pending(A2_BOUNDARY),
    }
    values.update(overrides)
    return RequirementEvaluation(**values)


# --- requirement_key ---------------------------------------------------------


def test_requirement_key_is_the_full_sha256_of_the_frozen_canonical_envelope() -> None:
    evaluation = _a2(targets=_series(("app", "cpu"), ("app", "memory")))
    envelope = {
        "schema": "agentic-sre.evidence-requirement.v1",
        "incident_id": "00000000-0000-4000-8000-000000000001",
        "hypothesis_key": "hkey:payment",
        "rule_id": "m16.resource-pressure",
        "rule_version": "v1",
        "kind": "RESOURCE_COVERAGE",
        "targets": [
            {"entity": "shop/Deployment/payment", "container": "app", "resource": "cpu"},
            {"entity": "shop/Deployment/payment", "container": "app", "resource": "memory"},
        ],
    }
    canonical = json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    key = requirement_key(INCIDENT, evaluation)

    assert REQUIREMENT_SCHEMA == envelope["schema"]
    assert key == sha256(canonical.encode("utf-8")).hexdigest()
    assert len(key) == 64 and all(char in "0123456789abcdef" for char in key)
    assert requirement_targets_document(evaluation) == envelope["targets"]
    assert requirement_targets_document(_a1()) == [{"entity": "shop/Pod/worker-0", "uid": "u1"}]


def test_requirement_key_changes_with_every_identity_dimension() -> None:
    base = requirement_key(INCIDENT, _a1())
    changed = {
        "incident": requirement_key(OTHER_INCIDENT, _a1()),
        "hypothesis_key": requirement_key(INCIDENT, _a1(hypothesis_key="hkey:other")),
        "rule_id": requirement_key(INCIDENT, _a1(rule_id="m16.other")),
        "rule_version": requirement_key(INCIDENT, _a1(rule_version="v2")),
        "kind": requirement_key(
            INCIDENT,
            _a1(kind=RequirementKind.RESOURCE_COVERAGE, targets=_series(("app", "memory"))),
        ),
        "targets": requirement_key(INCIDENT, _a1("u2")),
        "series": requirement_key(INCIDENT, _a2(targets=_series(("app", "cpu")))),
    }
    assert base not in changed.values()
    assert len(set(changed.values())) == len(changed)
    assert requirement_key(INCIDENT, _a2()) != changed["series"]


def test_requirement_key_ignores_revision_deadline_outcome_and_hypothesis_id() -> None:
    key = requirement_key(INCIDENT, _a1())
    for other in (
        _a1(result=PreconditionResult.pending(A1_BOUNDARY + timedelta(minutes=5))),
        _a1(result=PreconditionResult.passed()),
        _a1(result=PreconditionResult.disqualified("POST_ONSET_READY_FALSE")),
        _a1(result=None, audit_reason=RequirementAuditReason.NO_DATA_AFTER_DEADLINE),
        _a1(hypothesis_id="hypothesis:revision-local-id-of-a-later-revision"),
    ):
        assert requirement_key(INCIDENT, other) == key


def test_requirement_key_fails_loudly_without_identity() -> None:
    with pytest.raises(ValueError, match="hypothesis_key"):
        requirement_key(INCIDENT, _a1(hypothesis_key=None))
    with pytest.raises(TypeError, match="UUID"):
        requirement_key(str(INCIDENT), _a1())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "overrides",
    [
        {"result": None},  # neither result nor audit reason
        {"audit_reason": RequirementAuditReason.SCOPE_EXITED},  # both
        {"targets": ()},
        {"targets": (RequirementTarget(entity="shop/Pod/worker-0"),)},  # no uid
        {
            "targets": (
                RequirementTarget(entity="shop/Pod/worker-0", uid="u1"),
                RequirementTarget(entity="shop/Pod/worker-0", uid="u2"),
            )
        },  # one obligation per UID
        {"hypothesis_key": ""},
    ],
)
def test_status_continuity_evaluation_shape_is_enforced(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _a1(**overrides)


@pytest.mark.parametrize(
    "targets",
    [
        _series(("app", "memory"), ("app", "cpu")),  # unsorted
        _series(("app", "memory"), ("app", "memory")),  # repeated
        (RequirementTarget(entity=DEPLOY.canonical, uid="u1"),),  # A1 shape
        (RequirementTarget(entity=DEPLOY.canonical, container="app"),),  # no resource
    ],
)
def test_resource_coverage_targets_are_unique_sorted_series(
    targets: tuple[RequirementTarget, ...],
) -> None:
    with pytest.raises(ValidationError):
        _a2(targets=targets)


# --- A1 per-UID requirement metadata ----------------------------------------


def _instances(result: Any) -> dict[str, tuple[Any, Any]]:
    (items,) = result.instance_requirements.values()
    return {
        item.uid: (
            item.result.status if item.result is not None else None,
            item.audit_reason,
        )
        for item in items
    }


def test_a1_ended_uid_passes_its_requirement_while_another_uid_is_pending() -> None:
    result = _evaluation(
        _finding("u1"),
        _finding("u2", -8),
        history=(_deleted("u1"),),
        statuses=(_status("u2", 8),),
    )
    # No A1 elimination: u2 still blocks the hypothesis-level end.
    assert result.ended_episodes == {}
    (pending,) = result.preconditions["hypothesis:worker"]
    assert pending.status is PreconditionStatus.PENDING
    assert _instances(result) == {
        "u1": (PreconditionStatus.PASS, None),
        "u2": (PreconditionStatus.PENDING, None),
    }


def test_a1_requirement_outcomes_mirror_each_uid_decision() -> None:
    ended = _evaluation(_finding(), statuses=(_status("u1", 20),))
    assert ended.ended_episodes
    assert _instances(ended) == {"u1": (PreconditionStatus.PASS, None)}
    blocked = _evaluation(_finding(), statuses=(_status("u1", 4, ready=False, since=None),))
    assert _instances(blocked) == {"u1": (PreconditionStatus.DISQUALIFIED, None)}
    missing = _evaluation(_finding(), evaluation_at=A1_BOUNDARY)
    assert _instances(missing) == {"u1": (None, RequirementAuditReason.NO_DATA_AFTER_DEADLINE)}
    early_unknown = _evaluation(_finding(), evaluation_at=A1_ONSET + timedelta(minutes=2))
    assert early_unknown.instance_requirements == {}  # nothing known yet: no state


def test_a1_scope_exit_is_structural_and_missing_uid_is_not_an_exit() -> None:
    supporting_change = Finding(
        kind=FindingKind.SPEC_CHANGE,
        entity=A1_POD,
        at=A1_ONSET - timedelta(minutes=1),
        incident_onset=A1_ONSET,
        temporal_role=EvidenceTemporalRole.AMBIGUOUS,
        summary="pod spec changed",
        evidence_ids=("journal:9",),
    )
    exited = _evaluation(_finding("u1"), supporting_change, statuses=(_status("u1", 8),))
    assert _instances(exited) == {"u1": (None, RequirementAuditReason.SCOPE_EXITED)}
    assert exited.preconditions == {} and exited.reasons == {}
    uidless = _evaluation(_finding(uid=None), statuses=(_status("u1", 8),))
    assert uidless.instance_requirements == {}
    # One manifestation lacks its UID: missing evidence, so u1 is not scope-exited.
    partial_identity = _evaluation(
        _finding("u1"), _finding(uid=None, minute=-8), statuses=(_status("u1", 8),)
    )
    assert partial_identity.instance_requirements == {}
    assert partial_identity.preconditions == {}


# --- A2 lowered-series requirement metadata ----------------------------------


def _coverage(result: Any) -> tuple[Any, ...]:
    (item,) = result.requirements.values()
    return (
        item.series,
        item.result.status if item.result is not None else None,
        item.result.reason if item.result is not None else None,
        item.audit_reason,
    )


def test_a2_requirement_outcomes_mirror_the_coverage_decision() -> None:
    series = (("app", "memory"),)
    assert _coverage(_evaluate(records=[], evaluation_at=EARLY)) == (
        series,
        PreconditionStatus.PENDING,
        None,
        None,
    )
    assert _coverage(_evaluate(records=[_partial()], evaluation_at=LATE)) == (
        series,
        None,
        None,
        RequirementAuditReason.PARTIAL_COVERAGE_AFTER_DEADLINE,
    )
    assert _coverage(_evaluate(records=[_normal()], evaluation_at=LATE)) == (
        series,
        PreconditionStatus.PASS,
        None,
        None,
    )
    assert _evaluate(records=[], evaluation_at=None).requirements == {}


def test_a2_positive_pressure_disqualifies_the_requirement_without_state_change() -> None:
    reason = ResourceCoverageDisqualificationReason.POSITIVE_PRESSURE_EVIDENCE.value
    oom = {"containerStatuses": [{"lastState": {"terminated": {"reason": "OOMKilled"}}}]}
    for result in (
        _evaluate(records=[_partial(peak=0.95)], evaluation_at=EARLY),
        _evaluate(records=[], pod_status=oom, evaluation_at=LATE),
    ):
        assert _coverage(result) == (
            (("app", "memory"),),
            PreconditionStatus.DISQUALIFIED,
            reason,
            None,
        )
        # The frozen 5.3 decision outputs are unchanged: no mismatch, no audit state.
        assert result.mismatches == {} and result.preconditions == {} and result.reasons == {}


def test_a2_scope_exit_is_a_failed_prerequisite_not_missing_evidence() -> None:
    image_change = _evaluate(
        records=[], evaluation_at=EARLY, after=_deployment(_container("128Mi", image="payment:2"))
    )
    assert _coverage(image_change) == (
        (("app", "memory"),),
        None,
        None,
        RequirementAuditReason.SCOPE_EXITED,
    )
    assert image_change.preconditions == {}
    # A raised limit leaves no lowered series to name: nothing to key.
    assert (
        _evaluate(
            records=[], evaluation_at=EARLY, after=_deployment(_container("1Gi"))
        ).requirements
        == {}
    )


# --- engine provenance -------------------------------------------------------


def test_diagnosis_carries_requirement_evaluations_and_the_full_inventory() -> None:
    diagnosis = diagnose_case(build_case(_engine_source(EARLY)))
    (hypothesis,) = diagnosis.hypothesis_inventory
    (evaluation,) = diagnosis.requirement_evaluations
    assert evaluation.hypothesis_id == hypothesis.hypothesis_id
    assert evaluation.hypothesis_key == hypothesis.hypothesis_key is not None
    assert (evaluation.rule_id, evaluation.rule_version, evaluation.kind) == (
        A2_RULE,
        "v1",
        RequirementKind.RESOURCE_COVERAGE,
    )
    assert requirement_targets_document(evaluation) == [
        {"entity": DEPLOY.canonical, "container": "app", "resource": "memory"}
    ]
    assert evaluation.result == PreconditionResult.pending(A2_BOUNDARY)
    # Persisted and reloaded exactly.
    reloaded = Diagnosis.model_validate(diagnosis.model_dump(mode="json"))
    assert reloaded.requirement_evaluations == diagnosis.requirement_evaluations
    assert reloaded.hypothesis_inventory == diagnosis.hypothesis_inventory
    assert POD.canonical not in json.dumps(requirement_targets_document(evaluation))


def test_requirement_provenance_is_outside_the_epistemic_digest() -> None:
    for cutoff in (EARLY, LATE):
        diagnosis = diagnose_case(build_case(_engine_source(cutoff)))
        assert diagnosis.requirement_evaluations and diagnosis.hypothesis_inventory
        stripped = diagnosis.model_copy(
            update={"requirement_evaluations": (), "hypothesis_inventory": ()}
        )
        assert diagnosis_epistemic_digest(stripped) == diagnosis_epistemic_digest(diagnosis)


def test_documents_written_before_requirement_provenance_still_load() -> None:
    document = diagnose_case(build_case(_engine_source(EARLY))).model_dump(mode="json")
    del document["requirement_evaluations"], document["hypothesis_inventory"]
    legacy = Diagnosis.model_validate(document)
    assert legacy.requirement_evaluations == () and legacy.hypothesis_inventory == ()


def _hypothesis(index: int, key: str) -> Hypothesis:
    return _limit_change().model_copy(
        update={"hypothesis_id": f"hypothesis:{index:02d}", "hypothesis_key": key}
    )


def test_inventory_is_uncapped_ordered_and_keeps_repeated_or_missing_keys() -> None:
    hypotheses = [_hypothesis(index, f"hkey:{index % 5}") for index in reversed(range(12))]
    hypotheses.append(_hypothesis(12, ""))
    inventory = hypothesis_inventory(hypotheses)
    assert len(inventory) == 13  # beyond the 8-item resolution audit cap
    assert [entry.hypothesis_id for entry in inventory] == sorted(
        h.hypothesis_id for h in hypotheses
    )
    keys = [entry.hypothesis_key for entry in inventory]
    assert keys.count("hkey:0") == 3  # duplicates are not deduped
    assert keys[-1] is None  # an empty key is recorded as missing, never invented


def test_evaluations_are_built_for_every_hypothesis_in_a_stable_order() -> None:
    hypotheses = [_hypothesis(index, f"hkey:{index}") for index in reversed(range(10))]
    from packages.rca.resource_mechanism import CoverageRequirement  # noqa: PLC0415

    coverage = {
        h.hypothesis_id: CoverageRequirement(
            (("app", "memory"),), result=PreconditionResult.pending(A2_BOUNDARY)
        )
        for h in hypotheses
    }
    built = build_requirement_evaluations(
        hypotheses, instance_requirements={}, coverage_requirements=coverage
    )
    assert [item.hypothesis_id for item in built] == sorted(coverage)
    assert (
        build_requirement_evaluations(
            list(reversed(hypotheses)), instance_requirements={}, coverage_requirements=coverage
        )
        == built
    )

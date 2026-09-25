from __future__ import annotations

import os
import subprocess
import sys
from copy import deepcopy
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from rca_builders import ref

from packages.rca.model import (
    Confidence,
    Diagnosis,
    EliminationConsequence,
    Hypothesis,
    HypothesisEpistemicState,
    HypothesisResolutionAudit,
    HypothesisSignature,
    Resolution,
    ResolutionElimination,
    ResolutionReasonCode,
    ResolutionTrace,
    Symptoms,
)
from packages.rca.revision_diff import (
    ManifestDiffStatus,
    PersistedDiagnosisRevision,
    diff_revisions,
)

AT = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def _hypothesis(
    identifier: str,
    key: str | None,
    actor: str = "shop/Deployment/api",
) -> Hypothesis:
    return Hypothesis(
        hypothesis_id=identifier,
        hypothesis_key=key or "",
        causal_actor=ref(actor),
        score=0.4,
        reasons=(f"explanation-{identifier}",),
    )


def _audit(
    identifier: str,
    state: HypothesisEpistemicState = HypothesisEpistemicState.SUPPORTED,
) -> HypothesisResolutionAudit:
    return HypothesisResolutionAudit(
        hypothesis_id=identifier,
        signature=HypothesisSignature(),
        epistemic_state=state,
        plausible=state is HypothesisEpistemicState.SUPPORTED,
    )


def _elimination(
    identifier: str,
    *,
    code: ResolutionReasonCode = ResolutionReasonCode.EXPLICIT_TEMPORAL_CONTRADICTION,
    rule_id: str = "m16.temporal-contradiction",
    rule_version: str = "v1",
    evidence_ids: tuple[str, ...] = ("event:1",),
    decisive_evidence_ids: tuple[str, ...] = ("read:1",),
    detail: str = "explanation",
    consequence: EliminationConsequence | None = EliminationConsequence.CONTRADICTION,
) -> ResolutionElimination:
    return ResolutionElimination(
        hypothesis_id=identifier,
        code=code,
        rule_id=rule_id,
        rule_version=rule_version,
        evidence_ids=evidence_ids,
        decisive_evidence_ids=decisive_evidence_ids,
        detail=detail,
        consequence=consequence,
    )


def _diagnosis(
    hypotheses: tuple[Hypothesis, ...] = (),
    *,
    states: dict[str, HypothesisEpistemicState] | None = None,
    eliminations: tuple[ResolutionElimination, ...] = (),
    resolution: Resolution = Resolution.AMBIGUOUS,
) -> Diagnosis:
    items = hypotheses
    hypothesis = items[0] if items else None
    alternatives = items[1:] if items else ()
    audits = tuple(
        _audit(identifier, state) for identifier, state in sorted((states or {}).items())
    )
    return Diagnosis(
        incident_id="incident-1",
        root_cause=hypothesis.causal_actor if hypothesis is not None else None,
        confidence=Confidence.LIKELY,
        resolution=resolution,
        summary="summary is not diff state",
        symptoms=Symptoms(
            onset=AT,
            last_seen=AT,
            services=("api",),
            namespaces=("shop",),
            alert_names=(),
        ),
        hypothesis=hypothesis,
        alternative_hypotheses=alternatives,
        resolution_trace=ResolutionTrace(
            state=resolution,
            hypothesis_audits=audits,
            eliminations=eliminations,
            rationale="rationale is not diff state",
        ),
    )


def _revision(
    diagnosis: Diagnosis,
    digest: str | None = "manifest:a",
) -> PersistedDiagnosisRevision:
    return PersistedDiagnosisRevision(diagnosis.model_dump(mode="json"), digest)


def test_state_transition_matches_by_key_even_when_revision_local_id_changes() -> None:
    before = _revision(
        _diagnosis(
            (_hypothesis("old-id", "key:A"),), states={"old-id": HypothesisEpistemicState.SUPPORTED}
        )
    )
    after = _revision(
        _diagnosis(
            (_hypothesis("new-id", "key:A"),),
            states={"new-id": HypothesisEpistemicState.CONTRADICTED},
        )
    )

    result = diff_revisions(before, after)

    assert len(result.hypothesis_changes) == 1
    change = result.hypothesis_changes[0]
    assert change.hypothesis_key == "key:A"
    assert change.previous.hypothesis_id == "old-id"
    assert change.current.hypothesis_id == "new-id"
    assert change.previous.state == "SUPPORTED"
    assert change.current.state == "CONTRADICTED"
    assert result.appeared == result.disappeared == ()


def test_new_elimination_is_reported_for_safely_matched_hypothesis() -> None:
    before_h = _hypothesis("old", "key:A")
    after_h = _hypothesis("new", "key:A")
    before = _revision(_diagnosis((before_h,)))
    after = _revision(
        _diagnosis(
            (after_h,),
            eliminations=(_elimination("new", evidence_ids=("event:1", "event:1")),),
        )
    )

    result = diff_revisions(before, after)

    assert len(result.new_eliminations) == 1
    assert result.new_eliminations[0].hypothesis_key == "key:A"
    assert result.new_eliminations[0].hypothesis_id == "new"
    assert result.new_eliminations[0].evidence_ids == ("event:1", "event:1")
    assert result.new_decisive_evidence_ids == ("read:1",)


def test_new_decisive_evidence_is_delta_of_same_elimination_identity() -> None:
    before_h = _hypothesis("old", "key:A")
    after_h = _hypothesis("new", "key:A")
    before = _revision(
        _diagnosis(
            (before_h,), eliminations=(_elimination("old", decisive_evidence_ids=("read:1",)),)
        )
    )
    after = _revision(
        _diagnosis(
            (after_h,),
            eliminations=(_elimination("new", decisive_evidence_ids=("read:2", "read:1")),),
        )
    )

    result = diff_revisions(before, after)

    assert result.new_eliminations == ()
    assert result.new_decisive_evidence_ids == ("read:2",)


def test_decisive_evidence_is_not_subtracted_from_unrelated_hypothesis() -> None:
    before = _revision(
        _diagnosis(
            (_hypothesis("a", "key:A"), _hypothesis("b", "key:B")),
            eliminations=(_elimination("a", decisive_evidence_ids=("read:shared",)),),
        )
    )
    after = _revision(
        _diagnosis(
            (_hypothesis("a2", "key:A"), _hypothesis("b2", "key:B")),
            eliminations=(
                _elimination("a2", decisive_evidence_ids=("read:shared",)),
                _elimination("b2", decisive_evidence_ids=("read:shared",)),
            ),
        )
    )

    result = diff_revisions(before, after)

    assert result.new_decisive_evidence_ids == ("read:shared",)
    assert len(result.new_eliminations) == 1
    assert result.new_eliminations[0].hypothesis_key == "key:B"


def test_appeared_and_disappeared_are_directional_unmatched_hypotheses() -> None:
    before = _revision(_diagnosis((_hypothesis("a", "key:A"), _hypothesis("c", "key:C"))))
    after = _revision(_diagnosis((_hypothesis("a2", "key:A"), _hypothesis("b", "key:B"))))

    result = diff_revisions(before, after)

    assert [(item.hypothesis_key, item.hypothesis_id) for item in result.appeared] == [
        ("key:B", "b")
    ]
    assert [(item.hypothesis_key, item.hypothesis_id) for item in result.disappeared] == [
        ("key:C", "c")
    ]


@pytest.mark.parametrize(
    ("before_keys", "after_keys", "before_ids", "after_ids"),
    [
        (("key:A", "key:A"), ("key:A",), ("a1", "a2"), ("a3",)),
        (("key:A",), ("key:A", "key:A"), ("a1",), ("a2", "a3")),
        (("key:A", "key:A"), ("key:A", "key:A"), ("a1", "a2"), ("a3", "a4")),
    ],
)
def test_duplicate_key_on_either_side_never_matches_and_preserves_multiplicity(
    before_keys: tuple[str, ...],
    after_keys: tuple[str, ...],
    before_ids: tuple[str, ...],
    after_ids: tuple[str, ...],
) -> None:
    before = _revision(
        _diagnosis(tuple(_hypothesis(i, k) for i, k in zip(before_ids, before_keys, strict=True)))
    )
    after = _revision(
        _diagnosis(tuple(_hypothesis(i, k) for i, k in zip(after_ids, after_keys, strict=True)))
    )

    result = diff_revisions(before, after)

    assert result.hypothesis_changes == ()
    assert [item.hypothesis_id for item in result.disappeared] == sorted(before_ids)
    assert [item.hypothesis_id for item in result.appeared] == sorted(after_ids)


def test_null_keys_never_match_even_when_actor_and_state_are_identical() -> None:
    before = _revision(
        _diagnosis((_hypothesis("old", None),), states={"old": HypothesisEpistemicState.SUPPORTED})
    )
    after = _revision(
        _diagnosis((_hypothesis("new", None),), states={"new": HypothesisEpistemicState.SUPPORTED})
    )

    result = diff_revisions(before, after)

    assert result.hypothesis_changes == ()
    assert [item.hypothesis_id for item in result.disappeared] == ["old"]
    assert [item.hypothesis_id for item in result.appeared] == ["new"]


def test_different_hypothesis_id_alone_is_not_a_change() -> None:
    before = _revision(_diagnosis((_hypothesis("old", "key:A"),)))
    after = _revision(_diagnosis((_hypothesis("new", "key:A"),)))

    result = diff_revisions(before, after)

    assert result.hypothesis_changes == ()
    assert result.appeared == result.disappeared == ()


def test_no_change_has_empty_semantic_delta() -> None:
    diagnosis = _diagnosis((_hypothesis("a", "key:A"),))

    result = diff_revisions(_revision(diagnosis), _revision(diagnosis))

    assert not result.resolution_transition.changed
    assert result.hypothesis_changes == ()
    assert result.new_eliminations == ()
    assert result.new_decisive_evidence_ids == ()
    assert result.appeared == result.disappeared == ()
    assert result.manifest_diff.status is ManifestDiffStatus.UNCHANGED


def test_resolution_only_change_is_factual_transition() -> None:
    before = _revision(_diagnosis(resolution=Resolution.INSUFFICIENT_EVIDENCE))
    after = _revision(_diagnosis(resolution=Resolution.AMBIGUOUS))

    result = diff_revisions(before, after)

    assert (result.resolution_transition.previous, result.resolution_transition.current) == (
        "INSUFFICIENT_EVIDENCE",
        "AMBIGUOUS",
    )
    assert result.resolution_transition.changed
    assert result.hypothesis_changes == ()
    assert result.new_eliminations == ()
    assert result.new_decisive_evidence_ids == ()
    assert result.appeared == result.disappeared == ()


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        ("manifest:a", "manifest:a", ManifestDiffStatus.UNCHANGED),
        ("manifest:a", "manifest:b", ManifestDiffStatus.CHANGED),
        (None, "manifest:b", ManifestDiffStatus.UNKNOWN),
        ("manifest:a", None, ManifestDiffStatus.UNKNOWN),
        (None, None, ManifestDiffStatus.UNKNOWN),
    ],
)
def test_manifest_diff_is_digest_only_three_state_transition(
    before: str | None, after: str | None, expected: ManifestDiffStatus
) -> None:
    result = diff_revisions(_revision(_diagnosis(), before), _revision(_diagnosis(), after))

    assert result.manifest_diff.previous_digest == before
    assert result.manifest_diff.current_digest == after
    assert result.manifest_diff.status is expected


def test_permuted_hypothesis_elimination_and_evidence_order_is_deterministic() -> None:
    previous_hypotheses = (
        _hypothesis("a", "key:A"),
        _hypothesis("b", "key:B"),
        _hypothesis("c", "key:C"),
    )
    current_hypotheses = (
        _hypothesis("a2", "key:A"),
        _hypothesis("b2", "key:B"),
        _hypothesis("c2", "key:C"),
    )
    left_before = _revision(_diagnosis(previous_hypotheses))
    right_before = _revision(
        _diagnosis((previous_hypotheses[0], *reversed(previous_hypotheses[1:])))
    )
    left_after = _revision(
        _diagnosis(
            current_hypotheses,
            eliminations=(
                _elimination("a2", evidence_ids=("event:b", "event:a")),
                _elimination(
                    "b2",
                    evidence_ids=("event:d", "event:c"),
                    decisive_evidence_ids=("read:4", "read:3"),
                ),
            ),
        )
    )
    right_after = _revision(
        _diagnosis(
            (current_hypotheses[0], *reversed(current_hypotheses[1:])),
            eliminations=(
                _elimination(
                    "b2",
                    evidence_ids=("event:c", "event:d"),
                    decisive_evidence_ids=("read:3", "read:4"),
                ),
                _elimination("a2", evidence_ids=("event:a", "event:b")),
            ),
        )
    )

    assert diff_revisions(left_before, left_after) == diff_revisions(right_before, right_after)


def test_diff_does_not_mutate_persisted_inputs() -> None:
    previous = _revision(_diagnosis((_hypothesis("a", "key:A"),)))
    current = _revision(_diagnosis((_hypothesis("b", "key:B"),)))
    previous_before, current_before = deepcopy(previous), deepcopy(current)

    diff_revisions(previous, current)

    assert previous == previous_before
    assert current == current_before


def test_diff_output_is_independent_of_python_hash_seed() -> None:
    script = """
import runpy
namespace = runpy.run_path('tests/unit/rca/test_revision_diff.py')
before = namespace['_revision'](namespace['_diagnosis']((namespace['_hypothesis']('a', 'key:A'), namespace['_hypothesis']('b', 'key:B'))))
after = namespace['_revision'](namespace['_diagnosis']((namespace['_hypothesis']('b2', 'key:B'), namespace['_hypothesis']('c', 'key:C'))))
print(repr(namespace['diff_revisions'](before, after)))
"""
    outputs = []
    for seed in ("1", "987654"):
        env = os.environ.copy()
        env["PYTHONHASHSEED"] = seed
        env["PYTHONPATH"] = os.pathsep.join(
            item for item in ("tests", "tests/unit/rca", env.get("PYTHONPATH", "")) if item
        )
        outputs.append(
            subprocess.run(
                [sys.executable, "-c", script],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            ).stdout
        )
    assert outputs[0] == outputs[1]


def test_malformed_persisted_document_fails_loudly() -> None:
    invalid = PersistedDiagnosisRevision({"resolution": "NOT_A_RESOLUTION"}, None)

    with pytest.raises(ValidationError):
        diff_revisions(invalid, _revision(_diagnosis()))

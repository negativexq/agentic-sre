"""``hypothesis_key`` matches one hypothesis across revisions (M19-1.8)."""

from __future__ import annotations

from rca_builders import alert, at, deployment, event, pod, ref, replicaset, service, version
from test_hypotheses import _rollout_source

from packages.rca.engine import build_case
from packages.rca.hypotheses import _hypothesis_id, _hypothesis_key, matchable_by_key
from packages.rca.model import (
    EvidenceTemporalRole,
    Finding,
    FindingKind,
    Hypothesis,
)
from packages.rca.source import InMemorySource

POD = "shop/Pod/catalog-rs-abcde"


def _rollout(*extra_events: tuple[str, float]) -> Hypothesis:
    source = _rollout_source(changed_body=deployment("catalog", image="app:2"))
    source.event_items.append(event(POD, "ImagePullBackOff", 6, type_="Warning"))
    for reason, minutes in extra_events:
        source.event_items.append(event(POD, reason, minutes, type_="Warning"))
    (hypothesis,) = build_case(source).hypotheses
    return hypothesis


def _pod_only(uid: str) -> Hypothesis:
    source = _rollout_source()
    warning = event(POD, "BackOff", 6, type_="Warning")
    source.event_items.append(
        warning.model_copy(
            update={"involved_uid": uid, "evidence_id": f"{warning.evidence_id}:{uid}"}
        )
    )
    (hypothesis,) = build_case(source).hypotheses
    return hypothesis


def test_added_supporting_evidence_keeps_the_key_but_changes_the_id() -> None:
    before, after = _rollout(), _rollout(("BackOff", 7))

    assert len(after.findings) > len(before.findings)
    assert before.hypothesis_key == after.hypothesis_key
    assert before.hypothesis_key.startswith("hkey:")
    assert before.hypothesis_id != after.hypothesis_id


def test_added_evidence_on_the_actor_itself_keeps_the_key() -> None:
    before = _rollout()
    source = _rollout_source(changed_body=deployment("catalog", image="app:2"))
    source.event_items.append(event(POD, "ImagePullBackOff", 6, type_="Warning"))
    source.event_items.append(
        event("shop/Deployment/catalog", "ProgressDeadlineExceeded", 9, type_="Warning")
    )
    (after,) = build_case(source).hypotheses

    assert any(f.entity == after.causal_actor and f not in before.findings for f in after.findings)
    assert after.hypothesis_key == before.hypothesis_key
    assert after.hypothesis_id != before.hypothesis_id


def test_more_initiating_evidence_of_the_same_kind_keeps_the_key() -> None:
    actor = ref("shop/Deployment/catalog")

    def change(evidence: str) -> Finding:
        return Finding(
            kind=FindingKind.IMAGE_CHANGE,
            entity=actor,
            at=at(5),
            temporal_role=EvidenceTemporalRole.INITIATING,
            summary="image changed",
            evidence_ids=(evidence,),
        )

    one, two = (change("journal:1"),), (change("journal:1"), change("journal:2"))
    assert _hypothesis_key(actor, one) == _hypothesis_key(actor, two)
    assert _hypothesis_id(actor, (actor,), one) != _hypothesis_id(actor, (actor,), two)


def test_different_actors_or_episode_classes_get_different_keys() -> None:
    rollout, pod_only = _rollout(), _pod_only("uid-a")

    assert rollout.causal_actor != pod_only.causal_actor
    assert rollout.hypothesis_key != pod_only.hypothesis_key
    actor = ref("shop/Deployment/catalog")
    assert _hypothesis_key(actor, ()) != rollout.hypothesis_key


def test_pod_uid_never_enters_the_key() -> None:
    first, second = _pod_only("uid-a"), _pod_only("uid-b")

    assert first.findings[0].entity_instance is not None
    assert first.hypothesis_key == second.hypothesis_key
    assert first.hypothesis_id != second.hypothesis_id


def test_records_without_a_key_still_load() -> None:
    legacy = Hypothesis.model_validate(
        {"hypothesis_id": "hypothesis:old", "causal_actor": ref("shop/Pod/p").model_dump()}
    )
    assert legacy.hypothesis_key == ""


def _catalog_source(later_image_at: float | None) -> InMemorySource:
    versions = [
        version("shop/Deployment/catalog", 0, deployment("catalog", image="app:1")),
        version("shop/ReplicaSet/catalog-rs", 0, replicaset("catalog-rs", "catalog")),
        version("shop/Pod/catalog-rs-abcde", 0, pod("catalog-rs-abcde", "catalog", "catalog-rs")),
        version("shop/Service/catalog", 0, service("catalog")),
        version("shop/Deployment/catalog", 5, deployment("catalog", image="app:2"), 1),
    ]
    if later_image_at is not None:
        versions.append(
            version(
                "shop/Deployment/catalog", later_image_at, deployment("catalog", image="app:3"), 2
            )
        )
    source = InMemorySource(
        name="hypothesis-key-counter",
        alert_coverage_start=at(0),
        alert_items=[alert("RequestErrorRate", "catalog", 7)],
        versions=versions,
        cutoff=at(40),
    )
    source.event_items.append(event(POD, "ImagePullBackOff", 6, type_="Warning"))
    return source


def _catalog(later_image_at: float | None) -> Hypothesis:
    (hypothesis,) = (
        item
        for item in build_case(_catalog_source(later_image_at)).hypotheses
        if item.causal_actor == ref("shop/Deployment/catalog")
    )
    return hypothesis


def test_real_counter_evidence_keeps_the_key() -> None:
    # A second image change 23 minutes after onset lands past the onset grace,
    # so the pipeline records it as CONSEQUENCE counter evidence on the actor.
    before, after = _catalog(None), _catalog(30)

    assert before.contradictory_findings == ()
    (counter,) = after.contradictory_findings
    assert counter.temporal_role is EvidenceTemporalRole.CONSEQUENCE
    assert counter.entity == after.causal_actor
    assert after.hypothesis_key == before.hypothesis_key


def test_repeated_key_in_one_revision_is_never_matched() -> None:
    first, second = _pod_only("uid-a"), _pod_only("uid-b")
    unique = _rollout()
    assert first.hypothesis_key == second.hypothesis_key

    matchable = matchable_by_key([first, second, unique])

    assert first.hypothesis_key not in matchable
    assert matchable == {unique.hypothesis_key: unique}
    assert matchable_by_key([first]) == {first.hypothesis_key: first}

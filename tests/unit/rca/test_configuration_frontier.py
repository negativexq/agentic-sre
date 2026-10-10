"""m21 §25 and §26: the API access the admission adds, and a configuration observed unchanged through the window."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from rca_builders import at, ref, version

from packages.rca.evidence_coverage import (
    EvidenceCoverage,
    ScopeCoverage,
    SourceContinuity,
    StreamGap,
    TransportCompleteness,
)
from packages.rca.frontier import derive_structural_frontier, unchanged_configuration
from packages.rca.live import keep_write_times
from packages.rca.model import Lifecycle, ObjectVersion, StructuralAlternative, Symptoms
from packages.rca.ranking import Context
from packages.rca.topology import Topology, api_access_refs, derive_edges

ONSET = at(120)
CA = "shop/ConfigMap/kube-root-ca.crt"
OWN = "shop/ConfigMap/workload-config"
ACCESS_VOLUME = {
    "name": "kube-api-access-x1",
    "projected": {
        "sources": [
            {"serviceAccountToken": {"path": "token"}},
            {"configMap": {"name": "kube-root-ca.crt", "items": [{"key": "ca.crt"}]}},
            {"downwardAPI": {"items": []}},
        ]
    },
}


def pod_spec(*, access: bool = True, own: bool = True) -> dict[str, Any]:
    container: dict[str, Any] = {"name": "app", "image": "app:1"}
    if own:
        container["envFrom"] = [{"configMapRef": {"name": "workload-config"}}]
    return {"containers": [container], **({"volumes": [ACCESS_VOLUME]} if access else {})}


def world(
    *,
    template_mounts_ca: bool = False,
    owned: bool = True,
    ca_versions: tuple[ObjectVersion, ...] = (),
) -> tuple[Context, dict[Any, list[ObjectVersion]]]:
    template = pod_spec(access=False)
    if template_mounts_ca:
        template["volumes"] = [{"name": "ca", "configMap": {"name": "kube-root-ca.crt"}}]
    versions = [
        version("shop/Deployment/pay", 0, {"spec": {"template": {"spec": template}}}),
        version(
            "shop/ReplicaSet/pay-1",
            0,
            {
                "metadata": {"ownerReferences": [{"kind": "Deployment", "name": "pay"}]},
                "spec": {"template": {"spec": template}},
            },
        ),
        version(
            "shop/Pod/pay-1-a",
            0,
            {
                "metadata": {"ownerReferences": [{"kind": "ReplicaSet", "name": "pay-1"}]}
                if owned
                else {},
                "spec": pod_spec(),
            },
        ),
        version(OWN, 0, {"data": {"mode": "a"}}),
        *(ca_versions or (version(CA, 0, {"data": {"ca.crt": "one"}}),)),
    ]
    history: dict[Any, list[ObjectVersion]] = {}
    for item in versions:
        history.setdefault(item.entity, []).append(item)
    latest = {entity: items[-1] for entity, items in history.items()}
    symptoms = Symptoms(
        onset=ONSET, last_seen=None, services=("pay",), namespaces=("shop",), alert_names=("A",)
    )
    pod = ref("shop/Pod/pay-1-a")
    entities = {ref("shop/Deployment/pay"), pod} if not owned else {ref("shop/Deployment/pay")}
    context = Context(
        symptoms=symptoms,
        symptom_entities=entities,
        topology=Topology(derive_edges(latest), latest),
    )
    return context, history


def roles(context: Context, history: dict[Any, list[ObjectVersion]]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for item in derive_structural_frontier(context, history=history, onset=ONSET):
        result.setdefault(item.actor.canonical, set()).add(item.role)
    return result


def test_only_the_service_account_projection_is_api_access() -> None:
    assert api_access_refs(pod_spec()) == {("ConfigMap", "kube-root-ca.crt")}
    # the same ConfigMap also reached another way is the workload's own configuration
    mounted = {
        **pod_spec(),
        "volumes": [ACCESS_VOLUME, {"configMap": {"name": "kube-root-ca.crt"}}],
    }
    assert api_access_refs(mounted) == set()
    without_token = {"projected": {"sources": [{"configMap": {"name": "kube-root-ca.crt"}}]}}
    assert api_access_refs({**pod_spec(), "volumes": [without_token]}) == set()


def test_the_admission_volume_is_api_access_and_the_declared_config_stays_a_source() -> None:
    found = roles(*world())
    assert found[CA] == {"api_access"}
    assert found[OWN] == {"configuration_source"}


def test_a_template_that_declares_it_keeps_it_a_configuration_source() -> None:
    assert roles(*world(template_mounts_ca=True))[CA] == {"configuration_source"}


def test_a_pod_without_an_owner_keeps_it_a_configuration_source() -> None:
    assert roles(*world(owned=False))[CA] == {"configuration_source"}


def test_an_observed_content_change_before_the_onset_makes_it_material_again() -> None:
    changed = (
        version(CA, 0, {"data": {"ca.crt": "one"}}),
        version(CA, 100, {"data": {"ca.crt": "two"}}, 1),
    )
    assert roles(*world(ca_versions=changed))[CA] == {"configuration_source"}
    after = (
        version(CA, 0, {"data": {"ca.crt": "one"}}),
        version(CA, 130, {"data": {"ca.crt": "two"}}, 1),
    )
    assert roles(*world(ca_versions=after))[CA] == {"api_access"}


# --- §26 B: unchanged configuration --------------------------------------------------------------------------

START = at(0)
LISTED = at(110)
FOLLOWED = at(100)


def coverage(
    *,
    followed: Any = FOLLOWED,
    gaps: tuple[StreamGap, ...] = (),
    transport: TransportCompleteness = TransportCompleteness.PROVEN,
) -> EvidenceCoverage:
    return EvidenceCoverage(
        starts_at=START,
        window_end=at(125),
        stream_followed_since=followed,
        scopes=(
            ScopeCoverage(
                namespace="shop",
                kind="ConfigMap",
                source_continuity=SourceContinuity.UNKNOWN,
                gaps=gaps,
                transport_completeness=transport,
            ),
        ),
    )


def listing(written: Any = "2024-12-31T10:00:00Z", **extra: Any) -> ObjectVersion:
    metadata: dict[str, Any] = {"creationTimestamp": "2024-12-30T10:00:00Z", **extra}
    if written is not None:
        metadata["managedFields"] = [{"manager": "kubectl", "operation": "Update", "time": written}]
    item = version(
        OWN, 110, {"metadata": metadata, "data": {"mode": "a"}}, lifecycle=Lifecycle.OBSERVED
    )
    return item


def question() -> StructuralAlternative:
    return StructuralAlternative(
        alternative_id="alternative:own",
        actor=ref(OWN),
        role="configuration_source",
        material_for_hypothesis_ids=("h1",),
    )


def answer(*versions: ObjectVersion, record: EvidenceCoverage | None = None) -> Any:
    return unchanged_configuration(
        question(), {ref(OWN): list(versions)}, record or coverage(), ONSET
    )


def test_written_before_the_window_and_observed_unchanged_is_answered() -> None:
    result = answer(listing())
    assert result.state == "ANSWERED_NO_CHANGE_IN_WINDOW"
    assert result.rule_id == "m21.frontier.unchanged-configuration"
    assert result.evidence_ids == (listing().evidence_id,)
    assert result.listed_at == LISTED
    assert result.last_written_at is not None and result.last_written_at < START


def test_every_failed_condition_leaves_it_open_and_named() -> None:
    assert answer(listing(written=None)).blocked_reason == "NO_WRITE_RECORD"
    assert answer(listing(written="2025-01-01T12:30:00Z")).blocked_reason == "WRITTEN_IN_WINDOW"
    gap = StreamGap(reason="RESYNC", since=at(115) - timedelta(seconds=1), at=at(116))
    assert answer(listing(), record=coverage(gaps=(gap,))).blocked_reason == "SCOPE_NOT_CONTINUOUS"
    assert answer(listing(), record=coverage(followed=at(111))).blocked_reason == (
        "SCOPE_NOT_CONTINUOUS"
    )
    not_proven = coverage(transport=TransportCompleteness.NOT_PROVEN)
    assert answer(listing(), record=not_proven).blocked_reason == "TRANSPORT_NOT_PROVEN"
    deleted = listing().model_copy(update={"lifecycle": Lifecycle.DELETED})
    assert answer(deleted).blocked_reason == "WRITTEN_IN_WINDOW"
    assert answer().blocked_reason == "NO_LISTING"
    for result in (answer(listing(written=None)), answer()):
        assert result.state == "OPEN"


def test_a_gap_outside_the_listing_interval_does_not_matter() -> None:
    earlier = StreamGap(reason="RESYNC", since=at(50), at=at(60))
    assert answer(listing(), record=coverage(gaps=(earlier,))).state == (
        "ANSWERED_NO_CHANGE_IN_WINDOW"
    )


def test_without_a_coverage_record_the_rule_does_not_apply() -> None:
    assert unchanged_configuration(question(), {ref(OWN): [listing()]}, None, ONSET) is None
    other = question().model_copy(update={"role": "dependency"})
    assert unchanged_configuration(other, {ref(OWN): [listing()]}, coverage(), ONSET) is None


def test_the_connector_keeps_only_the_write_times() -> None:
    metadata: dict[str, Any] = {
        "managedFields": [
            {"manager": "kubectl", "operation": "Update", "time": "t1", "fieldsV1": {"f:data": {}}}
        ]
    }
    keep_write_times(metadata)
    assert metadata == {
        "managedFields": [{"manager": "kubectl", "operation": "Update", "time": "t1"}]
    }
    empty: dict[str, Any] = {}
    keep_write_times(empty)
    assert empty == {}

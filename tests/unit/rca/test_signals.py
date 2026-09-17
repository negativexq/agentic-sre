"""Signal extraction and topology on small scenarios."""

from __future__ import annotations

from rca_builders import (
    alert,
    config_change_source,
    event,
    ref,
    shop_objects,
    version,
)

from packages.rca.model import FindingKind
from packages.rca.signals import (
    change_findings,
    extract_symptoms,
    failure_findings,
    fault_event_findings,
    symptom_entities,
)
from packages.rca.source import InMemorySource
from packages.rca.topology import Topology, derive_edges, pod_workload_name


def _topology(source: InMemorySource) -> Topology:
    history = source.object_history()
    latest = {entity: versions[-1] for entity, versions in history.items()}
    return Topology(derive_edges(latest, list(source.events())), latest)


def test_background_alerts_do_not_define_symptoms() -> None:
    symptoms = extract_symptoms(
        [alert("Watchdog", "prometheus", 0), alert("RequestErrorRate", "checkout", 5)]
    )
    assert symptoms.services == ("checkout",)
    assert symptoms.alert_names == ("RequestErrorRate",)
    assert symptoms.background_alert_counts == {"Watchdog": 1}
    assert symptoms.onset is not None and symptoms.onset.minute == 5


def test_topology_links_config_workload_pod_and_service() -> None:
    topology = _topology(config_change_source())
    config = ref("shop/ConfigMap/checkout-flags")
    pod = ref("shop/Pod/checkout-5d8f7c9b4-abcde")
    assert topology.incoming(config, "uses_config") == (ref("shop/Deployment/checkout"),)
    assert topology.workload_of(pod) == ref("shop/Deployment/checkout")
    assert ref("shop/Service/checkout") in topology.incoming(pod, "selects")
    assert topology.distance(config, {ref("shop/Service/checkout")}) == 4


def test_alert_service_maps_to_workload_service_and_pod() -> None:
    source = config_change_source()
    entities = symptom_entities(list(source.alerts()), _topology(source))
    assert ref("shop/Deployment/checkout") in entities
    assert ref("shop/Service/checkout") in entities
    assert ref("shop/Pod/checkout-5d8f7c9b4-abcde") in entities


def test_config_change_reports_changed_keys_and_json_paths() -> None:
    findings = change_findings(config_change_source().object_history())
    config = next(f for f in findings if f.entity == ref("shop/ConfigMap/checkout-flags"))
    assert config.kind is FindingKind.CONFIG_CHANGE
    assert config.details["changed_keys"] == ["flags.json"]
    assert config.details["excerpts"]["flags.json"]["changed_paths"] == ".checkoutFailure"


def test_restart_image_and_scale_changes_are_classified() -> None:
    from rca_builders import deployment

    base = deployment("checkout")
    restarted = deployment("checkout", restarted="2025-01-01T12:10:00Z")
    new_image = deployment("checkout", image="app:2")
    scaled = deployment("checkout")
    scaled["spec"]["replicas"] = 0
    history = {
        ref("shop/Deployment/a"): [
            version("shop/Deployment/a", 0, deployment("checkout")),
            version("shop/Deployment/a", 5, restarted, 1),
        ],
        ref("shop/Deployment/b"): [
            version("shop/Deployment/b", 0, base),
            version("shop/Deployment/b", 5, new_image, 1),
        ],
        ref("shop/Deployment/c"): [
            version("shop/Deployment/c", 0, deployment("checkout")),
            version("shop/Deployment/c", 5, scaled, 1),
        ],
    }
    kinds = {f.entity.name: f.kind for f in change_findings(history)}
    assert kinds == {
        "a": FindingKind.ROLLOUT_RESTART,
        "b": FindingKind.IMAGE_CHANGE,
        "c": FindingKind.SCALE_CHANGE,
    }


def test_derived_and_unchanged_objects_produce_no_change_findings() -> None:
    history = {
        ref("shop/Pod/p"): [
            version("shop/Pod/p", 0, {"spec": {"a": 1}}),
            version("shop/Pod/p", 5, {"spec": {"a": 2}}, 1),
        ],
        ref("shop/Service/s"): [version("shop/Service/s", 0, {"spec": {"a": 1}})],
    }
    assert change_findings(history) == []


def test_chaos_events_become_one_finding_per_experiment_with_targets() -> None:
    events = [
        event("chaos/Schedule/checkout-delay", "Spawned", 1),
        event(
            "chaos/NetworkChaos/checkout-delay-x1y2z",
            "Applied",
            2,
            message="Successfully apply chaos for shop/checkout-5d8f7c9b4-abcde",
        ),
        event("chaos/NetworkChaos/checkout-delay-x1y2z", "Recovered", 3),
    ]
    from packages.rca.source import InMemorySource

    source = InMemorySource(name="chaos", versions=shop_objects(0), event_items=events)
    topology = _topology(source)
    findings = {f.entity.kind: f for f in fault_event_findings(events, topology)}
    injection = findings["NetworkChaos"]
    assert injection.kind is FindingKind.FAULT_INJECTION
    assert injection.related == (ref("shop/Pod/checkout-5d8f7c9b4-abcde"),)
    assert findings["Schedule"].kind is FindingKind.FAULT_SCHEDULE
    assert ref("chaos/NetworkChaos/checkout-delay-x1y2z") in topology.outgoing(
        ref("chaos/Schedule/checkout-delay"), "spawns"
    )


def test_warning_events_are_grouped_and_volume_noise_is_ignored() -> None:
    events = [
        event("shop/Pod/p", "BackOff", 1, type_="Warning", count=3),
        event("shop/Pod/p", "BackOff", 2, type_="Warning", count=2),
        event("_cluster/PersistentVolume/pv", "VolumeFailedDelete", 2, type_="Warning"),
        event("shop/Pod/p", "Pulled", 2),
    ]
    findings = failure_findings(events)
    assert len(findings) == 1
    assert findings[0].details == {"reason": "BackOff", "count": 5}


def test_pod_workload_name_strips_generated_suffixes() -> None:
    assert pod_workload_name("product-catalog-7c7f8b68dc-p564n") == "product-catalog"
    assert pod_workload_name("valkey-cart-0") == "valkey-cart"
    assert pod_workload_name("standalone") == "standalone"

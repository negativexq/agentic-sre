"""Signal extraction and topology on small scenarios."""

from __future__ import annotations

from rca_builders import (
    alert,
    at,
    config_change_source,
    event,
    ref,
    shop_objects,
    version,
)

from packages.rca.model import FindingKind, ResourcePressure
from packages.rca.signals import (
    change_findings,
    extract_symptoms,
    failure_findings,
    fault_event_findings,
    parse_quantity,
    resource_findings,
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
    assert topology.distance(config, {ref("shop/Service/checkout")}) == 2
    assert ref("shop/Deployment/checkout") in topology.outgoing(
        ref("shop/Service/checkout"), "routes_to"
    )


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


def test_env_endpoints_become_call_edges_with_resolved_references() -> None:
    from rca_builders import microservice

    source = InMemorySource(
        name="calls",
        versions=[
            *microservice(
                "cart",
                0,
                {
                    "COLLECTOR": "collector",
                    "OTEL_ENDPOINT": "http://$(COLLECTOR):4317",
                    "STORE_ADDR": "store:6379",
                    "BIND_ADDR": ":8080",
                    "GREETING": "hello",
                },
            ),
            *microservice("store", 0),
            *microservice("collector", 0),
        ],
    )
    topology = _topology(source)
    assert set(topology.outgoing(ref("shop/Deployment/cart"), "calls")) == {
        ref("shop/Service/store"),
        ref("shop/Service/collector"),
    }


def test_connection_errors_blame_the_only_specific_dependency() -> None:
    from rca_builders import microservice

    from packages.rca.model import LogRecord
    from packages.rca.signals import dependency_findings

    callers = [
        microservice(name, 0, {"STORE_ADDR": f"{name}-db:1", "OTEL_ADDR": "collector:4317"})
        for name in ("cart", "shipping", "quote")
    ]
    source = InMemorySource(
        name="deps",
        versions=[
            *[item for group in callers for item in group],
            *microservice("cart-db", 0),
            *microservice("shipping-db", 0),
            *microservice("quote-db", 0),
            *microservice("collector", 0),
        ],
    )
    topology = _topology(source)
    logs = [
        LogRecord(
            service="cart",
            at=None,
            severity="ERROR",
            message="Wasn't able to connect to redis",
            evidence_id="log:1",
        )
    ]
    findings = dependency_findings(logs, topology, {ref("shop/Deployment/cart")})
    assert [f.entity for f in findings] == [ref("shop/Pod/cart-db-5d8f7c9b4-abcde")]
    assert findings[0].related == (ref("shop/Service/cart-db"),)


def test_connection_errors_with_several_dependencies_need_a_named_target() -> None:
    from rca_builders import microservice

    from packages.rca.model import LogRecord
    from packages.rca.signals import dependency_findings

    source = InMemorySource(
        name="deps",
        versions=[
            *microservice("checkout", 0, {"PAY_ADDR": "payment:1", "MQ_ADDR": "kafka:9092"}),
            *microservice("payment", 0),
            *microservice("kafka", 0),
        ],
    )
    topology = _topology(source)

    def log(message: str) -> LogRecord:
        return LogRecord(
            service="checkout", at=None, severity="ERROR", message=message, evidence_id="l"
        )

    alerting = {ref("shop/Deployment/checkout")}
    assert dependency_findings([log("connection reset")], topology, alerting) == []
    named = dependency_findings([log("kafka: connection refused")], topology, alerting)
    assert [f.related for f in named] == [(ref("shop/Service/kafka"),)]


def test_env_from_configmap_values_become_call_edges() -> None:
    from rca_builders import microservice

    order = microservice("order", 0)
    order[0].body["spec"]["template"]["spec"]["containers"][0]["envFrom"] = [
        {"configMapRef": {"name": "shared"}}
    ]
    source = InMemorySource(
        name="env-from",
        versions=[
            *order,
            *microservice("payment", 0),
            version("shop/ConfigMap/shared", 0, {"data": {"PAYMENT_URL": "http://payment:8000"}}),
        ],
    )
    topology = _topology(source)
    assert topology.outgoing(ref("shop/Deployment/order"), "calls") == (
        ref("shop/Service/payment"),
    )


def test_added_env_var_is_reported_with_its_values() -> None:
    from rca_builders import deployment

    before = deployment("payment")
    after = deployment("payment")
    after["spec"]["template"]["spec"]["containers"][0]["env"] = [
        {"name": "FAULT_DELAY_MS", "value": "2500"}
    ]
    history = {
        ref("shop/Deployment/payment"): [
            version("shop/Deployment/payment", 0, before),
            version("shop/Deployment/payment", 5, after, 1),
        ]
    }
    finding = change_findings(history)[0]
    assert finding.kind is FindingKind.SPEC_CHANGE
    assert finding.summary == "spec changed: [payment].env[FAULT_DELAY_MS].value: unset -> 2500"


def test_scheduled_fault_reports_its_start_and_schedule_span() -> None:
    target = "Successfully apply chaos for shop/checkout-5d8f7c9b4-abcde"
    events = [
        event("chaos/Schedule/delay", "Spawned", 1),
        event("chaos/NetworkChaos/delay-early", "Applied", 1, message=target),
        event("chaos/NetworkChaos/delay-early", "Recovered", 2),
        event("chaos/NetworkChaos/delay-late", "Applied", 50, message=target),
        event("chaos/NetworkChaos/delay-failed", "Started", 30),
        event("chaos/NetworkChaos/delay-failed", "Failed", 31, type_="Warning"),
    ]
    source = InMemorySource(name="chaos", versions=shop_objects(0), event_items=events)
    topology = _topology(source)
    findings = {f.entity.name: f for f in fault_event_findings(events, topology)}
    assert "delay-failed" not in findings
    early = findings["delay-early"]
    assert early.at is not None and early.at.minute == 1
    assert "from 12:01 to 12:50" in early.summary
    assert findings["delay"].summary.startswith("chaos schedule injecting faults since 12:01")


def test_collapse_keeps_the_latest_experiment_started_before_onset() -> None:
    from rca_builders import at

    from packages.rca.model import Candidate, Finding
    from packages.rca.ranking import collapse_fault_instances

    events = [
        event("chaos/Schedule/delay", "Spawned", 0),
        *(
            event(f"chaos/NetworkChaos/delay-{name}", "Applied", minute)
            for name, minute in (("a", 1), ("b", 5), ("c", 20))
        ),
    ]
    source = InMemorySource(name="chaos", versions=shop_objects(0), event_items=events)
    topology = _topology(source)

    def candidate(name: str, minute: int, score: float) -> Candidate:
        entity = ref(f"chaos/NetworkChaos/delay-{name}")
        finding = Finding(
            kind=FindingKind.FAULT_INJECTION, entity=entity, at=at(minute), summary=""
        )
        return Candidate(entity=entity, score=score, findings=(finding,))

    ranked = [candidate("c", 20, 9.0), candidate("b", 5, 8.0), candidate("a", 1, 8.0)]
    kept = collapse_fault_instances(ranked, topology, onset=at(10))
    assert [(c.entity.name, c.score) for c in kept] == [("delay-b", 9.0)]
    early = collapse_fault_instances(ranked, topology, onset=at(0))
    assert [c.entity.name for c in early] == ["delay-a"]


def test_parse_quantity_handles_binary_and_decimal_suffixes() -> None:
    assert parse_quantity("1Gi") == 2**30
    assert parse_quantity("250m") == 0.25
    assert parse_quantity("2") == 2.0
    assert parse_quantity("x") is None


def _pressure(resource: str, baseline: float | None, peak: float) -> ResourcePressure:
    return ResourcePressure(
        pod=ref("shop/Pod/checkout-5d8f7c9b4-abcde"),
        container="checkout",
        resource=resource,
        baseline=baseline,
        peak=peak,
        at=at(4),
        evidence_id=f"metrics:{resource}",
    )


def test_only_new_resource_pressure_becomes_a_finding() -> None:
    findings = resource_findings(
        [
            _pressure("memory", 0.5, 0.96),
            _pressure("cpu", 0.4, 0.5),  # throttled before the incident too
            _pressure("cpu", None, 0.9),  # nothing to compare with
            _pressure("cpu", 0.0, 0.1),  # below threshold
        ]
    )
    assert len(findings) == 1
    assert findings[0].kind is FindingKind.RESOURCE_PRESSURE
    assert findings[0].summary == "checkout memory use of limit rose from 50% to 96%"

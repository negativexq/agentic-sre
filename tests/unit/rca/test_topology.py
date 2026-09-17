"""Graph algorithms on top of structural edges and directional causal paths."""

from __future__ import annotations

from rca_builders import ref

from packages.rca.model import Edge
from packages.rca.topology import FAN_OUT_THRESHOLD, RELATION_SEMANTICS, Topology


def _topology(edges: list[Edge]) -> Topology:
    return Topology(edges, {})


def test_shared_configmap_does_not_bridge_two_sibling_workloads() -> None:
    """A shared resource is a cause, not a causal path between its consumers."""
    config = ref("shop/ConfigMap/flags")
    edges = [
        Edge(source=ref(f"shop/Deployment/app{i}"), target=config, relation="uses_config")
        for i in range(2)
    ]
    topology = _topology(edges)
    targets = {ref("shop/Deployment/app1")}
    assert topology.distance(ref("shop/Deployment/app0"), targets) == 2
    assert topology.causal_distance(ref("shop/Deployment/app0"), targets) is None
    assert topology.causal_distance(config, targets) == 1


def test_causal_distance_does_not_cross_a_shared_configmap_hub() -> None:
    """Many workloads sharing one ConfigMap are not linked to each other."""
    config = ref("shop/ConfigMap/flags")
    edges = [
        Edge(source=ref(f"shop/Deployment/app{i}"), target=config, relation="uses_config")
        for i in range(FAN_OUT_THRESHOLD + 2)
    ]
    topology = _topology(edges)
    targets = {ref("shop/Deployment/app1")}
    # The undirected graph still sees them two hops apart through the ConfigMap...
    assert topology.distance(ref("shop/Deployment/app0"), targets) == 2
    # ...but causal_distance refuses to treat sharing a config as a causal link.
    assert topology.causal_distance(ref("shop/Deployment/app0"), targets) is None
    # The ConfigMap itself is the valid cause, reached in the stored reverse direction.
    assert topology.causal_distance(config, {ref("shop/Deployment/app0")}) == 1


def test_causal_distance_does_not_cross_a_shared_network_policy_hub() -> None:
    """Many pods restricted by the same NetworkPolicy are not linked to each other."""
    policy = ref("shop/NetworkPolicy/deny-all")
    edges = [
        Edge(source=policy, target=ref(f"shop/Pod/pod-{i}"), relation="restricts")
        for i in range(FAN_OUT_THRESHOLD + 2)
    ]
    topology = _topology(edges)
    targets = {ref("shop/Pod/pod-1")}
    assert topology.distance(ref("shop/Pod/pod-0"), targets) == 2
    assert topology.causal_distance(ref("shop/Pod/pod-0"), targets) is None
    assert topology.causal_distance(policy, {ref("shop/Pod/pod-0")}) == 1


def test_causal_distance_is_unaffected_by_non_fan_out_relations() -> None:
    """Ownership chains (not restricted/config relations) are never cut."""
    edges = [
        Edge(
            source=ref("shop/Pod/checkout-abc"),
            target=ref("shop/ReplicaSet/checkout-rs"),
            relation="owned_by",
        ),
        Edge(
            source=ref("shop/ReplicaSet/checkout-rs"),
            target=ref("shop/Deployment/checkout"),
            relation="owned_by",
        ),
        Edge(
            source=ref("shop/Service/checkout"),
            target=ref("shop/Pod/checkout-abc"),
            relation="selects",
        ),
    ]
    topology = _topology(edges)
    targets = {ref("shop/Service/checkout")}
    assert topology.distance(ref("shop/Deployment/checkout"), targets) == 3
    assert topology.causal_distance(ref("shop/Deployment/checkout"), targets) == 3


def test_causal_path_explains_owner_to_selected_service_direction() -> None:
    deployment = ref("shop/Deployment/checkout")
    replicaset = ref("shop/ReplicaSet/checkout-rs")
    pod = ref("shop/Pod/checkout-abc")
    service = ref("shop/Service/checkout")
    topology = _topology(
        [
            Edge(source=pod, target=replicaset, relation="owned_by"),
            Edge(source=replicaset, target=deployment, relation="owned_by"),
            Edge(source=service, target=pod, relation="selects"),
        ]
    )
    path = topology.causal_path(deployment, {service})
    assert path is not None
    assert [(hop.source, hop.relation, hop.target, hop.direction) for hop in path] == [
        (deployment, "owns", replicaset, "reverse"),
        (replicaset, "owns", pod, "reverse"),
        (pod, "backs", service, "reverse"),
    ]


def test_relation_semantics_make_structural_and_causal_direction_explicit() -> None:
    assert RELATION_SEMANTICS["owned_by"].backward is True
    assert RELATION_SEMANTICS["owned_by"].forward is False
    assert RELATION_SEMANTICS["calls"].forward is False
    assert RELATION_SEMANTICS["calls"].backward is True
    assert RELATION_SEMANTICS["uses_config"].forward is False


def test_unknown_relations_are_structural_but_not_causal() -> None:
    source = ref("shop/Deployment/source")
    target = ref("shop/Service/target")
    topology = _topology([Edge(source=source, target=target, relation="future_unknown_relation")])
    assert topology.distance(source, {target}) == 1
    assert topology.causal_distance(source, {target}) is None


def test_backend_failure_propagates_to_caller_not_the_reverse() -> None:
    caller = ref("shop/Deployment/checkout")
    backend = ref("shop/Service/payment")
    topology = _topology([Edge(source=caller, target=backend, relation="calls")])
    assert topology.causal_distance(backend, {caller}) == 1
    assert topology.causal_distance(caller, {backend}) is None


def test_network_policy_is_causal_cause_but_does_not_bridge_two_pods() -> None:
    policy = ref("shop/NetworkPolicy/deny-all")
    pod_a = ref("shop/Pod/a")
    pod_b = ref("shop/Pod/b")
    topology = _topology(
        [
            Edge(source=policy, target=pod_a, relation="restricts"),
            Edge(source=policy, target=pod_b, relation="restricts"),
        ]
    )
    assert topology.causal_distance(policy, {pod_a}) == 1
    assert topology.causal_distance(pod_a, {pod_b}) is None

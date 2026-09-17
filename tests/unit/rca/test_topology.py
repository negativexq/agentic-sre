"""Graph algorithms on top of derived edges: distance, causal_distance, fan-out."""

from __future__ import annotations

from rca_builders import ref

from packages.rca.model import Edge
from packages.rca.topology import FAN_OUT_THRESHOLD, RELATION_SEMANTICS, Topology


def _topology(edges: list[Edge]) -> Topology:
    return Topology(edges, {})


def test_causal_distance_matches_distance_below_the_fan_out_threshold() -> None:
    """A ConfigMap used by a few workloads still links them causally."""
    config = ref("shop/ConfigMap/flags")
    edges = [
        Edge(source=ref(f"shop/Deployment/app{i}"), target=config, relation="uses_config")
        for i in range(FAN_OUT_THRESHOLD)
    ]
    topology = _topology(edges)
    targets = {ref("shop/Deployment/app1")}
    assert topology.distance(ref("shop/Deployment/app0"), targets) == 2
    assert topology.causal_distance(ref("shop/Deployment/app0"), targets) == 2


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
    # The ConfigMap itself is still reachable either way (it is the actual finding).
    assert topology.causal_distance(ref("shop/Deployment/app0"), {config}) == 1


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
    assert [(hop.source, hop.relation, hop.target) for hop in path] == [
        (deployment, "owned_by", replicaset),
        (replicaset, "owned_by", pod),
        (pod, "selects", service),
    ]


def test_relation_semantics_make_structural_and_causal_direction_explicit() -> None:
    assert RELATION_SEMANTICS["owned_by"].backward is True
    assert RELATION_SEMANTICS["calls"].forward is True
    assert RELATION_SEMANTICS["calls"].backward is True
    assert RELATION_SEMANTICS["uses_config"].fan_out is True

"""Graph algorithms on top of derived edges: distance, causal_distance, fan-out."""

from __future__ import annotations

from rca_builders import ref

from packages.rca.model import Edge
from packages.rca.topology import FAN_OUT_THRESHOLD, Topology


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

from __future__ import annotations

from packages.evals.itbench.contracts import ITBenchGroundTruthGroup
from packages.evals.itbench.qualification import qualify_group
from packages.rca.model import Edge, EntityRef
from packages.rca.topology import Topology


def _topology() -> tuple[EntityRef, set[EntityRef], Topology]:
    deployment = EntityRef(kind="Deployment", name="product-catalog", namespace="shop")
    pod = EntityRef(kind="Pod", name="product-catalog-abc", namespace="shop")
    rs = EntityRef(kind="ReplicaSet", name="product-catalog-rs", namespace="shop")
    topology = Topology(
        [
            Edge(source=pod, target=rs, relation="owned_by"),
            Edge(source=rs, target=deployment, relation="owned_by"),
        ],
        {},
    )
    return deployment, {deployment, pod, rs}, topology


def test_qualification_reports_exact_and_relation_backed_entities() -> None:
    deployment, entities, topology = _topology()
    group = ITBenchGroundTruthGroup(
        group_id="root",
        kind="Deployment",
        namespace="shop",
        filters=(r"product-catalog-.*",),
        root_cause=True,
    )

    record = qualify_group(group, entities, topology)

    assert record["compiled_successfully"] is True
    assert record["exact_matches"] == []
    assert record["related_entities"]
    assert record["related_entities"][0]["entity"] == deployment.canonical
    assert deployment.canonical not in record["exact_matches"]


def test_qualification_reports_invalid_filter_without_fallback_match() -> None:
    _deployment, entities, topology = _topology()
    group = ITBenchGroundTruthGroup(
        group_id="root",
        kind="HorizontalPodAutoscaler",
        filters=("*.*",),
        root_cause=True,
    )

    record = qualify_group(group, entities, topology)

    assert record["compiled_successfully"] is False
    assert record["invalid_filters"] == ["*.*"]
    assert record["exact_matches"] == []


def test_qualification_can_report_parent_child_equivalence_when_parent_is_exact() -> None:
    deployment, entities, topology = _topology()
    group = ITBenchGroundTruthGroup(
        group_id="root",
        kind="Deployment",
        name=deployment.name,
        namespace=deployment.namespace,
        root_cause=True,
    )

    # Qualification itself reports only direct structural equivalence; the
    # production grader and RCA contracts remain unchanged.
    record = qualify_group(group, entities, topology)

    assert record["exact_matches"] == [deployment.canonical]
    assert {item["entity"] for item in record["related_entities"]} == {
        "shop/Pod/product-catalog-abc",
        "shop/ReplicaSet/product-catalog-rs",
    }

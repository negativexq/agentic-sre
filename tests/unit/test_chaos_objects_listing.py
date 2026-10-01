"""Chaos experiment objects are listed in every journaled namespace (chaos-objects-design.md §2)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from packages.rca.live import KubernetesClusterReader, ListingScope


class Missing(Exception):
    status = 404


class FakeCustomObjects:
    def __init__(self, absent: set[str]) -> None:
        self.absent = absent
        self.calls: list[tuple[str, str]] = []

    def list_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, **_: Any
    ) -> dict[str, Any]:
        self.calls.append((namespace, plural))
        if namespace in self.absent:
            raise Missing("the server could not find the requested resource")
        items = []
        if (namespace, plural) == ("shop", "networkchaos"):
            items = [
                {"metadata": {"name": "delay", "namespace": "shop"}, "spec": {"action": "delay"}}
            ]
        return {"metadata": {"resourceVersion": "42"}, "items": items}


class EmptyList:
    def __getattr__(self, _name: str) -> Any:
        return lambda *_args, **_kwargs: SimpleNamespace(
            items=[], metadata=SimpleNamespace(resource_version="42")
        )


def reader(absent: set[str]) -> tuple[KubernetesClusterReader, FakeCustomObjects]:
    custom = FakeCustomObjects(absent)
    client = SimpleNamespace(
        CustomObjectsApi=lambda: custom,
        CoreV1Api=EmptyList,
        AppsV1Api=EmptyList,
        AutoscalingV2Api=EmptyList,
        AutoscalingV1Api=EmptyList,
        NetworkingV1Api=EmptyList,
        BatchV1Api=EmptyList,
        DiscoveryV1Api=EmptyList,
        PolicyV1Api=EmptyList,
    )
    kubernetes_reader = KubernetesClusterReader(chaos_namespaces=("chaos-mesh",))
    kubernetes_reader._module = SimpleNamespace(client=client)
    kubernetes_reader._api_client = SimpleNamespace(sanitize_for_serialization=lambda item: item)
    return kubernetes_reader, custom


def test_an_experiment_created_beside_its_target_is_listed_and_watchable() -> None:
    kubernetes_reader, _ = reader(absent=set())
    listing = kubernetes_reader.list_objects(["shop"])
    (experiment,) = [o for o in listing.objects if o.get("kind") == "NetworkChaos"]
    assert experiment["spec"]["action"] == "delay"
    assert ListingScope("shop", "NetworkChaos") in listing.completed_scopes
    assert listing.resource_versions[ListingScope("shop", "NetworkChaos")] == "42"
    assert ListingScope("chaos-mesh", "NetworkChaos") in listing.completed_scopes  # as before


def test_a_namespace_without_chaos_mesh_is_not_a_failed_scope() -> None:
    kubernetes_reader, _ = reader(absent={"shop"})
    listing = kubernetes_reader.list_objects(["shop"])
    assert not [f for f in listing.failed_scopes if f.scope.namespace == "shop"]
    assert ListingScope("shop", "NetworkChaos") not in listing.completed_scopes

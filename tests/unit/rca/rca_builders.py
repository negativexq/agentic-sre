"""Builders for small, readable RCA scenarios."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from packages.rca.model import Alert, ClusterEvent, EntityRef, ObjectVersion
from packages.rca.source import InMemorySource

T0 = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def ref(value: str) -> EntityRef:
    return EntityRef.parse(value)


def version(value: str, minutes: float, body: dict[str, Any], index: int = 0) -> ObjectVersion:
    entity = ref(value)
    metadata: dict[str, Any] = {"name": entity.name, **body.pop("metadata", {})}
    if entity.namespace != "_cluster":
        metadata["namespace"] = entity.namespace
    return ObjectVersion(
        entity=entity,
        observed_at=at(minutes),
        body={"kind": entity.kind, "metadata": metadata, **body},
        evidence_id=f"obj:{value}:{minutes}:{index}",
    )


def deployment(
    name: str, *, config: str | None = None, image: str = "app:1", restarted: str | None = None
) -> dict[str, Any]:
    annotations = {"kubectl.kubernetes.io/restartedAt": restarted} if restarted else {}
    container: dict[str, Any] = {"name": name, "image": image}
    if config:
        container["envFrom"] = [{"configMapRef": {"name": config}}]
    return {
        "metadata": {"labels": {"app.kubernetes.io/name": name}},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": {"app": name}, "annotations": annotations},
                "spec": {"containers": [container]},
            },
        },
    }


def pod(name: str, app: str, owner: str) -> dict[str, Any]:
    return {
        "metadata": {
            "labels": {"app": app},
            "ownerReferences": [{"kind": "ReplicaSet", "name": owner}],
        },
        "spec": {"containers": [{"name": app, "image": "app:1"}]},
    }


def replicaset(name: str, deployment_name: str) -> dict[str, Any]:
    return {
        "metadata": {"ownerReferences": [{"kind": "Deployment", "name": deployment_name}]},
        "spec": {"replicas": 1},
    }


def service(app: str) -> dict[str, Any]:
    return {"spec": {"selector": {"app": app}}}


def alert(name: str, service_name: str, minutes: float, namespace: str = "shop") -> Alert:
    return Alert(
        name=name,
        service=service_name,
        namespace=namespace,
        starts_at=at(minutes),
        labels={"alertname": name, "service_name": service_name, "namespace": namespace},
    )


def event(
    value: str,
    reason: str,
    minutes: float,
    *,
    type_: str = "Normal",
    message: str = "",
    count: int = 1,
) -> ClusterEvent:
    return ClusterEvent(
        entity=ref(value),
        reason=reason,
        type=type_,
        message=message,
        first_at=at(minutes),
        last_at=at(minutes),
        count=count,
        evidence_id=f"evt:{value}:{reason}:{minutes}",
    )


def shop_objects(minutes: float) -> list[ObjectVersion]:
    """A checkout service with its ReplicaSet, pod, and Service."""
    return [
        version(
            "shop/Deployment/checkout", minutes, deployment("checkout", config="checkout-flags")
        ),
        version(
            "shop/ReplicaSet/checkout-5d8f7c9b4",
            minutes,
            replicaset("checkout-5d8f7c9b4", "checkout"),
        ),
        version(
            "shop/Pod/checkout-5d8f7c9b4-abcde",
            minutes,
            pod("checkout-5d8f7c9b4-abcde", "checkout", "checkout-5d8f7c9b4"),
        ),
        version("shop/Service/checkout", minutes, service("checkout")),
    ]


def config_change_source() -> InMemorySource:
    """checkout-flags flips a feature flag, then checkout starts failing."""
    versions = [
        *shop_objects(0),
        version(
            "shop/ConfigMap/checkout-flags",
            0,
            {"data": {"flags.json": '{"checkoutFailure": "off"}'}},
        ),
        version(
            "shop/ConfigMap/checkout-flags",
            10,
            {"data": {"flags.json": '{"checkoutFailure": "on"}'}},
            1,
        ),
        version("infra/ConfigMap/recorder", 0, {"data": {"a": "1"}}),
        version("infra/ConfigMap/recorder", 10, {"data": {"a": "2"}}, 1),
    ]
    return InMemorySource(
        name="config-incident",
        alert_items=[
            alert("RequestErrorRate", "checkout", 12),
            alert("Watchdog", "prometheus", 0),
        ],
        versions=versions,
        event_items=[],
    )

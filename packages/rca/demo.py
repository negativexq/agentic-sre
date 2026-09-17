"""A built-in incident for trying the product without a cluster or dataset.

A bad rollout sets a payment delay; checkout alerts on latency a few minutes
later. A noisy recorder ConfigMap in another namespace also changes, and the
checkout pod logs warnings, so the engine has to pick the real cause.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from packages.rca.model import Alert, ClusterEvent, EntityRef, LogRecord, ObjectVersion
from packages.rca.source import InMemorySource

START = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)


def _at(minutes: float) -> datetime:
    return START + timedelta(minutes=minutes)


def _version(value: str, minutes: float, body: dict[str, Any]) -> ObjectVersion:
    entity = EntityRef.parse(value)
    metadata = {"name": entity.name, "namespace": entity.namespace, **body.pop("metadata", {})}
    return ObjectVersion(
        entity=entity,
        observed_at=_at(minutes),
        body={"kind": entity.kind, "metadata": metadata, **body},
        evidence_id=f"journal:{entity.canonical}@{minutes:g}m",
    )


def _deployment(name: str, env: dict[str, str], image: str = "shop/app:1.4.2") -> dict[str, Any]:
    return {
        "metadata": {"labels": {"app": name}},
        "spec": {
            "replicas": 2,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": {"app": name}},
                "spec": {
                    "containers": [
                        {
                            "name": name,
                            "image": image,
                            "env": [{"name": k, "value": v} for k, v in env.items()],
                        }
                    ]
                },
            },
        },
    }


def _workload(name: str, minutes: float, env: dict[str, str]) -> list[ObjectVersion]:
    rs = f"{name}-6c9d8f7b5"
    return [
        _version(f"shop/Deployment/{name}", minutes, _deployment(name, env)),
        _version(
            f"shop/ReplicaSet/{rs}",
            minutes,
            {"metadata": {"ownerReferences": [{"kind": "Deployment", "name": name}]}},
        ),
        _version(
            f"shop/Pod/{rs}-k2x9p",
            minutes,
            {
                "metadata": {
                    "labels": {"app": name},
                    "ownerReferences": [{"kind": "ReplicaSet", "name": rs}],
                }
            },
        ),
        _version(f"shop/Service/{name}", minutes, {"spec": {"selector": {"app": name}}}),
    ]


def demo_source() -> InMemorySource:
    """The bad-rollout incident."""
    versions = [
        *_workload("checkout", 0, {"PAYMENT_ADDR": "payment:8080", "CART_ADDR": "cart:8080"}),
        *_workload("payment", 0, {"PAYMENT_TIMEOUT_MS": "800", "FAULT_DELAY_MS": "0"}),
        *_workload("cart", 0, {}),
        _version(
            "shop/Deployment/payment",
            7,
            _deployment("payment", {"PAYMENT_TIMEOUT_MS": "800", "FAULT_DELAY_MS": "2500"}),
        ),
        _version("observability/ConfigMap/recorder", 0, {"data": {"retention": "6h"}}),
        _version("observability/ConfigMap/recorder", 8, {"data": {"retention": "12h"}}),
    ]
    alerts = [
        Alert(
            name=name,
            service="checkout",
            namespace="shop",
            starts_at=_at(minutes),
            labels={"alertname": name, "service_name": "checkout", "namespace": "shop"},
        )
        for name, minutes in (("HighRequestLatency", 10), ("HighErrorRate", 11))
    ]
    alerts.append(Alert(name="Watchdog", starts_at=_at(0), labels={"alertname": "Watchdog"}))
    return InMemorySource(
        name="demo-bad-rollout",
        alert_items=alerts,
        versions=versions,
        event_items=[
            ClusterEvent(
                entity=EntityRef.parse("shop/Pod/checkout-6c9d8f7b5-k2x9p"),
                reason="Unhealthy",
                type="Warning",
                message="Readiness probe failed: context deadline exceeded",
                first_at=_at(10),
                last_at=_at(12),
                count=6,
                evidence_id="event:checkout-unhealthy",
            )
        ],
        error_items=[
            LogRecord(
                service="checkout",
                at=_at(10 + i / 2),
                severity="ERROR",
                message="payment request failed: context deadline exceeded",
                evidence_id=f"log:checkout:{i}",
            )
            for i in range(4)
        ],
    )


__all__ = ["demo_source"]

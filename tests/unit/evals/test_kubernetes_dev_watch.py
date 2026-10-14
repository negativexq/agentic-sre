"""Polling absence cannot authorize lifecycle creation/deletion in the DEV proof."""

from pathlib import Path

import pytest
from scripts.kubernetes_rollout_binding_dev import manifests
from scripts.rollout_resolution_dev_proof import source

from packages.evals.kubernetes_dev_watch import DevelopmentObjectWatch, apply_watch_lifecycle
from packages.rca.model import Lifecycle


def test_polling_creations_and_tombstones_have_no_watch_authority() -> None:
    records = source().versions
    result = apply_watch_lifecycle(records, [])
    assert not any(
        v.entity.kind in {"Pod", "ReplicaSet"}
        and v.lifecycle in {Lifecycle.CREATED, Lifecycle.DELETED}
        for v in result
    )


def test_exact_watch_events_provide_uid_bound_lifecycle() -> None:
    versions = source().versions
    created = next(
        v for v in versions if v.entity.kind == "Pod" and v.lifecycle is Lifecycle.CREATED
    )
    deleted = next(
        v for v in versions if v.entity.kind == "Pod" and v.lifecycle is Lifecycle.DELETED
    )
    rows = [
        {"observed_at": v.observed_at.isoformat(), "event": {"type": kind, "object": v.body}}
        for kind, v in (("ADDED", created), ("DELETED", deleted))
    ]
    result = apply_watch_lifecycle(versions, rows)
    authoritative = [v for v in result if v.evidence_id.startswith("dev-watch:")]
    assert [(v.uid, v.lifecycle, v.observed_at) for v in authoritative] == [
        (created.instance_uid, Lifecycle.CREATED, created.observed_at),
        (deleted.instance_uid, Lifecycle.DELETED, deleted.observed_at),
    ]


def test_watch_scope_and_harness_topology_are_isolated(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="isolated"):
        DevelopmentObjectWatch("sre-demo", "pods", tmp_path)
    spec = manifests(
        "binding-dev-1234abcd", "binding-dev-order-1234abcd", "agentic-sre/binding-dev:test"
    )
    assert not any(v["kind"] == "ConfigMap" for v in spec["items"])
    deployments = [v for v in spec["items"] if v["kind"] == "Deployment"]
    assert len(deployments) == 2
    for dep in deployments:
        container = dep["spec"]["template"]["spec"]["containers"][0]
        assert container["imagePullPolicy"] == "Never"
        assert container["readinessProbe"]["initialDelaySeconds"] == 15
        assert not container.get("volumeMounts")
        assert all(v["value"] != "kafka:9092" for v in container["env"] if "value" in v)

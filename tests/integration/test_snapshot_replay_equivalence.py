"""M19-3.18: the offline replay source equals the source the run's RCA actually saw.

Four components are compared, each derived independently on both sides with
the production code: object history, topology, container status Findings
(``container_findings``) and lifecycle status semantics (the lifecycle
``PodStatusObservation`` stream and the ``EndedEpisode`` it yields).
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from datetime import datetime, timedelta
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker
from test_evidence_manifest import Seen
from test_live_diagnosis import T0, FakeLogs, setup  # noqa: F401 - pytest fixture
from test_trajectory_replay import _NoClock

import packages.rca.investigation.graph as graph_module
import packages.rca.live as live_module
import packages.rca.replay as replay_module
import packages.storage.manifest as storage_manifest_module
from apps.control_plane.diagnosis import DiagnosisService
from packages.rca.engine import EngineConfig, build_case
from packages.rca.episode_end import assess_ended_episodes
from packages.rca.live import KubernetesClusterReader, LiveSource
from packages.rca.llm import OpenAIClient
from packages.rca.model import EntityRef, Lifecycle, object_key
from packages.rca.provider_adapter import ProviderAdapter, ProviderReaders
from packages.rca.replay import ReplaySource
from packages.rca.signals import container_findings
from packages.rca.topology import Topology, derive_edges
from packages.storage.manifest import ReplayDataError
from packages.storage.models import (
    DiagnosisRow,
    EventVersionRow,
    IncidentEventRow,
    InvestigationReadRow,
    InvestigationRunRow,
    LifecycleObservationRow,
    ObjectVersionRow,
    RunEvidenceManifestRow,
    SnapshotCycleObjectRow,
    SnapshotCycleRow,
)
from packages.storage.repositories import (
    DiagnosisRepository,
    LifecycleRepository,
    ObjectVersionRepository,
    SnapshotCycleRepository,
)

READ_ONLY_TABLES = (
    RunEvidenceManifestRow,
    SnapshotCycleRow,
    SnapshotCycleObjectRow,
    ObjectVersionRow,
    LifecycleObservationRow,
    EventVersionRow,
    InvestigationReadRow,
    InvestigationRunRow,
    DiagnosisRow,
    IncidentEventRow,
)


def _at(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat()


RECOVERED_POD: dict[str, Any] = {
    "kind": "Pod",
    "metadata": {
        "name": "payment-service-7d9f-x2x4q",
        "namespace": "sre-demo",
        "uid": "pay-uid-1",
        "labels": {"app": "payment-service"},
        "ownerReferences": [{"kind": "ReplicaSet", "name": "payment-service-7d9f"}],
    },
    "status": {"conditions": [{"type": "Ready", "status": "True", "lastTransitionTime": _at(6)}]},
}
CRASHING_POD: dict[str, Any] = {
    "kind": "Pod",
    "metadata": {
        "name": "order-service-5c9-abcde",
        "namespace": "sre-demo",
        "uid": "order-uid-1",
        "labels": {"app": "order-service"},
        "ownerReferences": [{"kind": "ReplicaSet", "name": "order-service-5c9"}],
    },
    "status": {
        "conditions": [{"type": "Ready", "status": "False", "lastTransitionTime": _at(10)}],
        "containerStatuses": [
            {
                "name": "app",
                "restartCount": 4,
                "state": {"waiting": {"reason": "CrashLoopBackOff", "message": "back-off"}},
                "lastState": {
                    "terminated": {"reason": "Error", "exitCode": 1, "finishedAt": _at(10)}
                },
            }
        ],
    },
}
ORDER_RS: dict[str, Any] = {
    "kind": "ReplicaSet",
    "metadata": {
        "name": "order-service-5c9",
        "namespace": "sre-demo",
        "ownerReferences": [{"kind": "Deployment", "name": "order-service"}],
    },
}
BACKOFF: dict[str, Any] = {
    "kind": "Event",
    "metadata": {"name": "pay.backoff", "namespace": "sre-demo", "uid": "k8s-event-1"},
    "involvedObject": {
        "kind": "Pod",
        "name": "payment-service-7d9f-x2x4q",
        "namespace": "sre-demo",
        "uid": "pay-uid-1",
    },
    "reason": "BackOff",
    "type": "Warning",
    "message": "back-off restarting failed container",
    "firstTimestamp": _at(3),
    "lastTimestamp": _at(4),
    "count": 2,
}
# Journaled earlier, never listed by the capture, no tombstone: absent is not deleted.
FLAGS: dict[str, Any] = {
    "kind": "ConfigMap",
    "metadata": {"name": "feature-flags", "namespace": "sre-demo", "uid": "cm-uid-1"},
    "data": {"mode": "on"},
}
FLAGS_REF = EntityRef(kind="ConfigMap", name="feature-flags", namespace="sre-demo")


# --- canonical form ----------------------------------------------------------


def canonical(value: Any) -> Any:
    """Plain JSON of any model value: no repr, identity, or collection-order accident."""
    if isinstance(value, BaseModel):
        return canonical(value.model_dump(mode="json"))
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: canonical(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, Mapping):
        return {
            str(key): canonical(item)
            for key, item in sorted(value.items(), key=lambda kv: str(kv[0]))
        }
    if isinstance(value, (list, tuple)):
        return [canonical(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((canonical(item) for item in value), key=_encode)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"no canonical form for {type(value).__name__}")


def _encode(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: Any) -> str:
    return sha256(_encode(canonical(value)).encode("utf-8")).hexdigest()


def object_history_state(history: Mapping[EntityRef, Sequence[Any]]) -> list[dict[str, Any]]:
    """Every version of every object, in the order RCA consumes it (oldest first)."""
    return [
        {
            "entity": entity.canonical,
            "versions": [
                {
                    "kind": version.entity.kind,
                    "namespace": version.entity.namespace,
                    "name": version.entity.name,
                    "uid": version.uid,
                    "lifecycle": version.lifecycle,
                    "evidence_id": version.evidence_id,
                    "observed_at": version.observed_at,
                    "body": version.body,
                }
                for version in versions
            ],
        }
        for entity, versions in sorted(history.items(), key=lambda item: item[0].canonical)
    ]


def topology_state(topology: Topology) -> dict[str, Any]:
    """Every derived relation plus the exact latest version each entity resolves to."""
    return {
        "edges": sorted(
            (
                [edge.source.canonical, edge.relation, edge.target.canonical]
                for edge in topology.edges
            ),
        ),
        "latest": {
            entity.canonical: [version.evidence_id, version.uid]
            for entity, version in sorted(topology.latest.items(), key=lambda kv: kv[0].canonical)
        },
    }


def snapshot_cycle_state(source: Any) -> dict[str, Any]:
    """The exact persisted snapshot cycle the source holds: id, time, every listed body."""
    live = source._base if isinstance(source, ReplaySource) else source
    return {
        "cycle_id": live.snapshot_cycle_id,
        "observed_at": live.snapshot_observed_at,
        "objects": sorted(
            ([object_key(body), body] for body in live.current_objects),
            key=lambda item: str(item[0]),
        ),
    }


def snapshot_state(source: Any) -> dict[str, Any]:
    """The four components, each derived from ``source`` by the production code."""
    history = source.object_history()
    events = list(source.events())
    latest = {entity: versions[-1] for entity, versions in history.items() if versions}
    topology = Topology(derive_edges(latest, events), latest)
    statuses = source.pod_status_observations()
    case = build_case(source)
    ended = assess_ended_episodes(
        case.hypotheses,
        history=history,
        pod_statuses=statuses,
        onset=case.symptoms.onset,
        grace=EngineConfig().ranking.verification_onset_grace,
    )
    return {
        "object_history": canonical(
            {
                "versions": object_history_state(history),
                "snapshot_cycle": snapshot_cycle_state(source),
            }
        ),
        "topology": canonical(topology_state(topology)),
        "container_status_findings": sorted(
            (canonical(finding) for finding in container_findings(history)), key=_encode
        ),
        "lifecycle_status_semantics": {
            "pod_status_observations": sorted(
                (canonical(status) for status in statuses), key=_encode
            ),
            "ended_episodes": canonical(ended),
        },
    }


def digests(state: Mapping[str, Any]) -> dict[str, str]:
    parts = {name: digest(value) for name, value in state.items()}
    return {**parts, "snapshot_state": digest(dict(state))}


# --- fixture -------------------------------------------------------------------


class Run:
    def __init__(
        self,
        factory: sessionmaker[Session],
        run_id: str,
        original: dict[str, Any],
        live: LiveSource,
    ) -> None:
        self.factory = factory
        self.run_id = run_id
        # Canonicalized from the run's own LiveSource right after the run.
        self.original = original
        self.live = live


def _record(world: Any, monkeypatch: pytest.MonkeyPatch) -> Run:
    factory, cluster, clock, incident_id = world
    cluster.objects = [item for item in cluster.objects if item["kind"] != "Pod"]
    cluster.objects += [
        copy.deepcopy(RECOVERED_POD),
        copy.deepcopy(CRASHING_POD),
        copy.deepcopy(ORDER_RS),
    ]
    cluster.events = [copy.deepcopy(BACKOFF)]
    with factory() as session:
        ObjectVersionRepository(session).record(copy.deepcopy(FLAGS), T0 + timedelta(minutes=1))
    service = DiagnosisService(
        session_factory=factory,
        namespaces=("sre-demo",),
        reader=cluster,
        clock=clock,
        provider_readers=ProviderReaders(loki=FakeLogs([])),
    )
    clock.now = T0 + timedelta(minutes=5)
    service.snapshot()  # an earlier watch cycle: the journal's first versions
    # Between cycles: a spec change (second journal version) and a status-only
    # change (the journal keeps desired state, the snapshot the full body).
    deployment = next(
        item for item in cluster.objects if item["metadata"]["name"] == "payment-service"
    )
    deployment["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"] = "900"
    crashing = next(
        item for item in cluster.objects if item["metadata"]["name"] == "order-service-5c9-abcde"
    )
    crashing["status"]["containerStatuses"][0]["restartCount"] = 6
    cluster.objects = [item for item in cluster.objects if item["kind"] != "ConfigMap"]
    seen = Seen(monkeypatch)
    clock.now = T0 + timedelta(minutes=30)
    service.run(incident_id)
    live = seen.source
    original = snapshot_state(live)
    with factory() as session:
        run_id = DiagnosisRepository(session).latest_run_id(incident_id)
    assert run_id is not None
    return Run(factory, run_id, original, live)


@pytest.fixture
def run(setup: Any, monkeypatch: pytest.MonkeyPatch) -> Run:  # noqa: F811
    return _record(setup, monkeypatch)


def _bomb(name: str) -> Any:
    def explode(*_: Any, **__: Any) -> Any:
        raise AssertionError(f"snapshot replay must not call {name}")

    return explode


def go_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(KubernetesClusterReader, "__init__", _bomb("KubernetesClusterReader()"))
    for method in ("_client", "list_objects", "list_events"):
        monkeypatch.setattr(KubernetesClusterReader, method, _bomb(method))
    monkeypatch.setattr(DiagnosisService, "snapshot_result", _bomb("snapshot_result"))
    monkeypatch.setattr(ProviderAdapter, "_provider_call", _bomb("ProviderAdapter._provider_call"))
    monkeypatch.setattr(live_module, "urlopen", _bomb("urlopen"))
    monkeypatch.setattr(OpenAIClient, "complete_json", _bomb("OpenAIClient.complete_json"))
    for module in (live_module, replay_module, graph_module, storage_manifest_module):
        monkeypatch.setattr(module, "datetime", _NoClock)


def _replayed(run: Run) -> dict[str, Any]:
    return snapshot_state(ReplaySource.from_run(run.run_id, session_factory=run.factory))


def _table_counts(factory: sessionmaker[Session]) -> tuple[int, ...]:
    with factory() as session:
        return tuple(
            int(session.scalar(select(func.count()).select_from(table)) or 0)
            for table in READ_ONLY_TABLES
        )


# --- tests ---------------------------------------------------------------------


def test_replay_source_state_equals_the_run_time_source(
    run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _table_counts(run.factory)
    go_offline(monkeypatch)
    replayed = _replayed(run)
    original_digests, replay_digests = digests(run.original), digests(replayed)
    for component in (
        "object_history",
        "topology",
        "container_status_findings",
        "lifecycle_status_semantics",
    ):
        assert replay_digests[component] == original_digests[component], component
    assert replay_digests["snapshot_state"] == original_digests["snapshot_state"]
    assert _table_counts(run.factory) == before

    # The fixture really exercises every component.
    history = replayed["object_history"]["versions"]
    versions = [v for item in history for v in item["versions"]]
    ids = [v["evidence_id"] for v in versions]
    # The capture journals its own listing, so every version is journal-backed.
    assert ids and all(i.startswith("journal:") for i in ids)
    assert len(ids) == len(set(ids))
    assert max(len(item["versions"]) for item in history) >= 2
    cycle = replayed["object_history"]["snapshot_cycle"]
    assert cycle == run.original["object_history"]["snapshot_cycle"]
    with run.factory() as session:
        assert cycle["cycle_id"] == session.scalars(select(SnapshotCycleRow.cycle_id)).one()
        boundary = next(
            row.payload
            for row in session.scalars(
                select(IncidentEventRow).where(IncidentEventRow.event_type == "EVIDENCE_GATHERED")
            )
        )
    listed = {key for key, _ in cycle["objects"]}
    assert len(listed) == boundary["objects"] == 7
    assert FLAGS_REF.canonical not in listed
    assert {v["uid"] for v in versions} >= {"pay-uid-1", "order-uid-1", "cm-uid-1"}
    assert replayed["topology"]["edges"]
    findings = replayed["container_status_findings"]
    assert [f["kind"] for f in findings] == ["CONTAINER_FAILURE"]
    assert findings[0]["entity_instance"]["uid"] == "order-uid-1"
    statuses = replayed["lifecycle_status_semantics"]["pod_status_observations"]
    assert {s["uid"] for s in statuses} == {"pay-uid-1", "order-uid-1"}
    ended = replayed["lifecycle_status_semantics"]["ended_episodes"]
    assert [[i["uid"], i["basis"]] for e in ended.values() for i in e["instances"]] == [
        ["pay-uid-1", "RECOVERED"]
    ]
    # Absent from the capture listing, never tombstoned: not a deletion.
    flags = next(item for item in history if item["entity"] == FLAGS_REF.canonical)
    assert [v["lifecycle"] for v in flags["versions"]] == [Lifecycle.OBSERVED.value]


def test_evidence_identity_is_part_of_the_digest(run: Run) -> None:
    source = ReplaySource.from_run(run.run_id, session_factory=run.factory)
    history = source.object_history()
    renamed = {
        entity: [
            version.model_copy(update={"evidence_id": f"{version.evidence_id}0"})
            if entity == FLAGS_REF
            else version
            for version in versions
        ]
        for entity, versions in history.items()
    }
    # Same body and time under another persistent identity: a different digest.
    assert digest(object_history_state(renamed)) != digest(object_history_state(history))
    assert canonical(object_history_state(history)) == run.original["object_history"]["versions"]


def test_topology_is_derived_from_each_side(run: Run, monkeypatch: pytest.MonkeyPatch) -> None:
    real = ReplaySource.object_history

    def without_service(self: ReplaySource) -> Any:
        return {e: v for e, v in real(self).items() if e.kind != "Service"}

    monkeypatch.setattr(ReplaySource, "object_history", without_service)
    replayed = _replayed(run)
    assert digests(replayed)["topology"] != digests(run.original)["topology"]


def test_later_rows_and_cycles_do_not_change_the_replayed_state(
    run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    later = T0 + timedelta(minutes=40)
    with run.factory() as session:
        repository = ObjectVersionRepository(session)
        repository.record(
            {
                "kind": "ConfigMap",
                "metadata": {"name": "late", "namespace": "sre-demo", "uid": "late-1"},
            },
            later,
        )
        changed = copy.deepcopy(FLAGS)
        changed["data"] = {"mode": "off"}
        repository.record(changed, later)
        newer_pod = copy.deepcopy(CRASHING_POD)
        newer_pod["status"]["containerStatuses"][0]["restartCount"] = 99
        SnapshotCycleRepository(session).record(
            run_id=run.run_id,
            started_at=later,
            observed_at=later,
            completed_at=later,
            completed_scopes=[],
            failed_scopes=[],
            objects=[newer_pod],
        )
        template = session.scalars(
            select(LifecycleObservationRow).where(
                LifecycleObservationRow.instance_uid == "pay-uid-1"
            )
        ).first()
        assert template is not None
        LifecycleRepository(session).append(
            namespace=template.namespace,
            kind=template.kind,
            name=template.name,
            instance_uid=template.instance_uid,
            type=template.type,
            observed_at=later,
            source="collector",
            payload={
                **template.payload,
                "ready": {
                    "type": "Ready",
                    "status": "False",
                    "lastTransitionTime": later.isoformat(),
                },
            },
        )
        session.commit()
    go_offline(monkeypatch)
    assert digests(_replayed(run)) == digests(run.original)


def test_missing_snapshot_rows_fail_instead_of_shrinking_the_source(run: Run) -> None:
    with run.factory() as session:
        cycle_id = session.scalars(select(SnapshotCycleRow.cycle_id)).one()
        key = session.scalars(
            select(SnapshotCycleObjectRow.object_key).where(
                SnapshotCycleObjectRow.cycle_id == cycle_id
            )
        ).first()
        # A retention-style Core delete the ORM guard does not see.
        session.execute(
            delete(SnapshotCycleObjectRow).where(
                SnapshotCycleObjectRow.cycle_id == cycle_id,
                SnapshotCycleObjectRow.object_key == key,
            )
        )
        session.commit()
    with pytest.raises(ReplayDataError, match="snapshot object"):
        ReplaySource.from_run(run.run_id, session_factory=run.factory)
    with run.factory() as session:
        session.execute(
            delete(SnapshotCycleObjectRow).where(SnapshotCycleObjectRow.cycle_id == cycle_id)
        )
        session.execute(delete(SnapshotCycleRow).where(SnapshotCycleRow.cycle_id == cycle_id))
        session.commit()
    with pytest.raises(ReplayDataError, match="was not found"):
        ReplaySource.from_run(run.run_id, session_factory=run.factory)


def test_replayed_state_is_identical_across_processes(run: Run) -> None:
    database = run.factory().get_bind().engine.url.database
    expected = digests(run.original)
    script = (
        "import json, sys\n"
        "sys.path[:0] = ['tests/integration', 'tests/unit/rca']\n"
        "from sqlalchemy import create_engine\n"
        "from packages.storage.database import create_session_factory\n"
        "from packages.rca.replay import ReplaySource\n"
        "from test_snapshot_replay_equivalence import digests, snapshot_state\n"
        "factory = create_session_factory(create_engine(sys.argv[1]))\n"
        "print(json.dumps(digests(snapshot_state(ReplaySource.from_run(sys.argv[2], session_factory=factory)))))\n"
    )
    for seed in ("21", "22"):
        completed = subprocess.run(
            [sys.executable, "-c", script, f"sqlite:///{database}", run.run_id],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
            cwd=Path(__file__).resolve().parents[2],
        )
        assert json.loads(completed.stdout.strip().splitlines()[-1]) == expected

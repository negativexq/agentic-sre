"""M21 amendment 5: bounded channel-K reference coverage (§4.5), audit only."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from rca_builders import at, config_change_source, deployment, ref, version

from packages.rca import k_coverage
from packages.rca.channels import _preemption, channel_assessments
from packages.rca.engine import EngineConfig, build_case
from packages.rca.k_coverage import (
    ApiAccess,
    ApiAuditEvidence,
    ApiServerAuditCoverage,
    JournalCoverageFact,
    evaluate_k,
)
from packages.rca.model import (
    ChannelApplicability,
    ChannelState,
    CoveragePreconditionStatus,
    EntityRef,
    ObjectVersion,
)
from packages.rca.topology import Topology, derive_edges

PASS = CoveragePreconditionStatus.PASS
FAIL = CoveragePreconditionStatus.FAIL
UNKNOWN = CoveragePreconditionStatus.UNKNOWN
RECORDER = ref("infra/ConfigMap/recorder")
CHECKOUT = ref("shop/Deployment/checkout")


def _history(*versions: ObjectVersion) -> dict[EntityRef, list[ObjectVersion]]:
    history: dict[EntityRef, list[ObjectVersion]] = {}
    for item in sorted(versions, key=lambda v: v.observed_at):
        history.setdefault(item.entity, []).append(item)
    return history


def _base() -> list[ObjectVersion]:
    return [
        version("infra/ConfigMap/recorder", 0, {"apiVersion": "v1", "data": {"a": "1"}}),
        version("infra/ConfigMap/recorder", 10, {"apiVersion": "v1", "data": {"a": "2"}}, 1),
        version("shop/Deployment/checkout", 0, deployment("checkout")),
    ]


def _k(
    versions: Sequence[ObjectVersion],
    *,
    closure: set[EntityRef] | None = None,
    symptoms: set[EntityRef] | None = None,
    journal: JournalCoverageFact | None = None,
    audit: ApiAuditEvidence | None = None,
) -> k_coverage.KEvaluation:
    history = _history(*versions)
    latest = {entity: items[-1] for entity, items in history.items()}
    return evaluate_k(
        closure or {RECORDER},
        history=history,
        events=(),
        topology=Topology(derive_edges(latest), latest),
        symptom_entities=symptoms or {CHECKOUT},
        window_start=at(0),
        window_end=at(20),
        journal_coverage=journal,
        api_audit=audit,
    )


def _status(result: k_coverage.KEvaluation) -> dict[str, CoveragePreconditionStatus]:
    return {p.precondition: p.status for p in result.preconditions}


def _journal(**changes: Any) -> JournalCoverageFact:
    fields: dict[str, Any] = {
        "coverage_start": at(-5),
        "coverage_end": at(30),
        "covered_namespaces": frozenset({"infra", "shop", "_cluster"}),
        "covered_kinds": frozenset(k_coverage.REQUIRED_JOURNAL_KINDS),
        "continuous": True,
        "gap_count": 0,
        "source": "test-journal",
        "source_version": "1",
        "evidence_id": "journal-coverage:1",
    }
    fields.update(changes)
    return JournalCoverageFact(**fields)


def _audit(*accesses: ApiAccess, **changes: Any) -> ApiAuditEvidence:
    fields: dict[str, Any] = {
        "serving_instances": frozenset({"api-0"}),
        "instances": (
            ApiServerAuditCoverage(
                instance="api-0",
                coverage_start=at(-5),
                coverage_end=at(30),
                counter_continuous=True,
                error_count_start=10,
                error_count_end=10,
            ),
        ),
        "policy_covers_relevant_requests": True,
        "active_watches_at_start_known": True,
        "accesses": accesses,
    }
    fields.update(changes)
    return ApiAuditEvidence(**fields)


def _access(user: str, verb: str = "get", **changes: Any) -> ApiAccess:
    fields: dict[str, Any] = {
        "user": user,
        "verb": verb,
        "kind": "ConfigMap",
        "namespace": "infra",
        "name": "recorder",
        "started_at": at(12),
        "ended_at": at(12),
        "evidence_id": f"audit:{user}:{verb}",
    }
    fields.update(changes)
    return ApiAccess(**fields)


def test_every_precondition_is_recorded_and_today_k_is_never_no_path_covered() -> None:
    result = _k(_base())
    assert [p.precondition for p in result.preconditions] == ["K1", "K2", "K3", "K4", "K5", "K6"]
    assert _status(result) == {
        "K1": UNKNOWN,  # no source supplies a journal-coverage fact
        "K2": FAIL,  # every profile kind is INCOMPLETE
        "K3": UNKNOWN,  # separation cannot be proven without the full OD-A4 side
        "K4": PASS,
        "K5": UNKNOWN,
        "K6": UNKNOWN,
    }
    # Precedence: a known open surface (K2) outranks missing facts.
    assert (result.state, result.gap_reason) == (
        ChannelState.UNCOVERED,
        "K2_KIND_PROFILE_INCOMPLETE:ConfigMap",
    )


def test_with_every_other_fact_supplied_k3_alone_keeps_k_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(k_coverage.KIND_PROFILE, "ConfigMap", ("v1", True))
    result = _k(_base(), journal=_journal(), audit=_audit())
    assert _status(result) == {
        "K1": PASS,
        "K2": PASS,
        "K3": UNKNOWN,
        "K4": PASS,
        "K5": PASS,
        "K6": PASS,
    }
    assert (result.state, result.gap_reason) == (ChannelState.UNKNOWN, "K3_SYMPTOM_SIDE_INCOMPLETE")


def _workload(
    namespace: str, name: str, *, config: str | None, calls: str | None
) -> list[ObjectVersion]:
    body = deployment(name, config=config)
    if calls:
        body["spec"]["template"]["spec"]["containers"][0]["env"] = [
            {"name": "BACKEND_ADDR", "value": f"http://{calls}:8080"}
        ]
    rs, pod = f"{name}-5d8f7c9b4", f"{name}-5d8f7c9b4-abcde"
    return [
        version(f"{namespace}/Deployment/{name}", 0, body),
        version(
            f"{namespace}/ReplicaSet/{rs}",
            0,
            {"metadata": {"ownerReferences": [{"kind": "Deployment", "name": name}]}},
        ),
        version(
            f"{namespace}/Pod/{pod}",
            0,
            {
                "metadata": {
                    "labels": {"app": name},
                    "ownerReferences": [{"kind": "ReplicaSet", "name": rs}],
                },
                "spec": {"containers": [{"name": name}]},
            },
        ),
        version(f"{namespace}/Service/{name}", 0, {"spec": {"selector": {"app": name}}}),
    ]


def test_a_path_deeper_than_any_policy_cap_is_found() -> None:
    # recorder -> collector -> Service collector -> relay (calls it) -> Service relay
    # -> front (calls relay): five causal hops, beyond F1's depth-4 search.
    versions = [
        *_base(),
        *_workload("infra", "collector", config="recorder", calls=None),
        *_workload("infra", "relay", config=None, calls="collector.infra"),
        *_workload("web", "front", config=None, calls="relay.infra"),
    ]
    front = ref("web/Deployment/front")
    result = _k(versions, symptoms={front})
    assert result.state is ChannelState.PATH
    assert _status(result)["K4"] is FAIL
    history = _history(*versions)
    latest = {entity: items[-1] for entity, items in history.items()}
    topology = Topology(derive_edges(latest), latest)
    assert topology.causal_distance(RECORDER, {front}) is None
    assert topology.causal_distance(RECORDER, {front}, max_depth=5) == 5


def test_a_path_present_only_in_an_earlier_window_version_counts() -> None:
    versions = [
        *_base(),
        version("infra/Deployment/checkout", 5, deployment("checkout", config="recorder")),
        version("infra/Deployment/checkout", 8, deployment("checkout"), 1),
    ]
    result = _k(versions, symptoms={ref("infra/Deployment/checkout")})
    assert result.state is ChannelState.PATH


def test_a_shared_namespace_fails_k3() -> None:
    result = _k(_base(), symptoms={ref("infra/Deployment/other")})
    assert _status(result)["K3"] is FAIL
    assert next(p for p in result.preconditions if p.precondition == "K3").reason == (
        "SAME_NAMESPACE_REFERENCE_SURFACE_INCOMPLETE"
    )


@pytest.mark.parametrize(
    ("journal", "status"),
    [
        (None, UNKNOWN),
        (_journal(continuous=False), FAIL),
        (_journal(gap_count=1), FAIL),
        (_journal(coverage_start=at(1)), FAIL),
        (_journal(covered_kinds=frozenset({"ConfigMap"})), FAIL),
        (_journal(), PASS),
    ],
)
def test_journal_coverage_must_be_positively_proven(
    journal: JournalCoverageFact | None, status: CoveragePreconditionStatus
) -> None:
    assert _status(_k(_base(), journal=journal))["K1"] is status


def test_a_namespace_selector_peer_is_an_unsupported_surface() -> None:
    policy = version(
        "shop/NetworkPolicy/allow-infra",
        0,
        {"spec": {"podSelector": {}, "ingress": [{"from": [{"namespaceSelector": {}}]}]}},
    )
    result = _k([*_base(), policy])
    k5 = next(p for p in result.preconditions if p.precondition == "K5")
    assert (k5.status, k5.reason) == (FAIL, "K5_NETPOL_NAMESPACE_SELECTOR")
    assert k5.evidence_ids == (policy.evidence_id,)


def _cluster_reader() -> list[ObjectVersion]:
    return [
        version(
            "_cluster/ClusterRole/reader",
            0,
            {"rules": [{"apiGroups": [""], "resources": ["configmaps"], "verbs": ["list"]}]},
        ),
        version(
            "_cluster/ClusterRoleBinding/reader",
            0,
            {
                "roleRef": {"kind": "ClusterRole", "name": "reader"},
                "subjects": [{"kind": "ServiceAccount", "name": "ksm", "namespace": "monitoring"}],
            },
        ),
    ]


def test_a_permission_without_audit_evidence_keeps_the_surface_open() -> None:
    result = _k([*_base(), *_cluster_reader()])
    k6 = next(p for p in result.preconditions if p.precondition == "K6")
    assert (k6.status, k6.reason) == (FAIL, "K6_PERMISSION_WITHOUT_AUDIT_EVIDENCE")
    # With complete audit evidence and no actual access, the permission is only potential.
    assert _status(_k([*_base(), *_cluster_reader()], audit=_audit()))["K6"] is PASS


def _node_pod(config: str | None) -> ObjectVersion:
    spec: dict[str, Any] = {"nodeName": "node-a", "containers": [{"name": "c"}]}
    if config:
        spec["volumes"] = [{"name": "v", "configMap": {"name": config}}]
    return version("infra/Pod/user-1", 0, {"spec": spec})


def test_a_kubelet_read_is_attributed_only_to_an_exact_pod_reference() -> None:
    read = _access("system:node:node-a")
    assert _status(_k([*_base(), _node_pod("recorder")], audit=_audit(read)))["K6"] is PASS
    result = _k([*_base(), _node_pod(None)], audit=_audit(read))
    k6 = next(p for p in result.preconditions if p.precondition == "K6")
    assert (k6.status, k6.reason) == (FAIL, "K6_UNATTRIBUTED_NODE_ACCESS")


@pytest.mark.parametrize(
    ("instance", "reason"),
    [
        ({"counter_continuous": False}, "K6_AUDIT_LOSS_UNPROVEN"),  # reset / restart / gap
        ({"error_count_end": None}, "K6_AUDIT_LOSS_UNPROVEN"),
        ({"coverage_end": at(19)}, "K6_AUDIT_COVERAGE_INCOMPLETE"),
    ],
)
def test_audit_loss_and_coverage_must_be_proven(instance: dict[str, Any], reason: str) -> None:
    coverage = _audit().instances[0].model_copy(update=instance)
    k6 = next(
        p
        for p in _k(_base(), audit=_audit(instances=(coverage,))).preconditions
        if p.precondition == "K6"
    )
    assert (k6.status, k6.reason) == (UNKNOWN, reason)


def test_a_counted_audit_loss_and_a_missing_replica_are_not_coverage() -> None:
    lost = _audit().instances[0].model_copy(update={"error_count_end": 11})
    assert _status(_k(_base(), audit=_audit(instances=(lost,))))["K6"] is FAIL
    replicas = _audit(serving_instances=frozenset({"api-0", "api-1"}))
    assert _status(_k(_base(), audit=replicas))["K6"] is UNKNOWN


def test_unknown_pre_window_watches_keep_the_surface_open() -> None:
    k6 = next(
        p
        for p in _k(_base(), audit=_audit(active_watches_at_start_known=False)).preconditions
        if p.precondition == "K6"
    )
    assert (k6.status, k6.reason) == (FAIL, "K6_PRE_WINDOW_WATCHES_UNKNOWN")


def test_a_watch_opened_before_the_window_and_a_collection_list_are_accesses() -> None:
    watch = _access("alice", "watch", name=None, started_at=at(-60), ended_at=None)
    assert _status(_k(_base(), audit=_audit(watch)))["K6"] is FAIL
    cluster_list = _access("alice", "list", name=None, namespace=None)
    assert _status(_k(_base(), audit=_audit(cluster_list)))["K6"] is FAIL
    elsewhere = _access("alice", "list", name=None, namespace="shop")
    assert _status(_k(_base(), audit=_audit(elsewhere)))["K6"] is PASS


def test_an_unrecognised_system_identity_is_not_control_plane() -> None:
    known = _k(_base(), audit=_audit(_access("system:kube-controller-manager")))
    assert known.control_plane_access and _status(known)["K6"] is PASS
    unknown = _k(_base(), audit=_audit(_access("system:some-addon")))
    assert not unknown.control_plane_access
    assert _status(unknown)["K6"] is FAIL


def test_an_impersonated_access_keeps_both_actors_and_the_surface_open() -> None:
    access = _access("system:serviceaccount:monitoring:ksm", impersonated_user="bob")
    k6 = next(p for p in _k(_base(), audit=_audit(access)).preconditions if p.precondition == "K6")
    assert (k6.status, k6.reason) == (FAIL, "K6_IMPERSONATED_ACCESS")


def test_an_actual_reader_workload_joins_the_closure_to_a_fixed_point() -> None:
    source = config_change_source()
    reader = version(
        "monitoring/Pod/ksm-1",
        0,
        {"spec": {"serviceAccountName": "ksm", "containers": [{"name": "ksm"}]}},
    )
    source.versions.append(reader)
    case = build_case(source)
    audit = _audit(_access("system:serviceaccount:monitoring:ksm", "list", name=None))
    assessments = channel_assessments(
        case.hypotheses,
        topology=case.topology,
        history=case.source.object_history(),
        symptom_entities=set(case.context.symptom_entities),
        symptom_services=case.symptoms.services,
        runtime_graph=case.runtime_graph,
        trace_spans=[],
        onset=case.symptoms.onset,
        grace=EngineConfig().ranking.verification_onset_grace,
        api_audit=audit,
    )
    recorder = next(a for a in assessments.values() if RECORDER.canonical in a.closure_members)
    assert reader.entity.canonical in recorder.closure_members
    # The joined Pod makes the runtime channels applicable and re-evaluated.
    n = next(e for e in recorder.evaluations if e.channel == "N")
    assert n.applicability is ChannelApplicability.APPLICABLE


def _pod(name: str, priority: int | None, policy: str | None) -> ObjectVersion:
    spec: dict[str, Any] = {"containers": [{"name": "c"}]}
    if priority is not None:
        spec["priority"] = priority
    if policy is not None:
        spec["preemptionPolicy"] = policy
    return version(name, 0, {"spec": spec})


@pytest.mark.parametrize(
    ("closure_pod", "expected"),
    [
        ((1000, "PreemptLowerPriority"), ChannelState.UNCOVERED),
        ((1000, "Never"), None),
        ((0, "PreemptLowerPriority"), None),
        ((None, "PreemptLowerPriority"), ChannelState.UNKNOWN),  # never NOT_APPLICABLE
    ],
)
def test_preemption_is_a_control_plane_channel_fact(
    closure_pod: tuple[int | None, str | None], expected: ChannelState | None
) -> None:
    job_pod = _pod("infra/Pod/job-1", *closure_pod)
    victim = _pod("shop/Pod/checkout-1", 0, "PreemptLowerPriority")
    latest = {job_pod.entity: job_pod, victim.entity: victim}
    topology = Topology(derive_edges(latest), latest)
    result = _preemption([job_pod.entity], latest, {victim.entity}, topology)
    assert (result[0] if result else None) is expected


def test_the_k_channel_carries_its_rule_and_preconditions_in_the_audit() -> None:
    case = build_case(config_change_source())
    for assessment in channel_assessments(
        case.hypotheses,
        topology=case.topology,
        history=case.source.object_history(),
        symptom_entities=set(case.context.symptom_entities),
        symptom_services=case.symptoms.services,
        runtime_graph=case.runtime_graph,
        trace_spans=[],
        onset=case.symptoms.onset,
        grace=EngineConfig().ranking.verification_onset_grace,
    ).values():
        k = next(e for e in assessment.evaluations if e.channel == "K")
        assert k.applicability is ChannelApplicability.APPLICABLE
        assert k.rule_id == "m21.k-reference-coverage.v1"
        assert len(k.preconditions) == 6
        assert k.state is not ChannelState.NO_PATH_COVERED

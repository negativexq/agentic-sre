from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import pytest

from packages.rca.mechanism_bridge import (
    RuntimeMechanismBridgeBasis,
    RuntimeMechanismRelation,
    derive_runtime_failure_workload_episodes,
    derive_runtime_mechanism_bridges,
    is_direct_fault_object_kind,
    live_object_version_at_or_before,
    object_version_at_or_before,
    workload_config_references,
)
from packages.rca.model import EntityRef, Finding, FindingKind, Lifecycle, ObjectVersion
from packages.rca.runtime_evidence import (
    RuntimeBindingQuality,
    RuntimeEvidence,
    RuntimeKubernetesBinding,
    RuntimeOutcomeState,
    RuntimeProtocol,
    RuntimeServiceOutcomeSummary,
)
from packages.rca.runtime_graph import RuntimeSpanKind

T0 = datetime(2025, 1, 1, tzinfo=UTC)


def _ref(kind: str, name: str, namespace: str = "shop") -> EntityRef:
    return EntityRef(namespace=namespace, kind=kind, name=name)


def _version(
    entity: EntityRef,
    at: int = 0,
    *,
    body: Mapping[str, object] | None = None,
    lifecycle: Lifecycle = Lifecycle.OBSERVED,
) -> ObjectVersion:
    return ObjectVersion(
        entity=entity,
        observed_at=T0 + timedelta(seconds=at),
        body=dict(body or {}),
        evidence_id=f"obj:{entity.kind}:{entity.name}:{at}",
        lifecycle=lifecycle,
    )


def _binding() -> RuntimeKubernetesBinding:
    return RuntimeKubernetesBinding(
        namespace="shop",
        deployment="payment",
        pod="payment-abc",
        quality=RuntimeBindingQuality.NAMESPACE_DEPLOYMENT_POD,
    )


def _runtime(*, state: RuntimeOutcomeState = RuntimeOutcomeState.ERROR) -> RuntimeEvidence:
    summary = RuntimeServiceOutcomeSummary(
        service="payment",
        binding=_binding(),
        span_kind=RuntimeSpanKind.SERVER,
        protocol=RuntimeProtocol.HTTP,
        state=state,
        protocol_code="503",
        error_type="upstream",
        first_seen=T0 + timedelta(seconds=10),
        last_seen=T0 + timedelta(seconds=12),
        observed_spans=2,
        evidence_ids=("runtime:1",),
    )
    empty = RuntimeEvidence.empty()
    return RuntimeEvidence(
        bindings=empty.bindings,
        service_outcomes=(summary,),
        call_outcomes=empty.call_outcomes,
        stats=empty.stats.model_copy(update={"service_outcome_summaries": 1}),
    )


def _history(deployment_body: Mapping[str, object]) -> dict[EntityRef, list[ObjectVersion]]:
    deployment = _ref("Deployment", "payment")
    pod = _ref("Pod", "payment-abc")
    return {
        deployment: [_version(deployment, body=deployment_body)],
        pod: [
            _version(
                pod,
                body={"metadata": {"labels": {"app": "payment"}}},
            )
        ],
    }


def _finding(kind: FindingKind, entity: EntityRef, *, evidence: str = "finding:1") -> Finding:
    return Finding(
        kind=kind,
        entity=entity,
        at=T0,
        summary="mechanism",
        evidence_ids=(evidence,),
    )


def test_config_references_cover_workload_reference_forms() -> None:
    body = {
        "spec": {
            "template": {
                "spec": {
                    "volumes": [
                        {"configMap": {"name": "volume-config"}},
                        {"secret": {"secretName": "volume-secret"}},
                        {
                            "projected": {
                                "sources": [
                                    {"configMap": {"name": "projected-config"}},
                                    {"secret": {"name": "projected-secret"}},
                                ]
                            }
                        },
                    ],
                    "containers": [
                        {
                            "envFrom": [
                                {"configMapRef": {"name": "env-config"}},
                                {"secretRef": {"name": "env-secret"}},
                            ],
                            "env": [
                                {"valueFrom": {"configMapKeyRef": {"name": "key-config"}}},
                                {"valueFrom": {"secretKeyRef": {"name": "key-secret"}}},
                            ],
                        }
                    ],
                }
            }
        }
    }
    assert workload_config_references(body) == frozenset(
        {
            ("ConfigMap", "volume-config"),
            ("Secret", "volume-secret"),
            ("ConfigMap", "projected-config"),
            ("Secret", "projected-secret"),
            ("ConfigMap", "env-config"),
            ("Secret", "env-secret"),
            ("ConfigMap", "key-config"),
            ("Secret", "key-secret"),
        }
    )


def test_history_lookup_is_historical_and_deleted_latest_wins() -> None:
    entity = _ref("ConfigMap", "payment-config")
    history = {
        entity: [
            _version(entity, 20, lifecycle=Lifecycle.DELETED),
            _version(entity, 0),
            _version(entity, 10),
        ]
    }
    selected = object_version_at_or_before(history, entity, T0 + timedelta(seconds=25))
    assert selected is not None and selected.lifecycle is Lifecycle.DELETED
    assert live_object_version_at_or_before(history, entity, T0 + timedelta(seconds=25)) is None
    assert object_version_at_or_before(history, entity, T0 - timedelta(seconds=1)) is None


@pytest.mark.parametrize(
    ("kind", "expected"),
    [("PodChaos", True), ("NetworkChaos", True), ("Schedule", False), ("Workflow", False)],
)
def test_direct_fault_kind_allowlist(kind: str, expected: bool) -> None:
    assert is_direct_fault_object_kind(kind) is expected


def test_runtime_failure_episode_is_server_only_and_verified() -> None:
    history = _history({})
    episodes = derive_runtime_failure_workload_episodes(_runtime(), history=history)
    assert len(episodes) == 1
    assert episodes[0].binding_state.value == "VERIFIED"
    assert episodes[0].observed_non_success_spans == 2
    for span_kind, state in (
        (RuntimeSpanKind.CLIENT, RuntimeOutcomeState.ERROR),
        (RuntimeSpanKind.SERVER, RuntimeOutcomeState.SUCCESS),
        (RuntimeSpanKind.SERVER, RuntimeOutcomeState.UNKNOWN),
    ):
        summary = RuntimeServiceOutcomeSummary(
            service="payment",
            binding=_binding(),
            span_kind=span_kind,
            protocol=RuntimeProtocol.HTTP,
            state=state,
            first_seen=T0,
            last_seen=T0,
            observed_spans=1,
        )
        empty = RuntimeEvidence.empty()
        evidence = RuntimeEvidence(
            bindings=(),
            service_outcomes=(summary,),
            call_outcomes=(),
            stats=empty.stats.model_copy(update={"service_outcome_summaries": 1}),
        )
        assert derive_runtime_failure_workload_episodes(evidence, history=history) == ()


def test_exact_config_bridge_ignores_finding_related() -> None:
    deployment_body = {
        "spec": {"template": {"spec": {"volumes": [{"configMap": {"name": "payment-config"}}]}}}
    }
    history = _history(deployment_body)
    config = _ref("ConfigMap", "payment-config")
    history[config] = [_version(config)]
    finding = _finding(
        FindingKind.CONFIG_CHANGE,
        config,
    ).model_copy(update={"related": (_ref("Deployment", "other"),)})
    result = derive_runtime_mechanism_bridges((finding,), _runtime(), history=history)
    assert len(result.bridges) == 1
    bridge = result.bridges[0]
    assert bridge.relation is RuntimeMechanismRelation.CONFIGURES_DEPLOYMENT
    assert bridge.basis is RuntimeMechanismBridgeBasis.HISTORICAL_CONFIG_REFERENCE
    assert bridge.target_entity == _ref("Deployment", "payment")


def test_network_policy_and_fault_and_hpa_bridges() -> None:
    history = _history({})
    policy = _ref("NetworkPolicy", "payment-policy")
    chaos = _ref("PodChaos", "payment-chaos")
    hpa = _ref("HorizontalPodAutoscaler", "payment-hpa")
    history[policy] = [
        _version(policy, body={"spec": {"podSelector": {"matchLabels": {"app": "payment"}}}})
    ]
    history[chaos] = [
        _version(
            chaos,
            body={
                "spec": {"selector": {"namespaces": ["shop"], "labelSelectors": {"app": "payment"}}}
            },
        )
    ]
    history[hpa] = [
        _version(
            hpa,
            body={"spec": {"scaleTargetRef": {"kind": "Deployment", "name": "payment"}}},
        )
    ]
    findings = (
        _finding(FindingKind.NETWORK_RESTRICTION, policy, evidence="policy"),
        _finding(FindingKind.FAULT_INJECTION, chaos, evidence="chaos"),
        _finding(FindingKind.AUTOSCALING_FAILURE, hpa, evidence="hpa"),
    )
    result = derive_runtime_mechanism_bridges(findings, _runtime(), history=history)
    assert {item.relation for item in result.bridges} == {
        RuntimeMechanismRelation.NETWORK_POLICY_SELECTS_POD,
        RuntimeMechanismRelation.FAULT_SELECTOR_TARGETS_POD,
        RuntimeMechanismRelation.HPA_SCALES_DEPLOYMENT,
    }


def test_historical_selector_does_not_use_future_pod_labels() -> None:
    history = _history({})
    pod = _ref("Pod", "payment-abc")
    history[pod] = [
        _version(pod, 0, body={"metadata": {"labels": {"app": "old"}}}),
        _version(pod, 20, body={"metadata": {"labels": {"app": "payment"}}}),
    ]
    policy = _ref("NetworkPolicy", "payment-policy")
    history[policy] = [
        _version(policy, body={"spec": {"podSelector": {"matchLabels": {"app": "payment"}}}})
    ]
    finding = _finding(FindingKind.NETWORK_RESTRICTION, policy)
    result = derive_runtime_mechanism_bridges((finding,), _runtime(), history=history)
    assert result.bridges == ()

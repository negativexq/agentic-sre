"""Exact historical Kubernetes mechanism relationships to runtime failures.

This module is intentionally observational.  A bridge records an explicit
Kubernetes relationship between an existing mechanism finding and an exact
runtime-bound workload; it does not turn that relationship into causal or RCA
authority.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.rca.model import EntityRef, Finding, FindingKind, Lifecycle, ObjectVersion
from packages.rca.runtime_evidence import (
    RuntimeEvidence,
    RuntimeKubernetesBinding,
    RuntimeOutcomeState,
)
from packages.rca.runtime_graph import RuntimeSpanKind
from packages.rca.runtime_propagation import (
    RuntimeBindingVerificationState,
    verify_runtime_binding,
)


class RuntimeFailureWorkloadEpisode(BaseModel):
    model_config = ConfigDict(frozen=True)

    service: str = Field(min_length=1)
    binding: RuntimeKubernetesBinding
    binding_state: RuntimeBindingVerificationState
    first_failure_at: datetime
    last_failure_at: datetime
    observed_non_success_spans: int = Field(gt=0)
    outcome_states: tuple[str, ...] = ()
    protocols: tuple[str, ...] = ()
    protocol_codes: tuple[str, ...] = ()
    error_types: tuple[str, ...] = ()
    runtime_evidence_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _valid_episode(self) -> RuntimeFailureWorkloadEpisode:
        if self.first_failure_at > self.last_failure_at:
            raise ValueError("runtime failure episode has inverted range")
        if len(self.runtime_evidence_ids) > 32:
            raise ValueError("runtime failure provenance is unbounded")
        return self


class RuntimeMechanismRelation(StrEnum):
    CONFIGURES_DEPLOYMENT = "CONFIGURES_DEPLOYMENT"
    NETWORK_POLICY_SELECTS_POD = "NETWORK_POLICY_SELECTS_POD"
    FAULT_SELECTOR_TARGETS_POD = "FAULT_SELECTOR_TARGETS_POD"
    HPA_SCALES_DEPLOYMENT = "HPA_SCALES_DEPLOYMENT"


class RuntimeMechanismBridgeBasis(StrEnum):
    HISTORICAL_CONFIG_REFERENCE = "HISTORICAL_CONFIG_REFERENCE"
    HISTORICAL_NETWORK_POLICY_SELECTOR = "HISTORICAL_NETWORK_POLICY_SELECTOR"
    HISTORICAL_FAULT_SELECTOR = "HISTORICAL_FAULT_SELECTOR"
    HISTORICAL_HPA_SCALE_TARGET = "HISTORICAL_HPA_SCALE_TARGET"


class RuntimeMechanismBridge(BaseModel):
    model_config = ConfigDict(frozen=True)

    bridge_id: str = Field(min_length=1)
    mechanism_entity: EntityRef
    finding_kind: FindingKind
    relation: RuntimeMechanismRelation
    basis: RuntimeMechanismBridgeBasis
    affected_service: str = Field(min_length=1)
    affected_binding: RuntimeKubernetesBinding
    target_entity: EntityRef
    mechanism_finding_at: datetime | None = None
    first_runtime_failure_at: datetime
    last_runtime_failure_at: datetime
    mechanism_version_observed_at: datetime
    target_version_observed_at: datetime
    finding_evidence_ids: tuple[str, ...] = ()
    mechanism_object_evidence_ids: tuple[str, ...] = ()
    target_object_evidence_ids: tuple[str, ...] = ()
    runtime_evidence_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _valid_bridge(self) -> RuntimeMechanismBridge:
        if self.first_runtime_failure_at > self.last_runtime_failure_at:
            raise ValueError("bridge has inverted runtime failure range")
        if self.mechanism_version_observed_at > self.first_runtime_failure_at:
            raise ValueError("mechanism version is observed after runtime failure")
        if self.target_version_observed_at > self.first_runtime_failure_at:
            raise ValueError("target version is observed after runtime failure")
        if any(
            len(values) > 32
            for values in (
                self.finding_evidence_ids,
                self.mechanism_object_evidence_ids,
                self.target_object_evidence_ids,
                self.runtime_evidence_ids,
            )
        ):
            raise ValueError("bridge provenance is unbounded")
        expected_kind = {
            RuntimeMechanismRelation.CONFIGURES_DEPLOYMENT: "Deployment",
            RuntimeMechanismRelation.NETWORK_POLICY_SELECTS_POD: "Pod",
            RuntimeMechanismRelation.FAULT_SELECTOR_TARGETS_POD: "Pod",
            RuntimeMechanismRelation.HPA_SCALES_DEPLOYMENT: "Deployment",
        }[self.relation]
        if self.target_entity.kind != expected_kind:
            raise ValueError("bridge target kind does not match relation")
        expected_name = (
            self.affected_binding.deployment
            if expected_kind == "Deployment"
            else self.affected_binding.pod
        )
        if expected_name is None or self.target_entity != EntityRef(
            namespace=self.affected_binding.namespace,
            kind=expected_kind,
            name=expected_name,
        ):
            raise ValueError("bridge target is not the exact runtime binding target")
        return self


class RuntimeMechanismBridgeStats(BaseModel):
    model_config = ConfigDict(frozen=True)

    server_non_success_summaries: int = Field(ge=0)
    unbound_server_non_success_summaries: int = Field(ge=0)
    runtime_failure_episodes: int = Field(ge=0)
    verified_failure_episodes: int = Field(ge=0)
    unresolved_failure_episodes: int = Field(ge=0)
    contradicted_failure_episodes: int = Field(ge=0)
    mechanism_findings: int = Field(ge=0)
    config_change_findings: int = Field(ge=0)
    network_policy_findings: int = Field(ge=0)
    fault_injection_findings: int = Field(ge=0)
    autoscaling_failure_findings: int = Field(ge=0)
    config_bridges: int = Field(ge=0)
    network_policy_bridges: int = Field(ge=0)
    fault_bridges: int = Field(ge=0)
    hpa_bridges: int = Field(ge=0)
    mechanism_object_missing_or_deleted: int = Field(ge=0)
    target_object_missing_or_deleted: int = Field(ge=0)
    invalid_network_policy_selectors: int = Field(ge=0)
    unsupported_network_policy_selectors: int = Field(ge=0)
    invalid_fault_selectors: int = Field(ge=0)
    unsupported_fault_selectors: int = Field(ge=0)
    invalid_hpa_targets: int = Field(ge=0)
    unsupported_hpa_target_kinds: int = Field(ge=0)
    bridges: int = Field(ge=0)
    bridged_failure_episodes: int = Field(ge=0)
    distinct_mechanism_entities: int = Field(ge=0)
    distinct_target_entities: int = Field(ge=0)


class RuntimeMechanismBridges:
    """Immutable, deterministically ordered mechanism bridge observations."""

    __slots__ = ("failure_episodes", "bridges", "stats")

    def __init__(
        self,
        *,
        failure_episodes: Sequence[RuntimeFailureWorkloadEpisode],
        bridges: Sequence[RuntimeMechanismBridge],
        stats: RuntimeMechanismBridgeStats,
    ) -> None:
        self.failure_episodes = tuple(
            sorted(
                failure_episodes,
                key=lambda item: (
                    item.service,
                    item.binding.namespace,
                    item.binding.deployment or "",
                    item.binding.pod or "",
                ),
            )
        )
        self.bridges = tuple(
            sorted(
                bridges,
                key=lambda item: (
                    item.mechanism_entity.canonical,
                    item.finding_kind.value,
                    item.relation.value,
                    item.affected_service,
                    item.target_entity.canonical,
                    item.first_runtime_failure_at,
                    item.bridge_id,
                ),
            )
        )
        self.stats = stats

    @classmethod
    def empty(cls) -> RuntimeMechanismBridges:
        fields = {name: 0 for name in RuntimeMechanismBridgeStats.model_fields}
        return cls(failure_episodes=(), bridges=(), stats=RuntimeMechanismBridgeStats(**fields))

    def for_mechanism(self, entity: EntityRef) -> tuple[RuntimeMechanismBridge, ...]:
        return tuple(item for item in self.bridges if item.mechanism_entity == entity)

    def for_target(self, entity: EntityRef) -> tuple[RuntimeMechanismBridge, ...]:
        return tuple(item for item in self.bridges if item.target_entity == entity)

    def for_service(self, service: str) -> tuple[RuntimeMechanismBridge, ...]:
        return tuple(item for item in self.bridges if item.affected_service == service)


_MECHANISM_FINDING_KINDS = frozenset(
    {
        FindingKind.CONFIG_CHANGE,
        FindingKind.POLICY_CREATED,
        FindingKind.NETWORK_RESTRICTION,
        FindingKind.FAULT_INJECTION,
        FindingKind.AUTOSCALING_FAILURE,
    }
)
_CONFIG_KINDS = frozenset({"ConfigMap", "Secret"})
_POLICY_KINDS = frozenset({"POLICY_CREATED", "NETWORK_RESTRICTION"})


def _binding_key(binding: RuntimeKubernetesBinding) -> tuple[str, str, str]:
    return (binding.namespace, binding.deployment or "", binding.pod or "")


def _bounded_ids(
    values: Sequence[str],
    *,
    order: tuple[object, ...] = (),
) -> tuple[str, ...]:
    del order
    return tuple(sorted(set(values)))[:32]


class _EpisodeAccumulator:
    def __init__(self, service: str, binding: RuntimeKubernetesBinding) -> None:
        self.service = service
        self.binding = binding
        self.first: datetime | None = None
        self.last: datetime | None = None
        self.spans = 0
        self.states: set[str] = set()
        self.protocols: set[str] = set()
        self.codes: set[str] = set()
        self.errors: set[str] = set()
        self.evidence: list[tuple[tuple[object, ...], str]] = []

    def add(self, summary: Any) -> None:
        self.first = (
            summary.first_seen if self.first is None else min(self.first, summary.first_seen)
        )
        self.last = summary.last_seen if self.last is None else max(self.last, summary.last_seen)
        self.spans += summary.observed_spans
        self.states.add(summary.state.value)
        self.protocols.add(summary.protocol.value)
        if summary.protocol_code:
            self.codes.add(f"{summary.protocol.value}:{summary.protocol_code}")
        if summary.error_type:
            self.errors.add(summary.error_type)
        order = (
            summary.first_seen,
            summary.service,
            summary.binding.namespace,
            summary.binding.deployment or "",
            summary.binding.pod or "",
        )
        for evidence_id in summary.evidence_ids:
            if not any(value == evidence_id for _key, value in self.evidence):
                self.evidence.append((order, evidence_id))

    def episode(self, state: RuntimeBindingVerificationState) -> RuntimeFailureWorkloadEpisode:
        assert self.first is not None and self.last is not None
        evidence = tuple(
            value for _key, value in sorted(self.evidence, key=lambda x: (x[0], x[1]))
        )[:32]
        return RuntimeFailureWorkloadEpisode(
            service=self.service,
            binding=self.binding,
            binding_state=state,
            first_failure_at=self.first,
            last_failure_at=self.last,
            observed_non_success_spans=self.spans,
            outcome_states=tuple(sorted(self.states)),
            protocols=tuple(sorted(self.protocols)),
            protocol_codes=tuple(sorted(self.codes)),
            error_types=tuple(sorted(self.errors)),
            runtime_evidence_ids=evidence,
        )


def derive_runtime_failure_workload_episodes(
    runtime_evidence: RuntimeEvidence,
    *,
    history: Mapping[EntityRef, Sequence[ObjectVersion]],
) -> tuple[RuntimeFailureWorkloadEpisode, ...]:
    accumulators: dict[tuple[str, str, str, str], _EpisodeAccumulator] = {}
    for summary in runtime_evidence.service_outcomes:
        if summary.span_kind is not RuntimeSpanKind.SERVER:
            continue
        if summary.state not in {RuntimeOutcomeState.NON_OK, RuntimeOutcomeState.ERROR}:
            continue
        if summary.binding is None:
            continue
        key = (summary.service, *_binding_key(summary.binding))
        accumulator = accumulators.setdefault(
            key, _EpisodeAccumulator(summary.service, summary.binding)
        )
        accumulator.add(summary)
    episodes: list[RuntimeFailureWorkloadEpisode] = []
    for accumulator in accumulators.values():
        assert accumulator.first is not None
        verification = verify_runtime_binding(
            accumulator.binding,
            at=accumulator.first,
            history=history,
        )
        assert verification is not None
        episodes.append(accumulator.episode(verification.state))
    return tuple(
        sorted(
            episodes,
            key=lambda item: (
                item.service,
                item.binding.namespace,
                item.binding.deployment or "",
                item.binding.pod or "",
            ),
        )
    )


def object_version_at_or_before(
    history: Mapping[EntityRef, Sequence[ObjectVersion]],
    entity: EntityRef,
    at: datetime,
) -> ObjectVersion | None:
    eligible = [item for item in history.get(entity, ()) if item.observed_at <= at]
    if not eligible:
        return None
    return max(eligible, key=lambda item: (item.observed_at, item.evidence_id))


def live_object_version_at_or_before(
    history: Mapping[EntityRef, Sequence[ObjectVersion]],
    entity: EntityRef,
    at: datetime,
) -> ObjectVersion | None:
    version = object_version_at_or_before(history, entity, at)
    return None if version is None or version.lifecycle is Lifecycle.DELETED else version


def _as_mapping(value: object) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _named(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def workload_config_references(body: Mapping[str, Any]) -> frozenset[tuple[str, str]]:
    spec = _as_mapping(body.get("spec"))
    template = _as_mapping(spec.get("template")) if spec is not None else None
    pod_spec = _as_mapping(template.get("spec")) if template is not None else None
    if pod_spec is None:
        return frozenset()
    refs: set[tuple[str, str]] = set()

    def add(kind: str, value: object) -> None:
        name = _named(value)
        if name is not None:
            refs.add((kind, name))

    volumes = pod_spec.get("volumes")
    if isinstance(volumes, Sequence) and not isinstance(volumes, (str, bytes)):
        for volume in volumes:
            item = _as_mapping(volume)
            if item is None:
                continue
            config_map = _as_mapping(item.get("configMap"))
            secret = _as_mapping(item.get("secret"))
            if config_map is not None:
                add("ConfigMap", config_map.get("name"))
            if secret is not None:
                add("Secret", secret.get("secretName"))
            projected = _as_mapping(item.get("projected"))
            sources = projected.get("sources") if projected is not None else None
            if isinstance(sources, Sequence) and not isinstance(sources, (str, bytes)):
                for source in sources:
                    source_map = _as_mapping(source)
                    if source_map is None:
                        continue
                    source_config = _as_mapping(source_map.get("configMap"))
                    source_secret = _as_mapping(source_map.get("secret"))
                    if source_config is not None:
                        add("ConfigMap", source_config.get("name"))
                    if source_secret is not None:
                        add("Secret", source_secret.get("name"))

    for field in ("containers", "initContainers"):
        containers = pod_spec.get(field)
        if not isinstance(containers, Sequence) or isinstance(containers, (str, bytes)):
            continue
        for container in containers:
            container_map = _as_mapping(container)
            if container_map is None:
                continue
            env_from = container_map.get("envFrom")
            if isinstance(env_from, Sequence) and not isinstance(env_from, (str, bytes)):
                for source in env_from:
                    source_map = _as_mapping(source)
                    if source_map is None:
                        continue
                    config_ref = _as_mapping(source_map.get("configMapRef"))
                    secret_ref = _as_mapping(source_map.get("secretRef"))
                    if config_ref is not None:
                        add("ConfigMap", config_ref.get("name"))
                    if secret_ref is not None:
                        add("Secret", secret_ref.get("name"))
            env = container_map.get("env")
            if isinstance(env, Sequence) and not isinstance(env, (str, bytes)):
                for variable in env:
                    variable_map = _as_mapping(variable)
                    value_from = (
                        _as_mapping(variable_map.get("valueFrom"))
                        if variable_map is not None
                        else None
                    )
                    if value_from is None:
                        continue
                    config_ref = _as_mapping(value_from.get("configMapKeyRef"))
                    secret_ref = _as_mapping(value_from.get("secretKeyRef"))
                    if config_ref is not None:
                        add("ConfigMap", config_ref.get("name"))
                    if secret_ref is not None:
                        add("Secret", secret_ref.get("name"))
    return frozenset(refs)


def is_direct_fault_object_kind(kind: str) -> bool:
    return kind.endswith("Chaos") and kind not in {"Schedule", "Workflow"}


def _pod_labels(body: Mapping[str, Any]) -> Mapping[str, Any]:
    metadata = _as_mapping(body.get("metadata"))
    labels = _as_mapping(metadata.get("labels")) if metadata is not None else None
    return labels or {}


def _exact_labels_match(selector: Mapping[str, Any], labels: Mapping[str, Any]) -> bool:
    return all(
        key in labels and isinstance(key, str) and str(labels[key]) == str(value)
        for key, value in selector.items()
    )


def _selector_match_labels(
    selector: object,
) -> tuple[bool, bool, bool]:
    """Return (valid, unsupported, matchLabels)."""
    selector_map = _as_mapping(selector)
    if selector_map is None:
        return False, False, False
    expressions = selector_map.get("matchExpressions")
    if expressions:
        return True, True, False
    match_labels = selector_map.get("matchLabels", {})
    if not isinstance(match_labels, Mapping):
        return False, False, False
    return True, False, _exact_labels_match(match_labels, {})


def _fault_selector(
    selector: object,
) -> tuple[bool, bool, tuple[str, ...], Mapping[str, Any] | None]:
    selector_map = _as_mapping(selector)
    if selector_map is None:
        return False, False, (), None
    namespaces = selector_map.get("namespaces")
    labels = selector_map.get("labelSelectors")
    if (
        not isinstance(namespaces, Sequence)
        or isinstance(namespaces, (str, bytes))
        or not namespaces
        or any(_named(value) is None for value in namespaces)
        or not isinstance(labels, Mapping)
    ):
        return False, False, (), None
    unsupported_keys = set(selector_map) - {"namespaces", "labelSelectors"}
    normalized_namespaces = tuple(
        value for value in (_named(item) for item in namespaces) if value is not None
    )
    return True, bool(unsupported_keys), normalized_namespaces, labels


def _scale_target(value: object) -> tuple[str, str] | None:
    target = _as_mapping(value)
    if target is None:
        return None
    kind = _named(target.get("kind"))
    name = _named(target.get("name"))
    return (kind, name) if kind is not None and name is not None else None


def _finding_key(finding: Finding) -> str:
    return json.dumps(finding.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def _bridge_id(
    finding: Finding,
    relation: RuntimeMechanismRelation,
    episode: RuntimeFailureWorkloadEpisode,
    target: EntityRef,
) -> str:
    material = "\n".join(
        (
            finding.entity.canonical,
            finding.kind.value,
            *sorted(finding.evidence_ids),
            relation.value,
            episode.service,
            episode.binding.namespace,
            episode.binding.deployment or "",
            episode.binding.pod or "",
            target.canonical,
            episode.first_failure_at.isoformat(),
        )
    ).encode()
    return f"mechanism-bridge:{hashlib.sha256(material).hexdigest()[:16]}"


def _episode_counts(episodes: Sequence[RuntimeFailureWorkloadEpisode]) -> Counter[str]:
    return Counter(item.binding_state.value for item in episodes)


def _valid_mechanism_finding(finding: Finding) -> bool:
    if finding.kind is FindingKind.CONFIG_CHANGE:
        return finding.entity.kind in _CONFIG_KINDS
    if finding.kind in {FindingKind.POLICY_CREATED, FindingKind.NETWORK_RESTRICTION}:
        return finding.entity.kind == "NetworkPolicy"
    if finding.kind is FindingKind.FAULT_INJECTION:
        return is_direct_fault_object_kind(finding.entity.kind)
    if finding.kind is FindingKind.AUTOSCALING_FAILURE:
        return finding.entity.kind == "HorizontalPodAutoscaler"
    return False


def derive_runtime_mechanism_bridges(
    findings: Sequence[Finding],
    runtime_evidence: RuntimeEvidence,
    *,
    history: Mapping[EntityRef, Sequence[ObjectVersion]],
) -> RuntimeMechanismBridges:
    episodes = derive_runtime_failure_workload_episodes(runtime_evidence, history=history)
    server_summaries = sum(
        1
        for item in runtime_evidence.service_outcomes
        if item.span_kind is RuntimeSpanKind.SERVER
        and item.state in {RuntimeOutcomeState.NON_OK, RuntimeOutcomeState.ERROR}
    )
    unbound_summaries = sum(
        1
        for item in runtime_evidence.service_outcomes
        if item.span_kind is RuntimeSpanKind.SERVER
        and item.state in {RuntimeOutcomeState.NON_OK, RuntimeOutcomeState.ERROR}
        and item.binding is None
    )
    unique_findings: dict[str, Finding] = {}
    for finding in findings:
        if finding.kind in _MECHANISM_FINDING_KINDS and _valid_mechanism_finding(finding):
            unique_findings.setdefault(_finding_key(finding), finding)
    mechanism_findings = tuple(
        sorted(
            unique_findings.values(),
            key=lambda item: (
                item.entity.canonical,
                item.kind.value,
                item.at.isoformat() if item.at is not None else "",
            ),
        )
    )
    bridge_by_id: dict[str, RuntimeMechanismBridge] = {}
    rejection = Counter[str]()

    def add_bridge(
        finding: Finding,
        episode: RuntimeFailureWorkloadEpisode,
        relation: RuntimeMechanismRelation,
        basis: RuntimeMechanismBridgeBasis,
        mechanism: ObjectVersion,
        target: ObjectVersion,
        target_entity: EntityRef,
    ) -> None:
        bridge = RuntimeMechanismBridge(
            bridge_id=_bridge_id(finding, relation, episode, target_entity),
            mechanism_entity=finding.entity,
            finding_kind=finding.kind,
            relation=relation,
            basis=basis,
            affected_service=episode.service,
            affected_binding=episode.binding,
            target_entity=target_entity,
            mechanism_finding_at=finding.at,
            first_runtime_failure_at=episode.first_failure_at,
            last_runtime_failure_at=episode.last_failure_at,
            mechanism_version_observed_at=mechanism.observed_at,
            target_version_observed_at=target.observed_at,
            finding_evidence_ids=_bounded_ids(finding.evidence_ids),
            mechanism_object_evidence_ids=(mechanism.evidence_id,),
            target_object_evidence_ids=(target.evidence_id,),
            runtime_evidence_ids=episode.runtime_evidence_ids,
        )
        bridge_by_id.setdefault(bridge.bridge_id, bridge)

    for episode in episodes:
        if episode.binding_state is not RuntimeBindingVerificationState.VERIFIED:
            continue
        deployment_entity = (
            EntityRef(
                namespace=episode.binding.namespace,
                kind="Deployment",
                name=episode.binding.deployment,
            )
            if episode.binding.deployment is not None
            else None
        )
        pod_entity = (
            EntityRef(namespace=episode.binding.namespace, kind="Pod", name=episode.binding.pod)
            if episode.binding.pod is not None
            else None
        )
        for finding in mechanism_findings:
            mechanism = live_object_version_at_or_before(
                history, finding.entity, episode.first_failure_at
            )
            if mechanism is None:
                rejection["mechanism_object_missing_or_deleted"] += 1
                continue
            if finding.kind is FindingKind.CONFIG_CHANGE:
                if deployment_entity is None:
                    continue
                target = live_object_version_at_or_before(
                    history, deployment_entity, episode.first_failure_at
                )
                if target is None:
                    rejection["target_object_missing_or_deleted"] += 1
                    continue
                if finding.entity.namespace != deployment_entity.namespace:
                    continue
                if (finding.entity.kind, finding.entity.name) not in workload_config_references(
                    target.body
                ):
                    continue
                add_bridge(
                    finding,
                    episode,
                    RuntimeMechanismRelation.CONFIGURES_DEPLOYMENT,
                    RuntimeMechanismBridgeBasis.HISTORICAL_CONFIG_REFERENCE,
                    mechanism,
                    target,
                    deployment_entity,
                )
            elif finding.kind in {FindingKind.POLICY_CREATED, FindingKind.NETWORK_RESTRICTION}:
                if pod_entity is None:
                    continue
                target = live_object_version_at_or_before(
                    history, pod_entity, episode.first_failure_at
                )
                if target is None:
                    rejection["target_object_missing_or_deleted"] += 1
                    continue
                if finding.entity.namespace != pod_entity.namespace:
                    continue
                spec = _as_mapping(mechanism.body.get("spec"))
                selector = spec.get("podSelector") if spec is not None else None
                valid, unsupported, _ = _selector_match_labels(selector)
                if not valid:
                    rejection["invalid_network_policy_selectors"] += 1
                    continue
                if unsupported:
                    rejection["unsupported_network_policy_selectors"] += 1
                    continue
                selector_map = _as_mapping(selector)
                assert selector_map is not None
                labels = selector_map.get("matchLabels", {})
                if not isinstance(labels, Mapping) or not _exact_labels_match(
                    labels, _pod_labels(target.body)
                ):
                    continue
                add_bridge(
                    finding,
                    episode,
                    RuntimeMechanismRelation.NETWORK_POLICY_SELECTS_POD,
                    RuntimeMechanismBridgeBasis.HISTORICAL_NETWORK_POLICY_SELECTOR,
                    mechanism,
                    target,
                    pod_entity,
                )
            elif finding.kind is FindingKind.FAULT_INJECTION:
                if pod_entity is None:
                    continue
                target = live_object_version_at_or_before(
                    history, pod_entity, episode.first_failure_at
                )
                if target is None:
                    rejection["target_object_missing_or_deleted"] += 1
                    continue
                selector = (
                    _as_mapping(mechanism.body.get("spec", {}).get("selector"))
                    if isinstance(mechanism.body.get("spec"), Mapping)
                    else None
                )
                valid, unsupported, namespaces, labels = _fault_selector(selector)
                if not valid:
                    rejection["invalid_fault_selectors"] += 1
                    continue
                if unsupported:
                    rejection["unsupported_fault_selectors"] += 1
                    continue
                assert labels is not None
                if pod_entity.namespace not in namespaces or not _exact_labels_match(
                    labels, _pod_labels(target.body)
                ):
                    continue
                add_bridge(
                    finding,
                    episode,
                    RuntimeMechanismRelation.FAULT_SELECTOR_TARGETS_POD,
                    RuntimeMechanismBridgeBasis.HISTORICAL_FAULT_SELECTOR,
                    mechanism,
                    target,
                    pod_entity,
                )
            elif finding.kind is FindingKind.AUTOSCALING_FAILURE:
                if deployment_entity is None:
                    continue
                target = live_object_version_at_or_before(
                    history, deployment_entity, episode.first_failure_at
                )
                if target is None:
                    rejection["target_object_missing_or_deleted"] += 1
                    continue
                spec = _as_mapping(mechanism.body.get("spec"))
                scale_target = _scale_target(spec.get("scaleTargetRef") if spec else None)
                if scale_target is None:
                    rejection["invalid_hpa_targets"] += 1
                    continue
                kind, name = scale_target
                if kind != "Deployment":
                    rejection["unsupported_hpa_target_kinds"] += 1
                    continue
                if (
                    EntityRef(namespace=finding.entity.namespace, kind=kind, name=name)
                    != deployment_entity
                ):
                    continue
                add_bridge(
                    finding,
                    episode,
                    RuntimeMechanismRelation.HPA_SCALES_DEPLOYMENT,
                    RuntimeMechanismBridgeBasis.HISTORICAL_HPA_SCALE_TARGET,
                    mechanism,
                    target,
                    deployment_entity,
                )

    bridges = tuple(bridge_by_id.values())
    states = _episode_counts(episodes)
    relation_counts = Counter(item.relation for item in bridges)
    stats = RuntimeMechanismBridgeStats(
        server_non_success_summaries=server_summaries,
        unbound_server_non_success_summaries=unbound_summaries,
        runtime_failure_episodes=len(episodes),
        verified_failure_episodes=states[RuntimeBindingVerificationState.VERIFIED.value],
        unresolved_failure_episodes=states[RuntimeBindingVerificationState.UNRESOLVED.value],
        contradicted_failure_episodes=states[RuntimeBindingVerificationState.CONTRADICTED.value],
        mechanism_findings=len(mechanism_findings),
        config_change_findings=sum(
            item.kind is FindingKind.CONFIG_CHANGE for item in mechanism_findings
        ),
        network_policy_findings=sum(
            item.kind in {FindingKind.POLICY_CREATED, FindingKind.NETWORK_RESTRICTION}
            for item in mechanism_findings
        ),
        fault_injection_findings=sum(
            item.kind is FindingKind.FAULT_INJECTION for item in mechanism_findings
        ),
        autoscaling_failure_findings=sum(
            item.kind is FindingKind.AUTOSCALING_FAILURE for item in mechanism_findings
        ),
        config_bridges=relation_counts[RuntimeMechanismRelation.CONFIGURES_DEPLOYMENT],
        network_policy_bridges=relation_counts[RuntimeMechanismRelation.NETWORK_POLICY_SELECTS_POD],
        fault_bridges=relation_counts[RuntimeMechanismRelation.FAULT_SELECTOR_TARGETS_POD],
        hpa_bridges=relation_counts[RuntimeMechanismRelation.HPA_SCALES_DEPLOYMENT],
        mechanism_object_missing_or_deleted=rejection["mechanism_object_missing_or_deleted"],
        target_object_missing_or_deleted=rejection["target_object_missing_or_deleted"],
        invalid_network_policy_selectors=rejection["invalid_network_policy_selectors"],
        unsupported_network_policy_selectors=rejection["unsupported_network_policy_selectors"],
        invalid_fault_selectors=rejection["invalid_fault_selectors"],
        unsupported_fault_selectors=rejection["unsupported_fault_selectors"],
        invalid_hpa_targets=rejection["invalid_hpa_targets"],
        unsupported_hpa_target_kinds=rejection["unsupported_hpa_target_kinds"],
        bridges=len(bridges),
        bridged_failure_episodes=len(
            {
                (
                    item.affected_service,
                    item.affected_binding.namespace,
                    item.affected_binding.deployment or "",
                    item.affected_binding.pod or "",
                )
                for item in bridges
            }
        ),
        distinct_mechanism_entities=len({item.mechanism_entity for item in bridges}),
        distinct_target_entities=len({item.target_entity for item in bridges}),
    )
    return RuntimeMechanismBridges(failure_episodes=episodes, bridges=bridges, stats=stats)


__all__ = [
    "RuntimeFailureWorkloadEpisode",
    "RuntimeMechanismRelation",
    "RuntimeMechanismBridgeBasis",
    "RuntimeMechanismBridge",
    "RuntimeMechanismBridgeStats",
    "RuntimeMechanismBridges",
    "derive_runtime_failure_workload_episodes",
    "object_version_at_or_before",
    "live_object_version_at_or_before",
    "workload_config_references",
    "is_direct_fault_object_kind",
    "derive_runtime_mechanism_bridges",
]

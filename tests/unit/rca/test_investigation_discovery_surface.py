from __future__ import annotations

from collections.abc import Sequence

from rca_builders import at, deployment, event, ref, service, version

from packages.rca.engine import EngineConfig, build_case, diagnose_case
from packages.rca.information_gap import (
    _capability_allows_target,
    authorized_event_namespaces,
    incident_namespaces,
)
from packages.rca.investigation.candidates import (
    build_observation_candidates,
    resolve_effective_query,
)
from packages.rca.investigation.environment import (
    SourceInvestigationBackend,
    initial_view,
)
from packages.rca.investigation.evidence import (
    InMemoryEvidenceStore,
    OverlayObservationSource,
    records_from_observation,
)
from packages.rca.investigation.tools import IncidentChangesTool, IncidentEventsTool
from packages.rca.model import (
    Alert,
    ClusterEvent,
    EntityRef,
    FindingKind,
    GapResolvability,
    InformationGapOrigin,
    InvestigationObservation,
    InvestigationQuery,
    ObjectVersion,
)
from packages.rca.source import InMemorySource


def _source(
    *,
    events: Sequence[ClusterEvent] = (),
    versions: Sequence[ObjectVersion] = (),
) -> InMemorySource:
    return InMemorySource(
        name="discovery-surface",
        alert_items=[Alert(name="Latency", service="checkout", namespace="shop", starts_at=at(0))],
        versions=[
            version("shop/Service/checkout", 0, service("checkout")),
            *versions,
        ],
        event_items=list(events),
        traffic_items=[],
        cutoff=at(60),
    )


def _overlay(
    source: InMemorySource, observation: InvestigationObservation
) -> OverlayObservationSource:
    store = InMemoryEvidenceStore()
    records = records_from_observation(observation)
    result = store.put_many(records)
    overlay = OverlayObservationSource(
        base=initial_view(source),
        store=store,
        acquired_evidence_refs=result.new_refs,
        supported_capabilities=frozenset(
            {
                "history",
                "events",
                "incident_events",
                "incident_changes",
                "traffic",
            }
        ),
    )
    return overlay


def test_bounded_case_exposes_discovery_gap_contracts() -> None:
    source = _source()
    view = initial_view(source)
    case = build_case(view)
    diagnosis = diagnose_case(case)

    assert incident_namespaces(case) == ("shop",)
    discovery = [
        gap for gap in diagnosis.information_gaps if gap.origin is InformationGapOrigin.DISCOVERY
    ]
    assert {gap.dimension.value for gap in discovery} == {
        "EVENT_SEQUENCE",
        "CHANGE_TIMING",
    }
    assert {query.capability for gap in discovery for query in gap.authorized_queries} == {
        "incident_events",
        "incident_changes",
    }
    assert all(gap.resolvability is GapResolvability.RESOLVABLE for gap in discovery)
    candidates = build_observation_candidates(
        case=case, diagnosis=diagnosis, engine_config=EngineConfig()
    )
    assert {candidate.capability for candidate in candidates} >= {
        "incident_events",
        "incident_changes",
    }
    assert all(candidate.query is not None for candidate in candidates)


def test_discovery_capabilities_are_scope_restricted() -> None:
    namespace = ref("_cluster/Namespace/shop")
    deployment_actor = ref("shop/Deployment/checkout")
    pod = ref("shop/Pod/checkout-0")
    service_entity = ref("shop/Service/checkout")

    assert _capability_allows_target("incident_events", namespace)
    assert not _capability_allows_target("incident_events", deployment_actor)
    assert _capability_allows_target("incident_changes", namespace)
    assert not _capability_allows_target("incident_changes", pod)
    assert _capability_allows_target("traffic", service_entity)
    assert not _capability_allows_target("traffic", namespace)


def test_auxiliary_event_scope_is_empty_and_deterministic() -> None:
    source = _source()
    case = build_case(initial_view(source))

    assert EngineConfig().auxiliary_event_namespaces == ()
    assert authorized_event_namespaces(case, EngineConfig()) == ("shop",)
    assert authorized_event_namespaces(
        case,
        EngineConfig(auxiliary_event_namespaces=("zeta", "", "shop", "alpha", "alpha")),
    ) == ("alpha", "shop", "zeta")


def test_auxiliary_event_scope_is_bounded_and_capability_specific() -> None:
    source = _source()
    case = build_case(initial_view(source))
    config = EngineConfig(
        auxiliary_event_namespaces=("zeta", "beta", "delta", "alpha", "zz-overflow")
    )
    assert config.auxiliary_event_namespaces == ("alpha", "beta", "delta", "zeta")
    assert authorized_event_namespaces(case, config) == (
        "alpha",
        "beta",
        "delta",
        "shop",
        "zeta",
    )
    diagnosis = diagnose_case(case, config=config)
    discovery = [
        gap for gap in diagnosis.information_gaps if gap.origin is InformationGapOrigin.DISCOVERY
    ]
    event_targets = {
        query.target.canonical
        for gap in discovery
        for query in gap.authorized_queries
        if query.capability == "incident_events"
    }
    change_targets = {
        query.target.canonical
        for gap in discovery
        for query in gap.authorized_queries
        if query.capability == "incident_changes"
    }
    assert event_targets == {
        "_cluster/Namespace/alpha",
        "_cluster/Namespace/beta",
        "_cluster/Namespace/delta",
        "_cluster/Namespace/shop",
        "_cluster/Namespace/zeta",
    }
    assert change_targets == {"_cluster/Namespace/shop"}


def test_incident_backend_is_namespace_scoped_and_bounded() -> None:
    shop_events = tuple(
        event("shop/Pod/checkout-0", f"Reason-{index}", index) for index in range(-5, 61)
    )
    other_event = event("payments/Pod/payment-0", "Other", 1)
    versions = [
        version("shop/ConfigMap/checkout", minute, {"data": {"key": str(minute)}})
        for minute in range(-2, 3)
    ] + [version("shop/Secret/checkout", 0, {"data": {"token": "hidden"}})]
    backend = SourceInvestigationBackend(
        _source(events=(*shop_events, other_event), versions=versions)
    )
    query = InvestigationQuery(start=at(-5), end=at(60), limit=64)

    returned_events = backend.query_incident_events(EntityRef(kind="Namespace", name="shop"), query)
    assert len(returned_events) == 64
    assert all(item.entity.namespace == "shop" for item in returned_events)

    returned_versions = backend.query_incident_changes(
        EntityRef(kind="Namespace", name="shop"), query
    )
    assert len(returned_versions) <= 64
    assert all(item.entity.namespace == "shop" for item in returned_versions)
    assert all(item.entity.kind != "Secret" for item in returned_versions)
    assert sum(item.entity.kind == "ConfigMap" for item in returned_versions) <= 4


def test_incident_event_projection_preserves_diagnostic_groups() -> None:
    noisy = tuple(event("shop/Pod/noise", "Repeated", minute) for minute in range(-5, 70))
    chaos = (
        event("shop/StressChaos/checkout", "Applied", 10),
        event("shop/StressChaos/checkout", "Failed", 11),
        event("shop/StressChaos/checkout", "Spawned", 12),
    )
    hpa = event(
        "shop/HorizontalPodAutoscaler/checkout",
        "FailedGetResourceMetric",
        13,
        type_="Warning",
    )
    quota = event(
        "shop/Pod/checkout-0",
        "FailedCreate",
        14,
        type_="Warning",
        message="exceeded quota: pods",
    )
    backend = SourceInvestigationBackend(_source(events=(*noisy, *chaos, hpa, quota)))
    query = InvestigationQuery(start=at(-5), end=at(60), limit=64)

    returned = backend.query_incident_events(EntityRef(kind="Namespace", name="shop"), query)
    assert len(returned) <= 64
    assert {item.reason for item in returned if item.entity.kind == "StressChaos"} >= {
        "Applied",
        "Failed",
        "Spawned",
    }
    assert any(item.reason == "FailedGetResourceMetric" for item in returned)
    assert any("exceeded quota" in item.message for item in returned)


def test_incident_event_projection_keeps_warning_groups_and_is_stable() -> None:
    first_group = tuple(
        event("shop/Pod/checkout-0", "FailedMount", minute, type_="Warning")
        for minute in range(-5, 45)
    )
    second_group = (
        event("shop/Pod/checkout-1", "BackOff", 45, type_="Warning"),
        event("shop/Pod/checkout-1", "BackOff", 46, type_="Warning"),
    )
    query = InvestigationQuery(start=at(-5), end=at(60), limit=64)
    forward = SourceInvestigationBackend(
        _source(events=(*first_group, *second_group))
    ).query_incident_events(EntityRef(kind="Namespace", name="shop"), query)
    reverse = SourceInvestigationBackend(
        _source(events=(*reversed(first_group), *reversed(second_group)))
    ).query_incident_events(EntityRef(kind="Namespace", name="shop"), query)
    assert [item.evidence_id for item in forward] == [item.evidence_id for item in reverse]
    assert {item.reason for item in forward} >= {"FailedMount", "BackOff"}


def test_incident_event_projection_preserves_fault_reasons_before_lifecycle_noise() -> None:
    noisy = tuple(
        event(f"shop/StressChaos/actor-{index}", "Updated", index) for index in range(100)
    )
    applied = event("shop/StressChaos/actor-99", "Applied", 59)
    query = InvestigationQuery(start=at(-5), end=at(120), limit=64)
    returned = SourceInvestigationBackend(_source(events=(*noisy, applied))).query_incident_events(
        EntityRef(kind="Namespace", name="shop"), query
    )
    assert len(returned) <= 64
    assert any(item.reason == "Applied" for item in returned)


def test_incident_events_rebuilds_existing_failure_and_fault_findings() -> None:
    chaos = ref("shop/StressChaos/checkout-stress")
    pod = ref("shop/Pod/checkout-0")
    source = _source(
        versions=[
            version("shop/Pod/checkout-0", 0, {}),
        ],
        events=(
            event("shop/StressChaos/checkout-stress", "Applied", 20),
            event("shop/Pod/checkout-0", "Failed", 21, type_="Warning", message="container failed"),
        ),
    )
    case = build_case(initial_view(source))
    assert not any(finding.kind is FindingKind.FAULT_INJECTION for finding in case.findings)
    gap = next(
        gap
        for gap in diagnose_case(case).information_gaps
        if gap.origin is InformationGapOrigin.DISCOVERY and "incident_events" in gap.candidate_tools
    )
    query = resolve_effective_query(
        capability="incident_events", case=case, engine_config=EngineConfig()
    )
    assert query is not None and query.limit == 64
    observation = IncidentEventsTool(SourceInvestigationBackend(source)).execute_query(
        case, gap, EntityRef(kind="Namespace", name="shop"), query
    )
    assert {record.evidence_id for record in records_from_observation(observation)}
    rebuilt = build_case(_overlay(source, observation))
    assert any(
        finding.kind is FindingKind.FAULT_INJECTION and finding.entity == chaos
        for finding in rebuilt.findings
    )
    assert any(
        finding.kind is FindingKind.FAILURE_EVENT and finding.entity == pod
        for finding in rebuilt.findings
    )


def test_incident_changes_rebuilds_config_change_and_rollout_restart() -> None:
    config = ref("shop/ConfigMap/checkout-config")
    deployment_actor = ref("shop/Deployment/checkout")
    source = _source(
        versions=(
            version("shop/ConfigMap/checkout-config", -1, {"data": {"mode": "old"}}),
            version("shop/ConfigMap/checkout-config", 1, {"data": {"mode": "new"}}),
            version("shop/Deployment/checkout", -1, deployment("checkout")),
            version(
                "shop/Deployment/checkout",
                1,
                deployment("checkout", restarted="now"),
            ),
        )
    )
    case = build_case(initial_view(source))
    gap = next(
        gap
        for gap in diagnose_case(case).information_gaps
        if gap.origin is InformationGapOrigin.DISCOVERY
        and "incident_changes" in gap.candidate_tools
    )
    query = resolve_effective_query(
        capability="incident_changes", case=case, engine_config=EngineConfig()
    )
    assert query is not None and query.limit == 64
    observation = IncidentChangesTool(SourceInvestigationBackend(source)).execute_query(
        case, gap, EntityRef(kind="Namespace", name="shop"), query
    )
    rebuilt = build_case(_overlay(source, observation))
    assert any(
        finding.kind is FindingKind.CONFIG_CHANGE and finding.entity == config
        for finding in rebuilt.findings
    )
    assert any(
        finding.kind is FindingKind.ROLLOUT_RESTART and finding.entity == deployment_actor
        for finding in rebuilt.findings
    )


def test_discovery_no_data_is_raw_neutral() -> None:
    source = _source()
    case = build_case(initial_view(source))
    gap = next(
        gap
        for gap in diagnose_case(case).information_gaps
        if gap.origin is InformationGapOrigin.DISCOVERY and "incident_events" in gap.candidate_tools
    )
    query = InvestigationQuery(start=at(-10), end=at(10), limit=64)
    observation = IncidentEventsTool(SourceInvestigationBackend(source)).execute_query(
        case, gap, EntityRef(kind="Namespace", name="shop"), query
    )
    assert observation.evidence_refs == ()
    assert observation.outcome.value == "NO_DATA"
    assert records_from_observation(observation) == ()

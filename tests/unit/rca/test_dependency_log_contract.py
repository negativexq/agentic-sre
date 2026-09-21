from __future__ import annotations

from datetime import UTC, datetime

from packages.rca.causal_roles import HypothesisCausalRoles
from packages.rca.engine import Case, EngineConfig, build_case, diagnose_case
from packages.rca.information_gap import InformationGapContext, derive_information_gaps
from packages.rca.investigation.candidates import build_observation_candidates
from packages.rca.investigation.environment import SourceInvestigationBackend, initial_view
from packages.rca.investigation.evidence import (
    InMemoryEvidenceStore,
    OverlayObservationSource,
    records_from_observation,
)
from packages.rca.investigation.tools import LogsTool
from packages.rca.mechanism_bridge import RuntimeMechanismBridges
from packages.rca.model import (
    Alert,
    AuthorizedQuery,
    Diagnosis,
    Edge,
    EntityRef,
    GapDimension,
    GapOutcomeKind,
    InformationGap,
    InvestigationQuery,
    Lifecycle,
    LogRecord,
    ObjectVersion,
    Resolution,
    ResolutionTrace,
    StructuralAlternative,
)
from packages.rca.root_cause_eligibility import RootCauseEligibilities
from packages.rca.runtime_propagation import RuntimePropagation
from packages.rca.source import InMemorySource
from packages.rca.topology import Topology


def _ref(kind: str, name: str) -> EntityRef:
    return EntityRef(namespace="shop", kind=kind, name=name)


def _context(topology: Topology, caller: EntityRef) -> InformationGapContext:
    return InformationGapContext(
        causal_roles=HypothesisCausalRoles.empty(),
        root_cause_eligibilities=RootCauseEligibilities.empty(),
        runtime_propagation=RuntimePropagation.empty(),
        runtime_mechanism_bridges=RuntimeMechanismBridges.empty(),
        topology=topology,
        symptom_entities=frozenset({caller}),
    )


def _dependency_alternative(actor: EntityRef, name: str) -> StructuralAlternative:
    return StructuralAlternative(
        alternative_id=f"alternative:{name}",
        actor=actor,
        role="dependency",
        queryable_dimensions=(GapDimension.DEPENDENCY_HEALTH, GapDimension.LOG_ERROR_PATTERN),
        observation_targets=(actor,),
    )


def _dependency_fixture() -> tuple[EntityRef, tuple[EntityRef, ...], Topology]:
    caller = _ref("Deployment", "caller")
    dependencies = tuple(_ref("Service", name) for name in ("backend-a", "backend-b", "backend-c"))
    latest = {
        caller: ObjectVersion(
            entity=caller,
            observed_at=datetime(2026, 1, 1, tzinfo=UTC),
            body={"kind": "Deployment", "metadata": {"name": "caller"}},
            evidence_id="object:caller",
            lifecycle=Lifecycle.OBSERVED,
        ),
        **{
            dependency: ObjectVersion(
                entity=dependency,
                observed_at=datetime(2026, 1, 1, tzinfo=UTC),
                body={"kind": "Service", "metadata": {"name": dependency.name}},
                evidence_id=f"object:{dependency.name}",
                lifecycle=Lifecycle.OBSERVED,
            )
            for dependency in dependencies
        },
    }
    topology = Topology(
        edges=tuple(
            Edge(source=caller, target=dependency, relation="calls") for dependency in dependencies
        ),
        latest=latest,
    )
    return caller, dependencies, topology


def test_dependency_log_contract_targets_symptom_linked_caller_and_coalesces() -> None:
    caller, dependencies, topology = _dependency_fixture()
    alternatives = tuple(
        _dependency_alternative(dependency, str(index))
        for index, dependency in enumerate(dependencies)
    )
    gaps = derive_information_gaps(
        (),
        ResolutionTrace(state=Resolution.RESOLVED),
        InMemorySource(name="contract"),
        structural_alternatives=alternatives,
        runtime_context=_context(topology, caller),
    )

    log_queries = [
        query for gap in gaps for query in gap.authorized_queries if query.capability == "logs"
    ]
    assert {query.target for query in log_queries} == {caller}
    assert all(query.target not in dependencies for query in log_queries)

    source = InMemorySource(
        name="coalescing",
        alert_items=[
            Alert(name="latency", service="caller", starts_at=datetime(2026, 1, 1, tzinfo=UTC))
        ],
        cutoff=datetime(2026, 1, 1, 1, tzinfo=UTC),
    )
    case = build_case(source, EngineConfig())
    diagnosis = case_to_diagnosis(case, tuple(gaps))
    candidates = build_observation_candidates(
        case=case,
        diagnosis=diagnosis,
        engine_config=EngineConfig(),
    )
    log_candidates = [candidate for candidate in candidates if candidate.capability == "logs"]
    assert len(log_candidates) == 1
    assert set(log_candidates[0].alternative_ids) == {
        alternative.alternative_id for alternative in alternatives
    }


def test_dependency_log_targets_require_direct_calls_and_incident_linkage() -> None:
    caller, dependencies, topology = _dependency_fixture()
    unrelated = _ref("Deployment", "unrelated")
    pod = _ref("Pod", "caller-0")
    topology = Topology(
        edges=(
            *topology.edges,
            Edge(source=unrelated, target=dependencies[0], relation="calls"),
        ),
        latest={
            **topology.latest,
            unrelated: ObjectVersion(
                entity=unrelated,
                observed_at=datetime(2026, 1, 1, tzinfo=UTC),
                body={"kind": "Deployment", "metadata": {"name": "unrelated"}},
                evidence_id="object:unrelated",
                lifecycle=Lifecycle.OBSERVED,
            ),
            pod: ObjectVersion(
                entity=pod,
                observed_at=datetime(2026, 1, 1, tzinfo=UTC),
                body={"kind": "Pod", "metadata": {"name": "caller-0"}},
                evidence_id="object:caller-0",
                lifecycle=Lifecycle.OBSERVED,
            ),
        },
    )
    alternative = _dependency_alternative(dependencies[0], "direct").model_copy(
        update={"observation_targets": (dependencies[0], caller, pod, unrelated)}
    )
    gaps = derive_information_gaps(
        (),
        ResolutionTrace(state=Resolution.RESOLVED),
        InMemorySource(name="scope"),
        structural_alternatives=(alternative,),
        runtime_context=_context(topology, caller),
    )
    log_targets = {
        query.target
        for gap in gaps
        for query in gap.authorized_queries
        if query.capability == "logs"
    }
    assert log_targets == {caller}
    assert all(target.kind == "Deployment" for target in log_targets)

    no_edge = _dependency_alternative(dependencies[1], "no-edge").model_copy(
        update={"observation_targets": (unrelated,)}
    )
    no_edge_gaps = derive_information_gaps(
        (),
        ResolutionTrace(state=Resolution.RESOLVED),
        InMemorySource(name="no-edge"),
        structural_alternatives=(no_edge,),
        runtime_context=_context(topology, caller),
    )
    assert not any(
        query.capability == "logs" for gap in no_edge_gaps for query in gap.authorized_queries
    )


def case_to_diagnosis(case: Case, gaps: tuple[InformationGap, ...]) -> Diagnosis:
    return diagnose_case(case).model_copy(update={"information_gaps": gaps})


def test_caller_log_observation_uses_normal_evidence_overlay_and_disambiguates_dependency() -> None:
    from rca_builders import alert, microservice

    from packages.rca.investigation.normalizers import normalize_observation

    caller = _ref("Deployment", "caller")
    dependency_a = _ref("Service", "backend-a")
    dependency_c = _ref("Service", "backend-c")
    source = InMemorySource(
        name="caller-logs",
        alert_items=[alert("latency", "caller", 0)],
        versions=[
            *microservice(
                "caller",
                0,
                {
                    "A_ADDR": "backend-a:1",
                    "B_ADDR": "backend-b:1",
                    "C_ADDR": "backend-c:1",
                },
            ),
            *microservice("backend-a", 0),
            *microservice("backend-b", 0),
            *microservice("backend-c", 0),
        ],
        error_items=[
            LogRecord(
                service="caller",
                at=None,
                severity="ERROR",
                message="backend-c connection refused",
                evidence_id="log:caller:backend-c",
            )
        ],
    )
    bounded = initial_view(source)
    case = build_case(bounded)
    gap = InformationGap(
        gap_id="gap:dependency",
        dimension=GapDimension.LOG_ERROR_PATTERN,
        missing_fact="which dependency emitted a caller-side connection error",
        authorized_queries=(
            AuthorizedQuery(
                capability="logs",
                target=caller,
                alternative_ids=("alternative:backend-c",),
            ),
        ),
    )
    query = InvestigationQuery(
        start=datetime(2024, 12, 31, 23, 30, tzinfo=UTC),
        end=datetime(2026, 1, 1, 1, tzinfo=UTC),
        limit=32,
    )
    observation = LogsTool(SourceInvestigationBackend(source)).execute_query(
        case, gap, caller, query
    )
    records = records_from_observation(observation)
    assert [record.evidence_id for record in records] == ["log:caller:backend-c"]

    normalized = normalize_observation(observation, case=case, gap=gap)
    assert {finding.related[0] for finding in normalized.findings} == {dependency_c}
    assert dependency_a not in {finding.related[0] for finding in normalized.findings}

    store = InMemoryEvidenceStore()
    store.put_many(records)
    overlay = OverlayObservationSource(
        bounded,
        store,
        ("log:caller:backend-c",),
        frozenset({"logs"}),
    )
    rebuilt = build_case(overlay)
    dependency_findings = [
        finding for finding in rebuilt.findings if finding.kind.value == "DEPENDENCY_ERRORS"
    ]
    assert {finding.related[0] for finding in dependency_findings} == {dependency_c}

    empty_source = InMemorySource(
        name="caller-logs-empty",
        alert_items=[alert("latency", "caller", 0)],
        versions=source.versions,
    )
    empty_case = build_case(initial_view(empty_source))
    empty_observation = LogsTool(SourceInvestigationBackend(empty_source)).execute_query(
        empty_case, gap, caller, query
    )
    assert empty_observation.outcome is GapOutcomeKind.NO_DATA
    assert normalize_observation(empty_observation, case=empty_case, gap=gap).findings == ()

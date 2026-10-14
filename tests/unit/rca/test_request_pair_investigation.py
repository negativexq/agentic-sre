"""Exact log reads must stay scoped, taped, budgeted and replayable."""

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

import pytest
from provider_test_helpers import provider_session_factory
from scripts.rollout_resolution_dev_proof import source
from sqlalchemy import select

from packages.rca.engine import EngineConfig, build_case, diagnose_case
from packages.rca.investigation.actions import (
    action_identity,
    observation_identity,
    validate_action,
)
from packages.rca.investigation.candidates import build_observation_candidates
from packages.rca.investigation.environment import (
    SourceInvestigationBackend,
    TempoInvestigationBackend,
)
from packages.rca.investigation.evidence import (
    InMemoryEvidenceStore,
    OverlayObservationSource,
    records_from_observation,
)
from packages.rca.investigation.state import InvestigationConfig
from packages.rca.investigation.tools import RuntimeTracesTool
from packages.rca.model import (
    AuthorizedQuery,
    EntityRef,
    GapDimension,
    GapResolvability,
    InformationGap,
    InvestigationAction,
    InvestigationQuery,
    LogRecord,
    TraceSpanObservation,
)
from packages.rca.provider_adapter import ProviderAdapter, ProviderReaders
from packages.rca.replay import ReplayDivergence, ReplayProviderAdapter, _Cursor, _load_tape
from packages.rca.source import InMemorySource
from packages.rca.trace_request_pairs import (
    RequestPairBatch,
    RequestPairTraceMissing,
    read_request_pairs,
)
from packages.storage.models import InvestigationReadRow


@dataclass
class PairSource(InMemorySource):
    provider_adapter: Any = None

    def supports_typed_runtime(self, capability: str) -> bool:
        return bool(self.provider_adapter and self.provider_adapter.supports(capability))

    def investigation_backend(self) -> TempoInvestigationBackend:
        return TempoInvestigationBackend(
            SourceInvestigationBackend(self), self.provider_adapter, self.cutoff
        )


TARGET = EntityRef(namespace="shop", kind="Deployment", name="order-service")


class Reader:
    def __init__(self, spans: list[TraceSpanObservation]) -> None:
        self.spans = spans
        self.calls: list[str] = []
        self.missing = False

    def query(
        self, target: EntityRef, query: InvestigationQuery
    ) -> tuple[TraceSpanObservation, ...]:
        raise AssertionError("Exact pairs must not use a general search")

    def query_pairs(
        self,
        target: EntityRef,
        query: InvestigationQuery,
        logs: tuple[LogRecord, ...],
        existing: tuple[TraceSpanObservation, ...],
    ) -> RequestPairBatch:
        def fetch(tid: str) -> tuple[TraceSpanObservation, ...]:
            self.calls.append(tid)
            if self.missing:
                raise RequestPairTraceMissing("not found")
            return tuple(s for s in self.spans if s.trace_id == tid)

        return read_request_pairs(target, query, logs, existing, fetch)


def setup() -> tuple[Any, Reader, Any, ProviderAdapter, InvestigationQuery, InformationGap]:
    observed = PairSource(**vars(source()))
    full = [
        s.model_copy(
            update={
                "semantic_attributes": {
                    **s.semantic_attributes,
                    "k8s.namespace.name": "shop",
                    "k8s.deployment.name": "order-service",
                }
            }
        )
        if s.service == "order-service"
        else s
        for s in observed.trace_items
    ]
    observed.trace_items = [s for s in full if not s.evidence_id.startswith("failed-client:")]
    factory = provider_session_factory()
    reader = Reader(full)
    adapter = ProviderAdapter("pair-run", "INVESTIGATION", factory, ProviderReaders(tempo=reader))
    observed.provider_adapter = adapter
    query = InvestigationQuery(
        start=observed.alert_coverage_start,
        end=observed.cutoff,
        source_log_ids=tuple(r.evidence_id for r in observed.error_items),
    )
    gap = InformationGap(
        gap_id="pair-gap",
        dimension=GapDimension.DEPENDENCY_HEALTH,
        resolvability=GapResolvability.RESOLVABLE,
        missing_fact="Which failed operations match the observed log records?",
        authorized_queries=(AuthorizedQuery(capability="runtime_traces", target=TARGET),),
        rationale="Existing caller failure evidence is incomplete",
    )
    return observed, reader, factory, adapter, query, gap


def test_pair_tool_admits_only_validated_evidence_and_existing_rules_resolve() -> None:
    observed, reader, factory, adapter, query, gap = setup()
    case = build_case(observed)
    assert diagnose_case(case).resolution.value == "AMBIGUOUS"
    backend = TempoInvestigationBackend(
        SourceInvestigationBackend(observed), adapter, observed.cutoff
    )
    observation = RuntimeTracesTool(backend).execute_query(case, gap, TARGET, query)
    assert observation.runtime is not None
    assert observation.runtime.query.template_id == "tempo.log_request_pairs.v1"
    assert len(reader.calls) == 4 and len(observation.payload["traces"]) == 8
    store = InMemoryEvidenceStore()
    added = store.put_many(records_from_observation(observation))
    overlay = OverlayObservationSource(
        observed, store, added.new_refs, frozenset({"runtime_traces"})
    )
    assert diagnose_case(build_case(overlay)).resolution.value == "RESOLVED"
    with factory() as session:
        row = session.scalars(select(InvestigationReadRow)).one()
        assert row.capability == "tempo_request_pairs"
        assert row.query_descriptor["source_log_ids"] == list(query.source_log_ids)


def test_pairs_reuse_cache_and_missing_trace_is_unknown_not_healthy() -> None:
    observed, reader, factory, adapter, query, gap = setup()
    cached = adapter.query_tempo_pairs(TARGET, query, observed.error_items, reader.spans)
    assert (
        isinstance(cached, RequestPairBatch) and not cached.attempted_trace_ids and not reader.calls
    )
    reader.missing = True
    backend = TempoInvestigationBackend(
        SourceInvestigationBackend(observed), adapter, observed.cutoff
    )
    observation = RuntimeTracesTool(backend).execute_query(build_case(observed), gap, TARGET, query)
    assert observation.runtime is not None
    assert observation.runtime.state.value == "UNKNOWN"
    assert observation.payload["traces"] == []
    assert all(
        a["deficits"] == ["TRACE_NOT_FOUND"]
        for a in observation.payload["request_pair_assessments"]
    )


def test_unseen_log_and_cross_namespace_pair_never_enter_overlay() -> None:
    observed, reader, factory, adapter, query, gap = setup()
    tool = RuntimeTracesTool(
        TempoInvestigationBackend(SourceInvestigationBackend(observed), adapter, observed.cutoff)
    )
    with pytest.raises(ValueError, match="visible"):
        tool.execute_query(
            build_case(observed),
            gap,
            TARGET,
            query.model_copy(update={"source_log_ids": ("unknown-log",)}),
        )
    assert not reader.calls
    reader.spans = [
        s.model_copy(
            update={
                "semantic_attributes": {**s.semantic_attributes, "k8s.namespace.name": "foreign"}
            }
        )
        for s in reader.spans
    ]
    observation = tool.execute_query(build_case(observed), gap, TARGET, query)
    assert observation.payload["traces"] == []
    assert all(
        "KUBERNETES_TARGET_MISMATCH" in a["deficits"]
        for a in observation.payload["request_pair_assessments"]
    )


def test_provider_tape_replays_exact_source_and_cache_without_network() -> None:
    observed, reader, factory, adapter, query, gap = setup()
    live = adapter.query_tempo_pairs(TARGET, query, observed.error_items, observed.trace_items)
    replay = ReplayProviderAdapter(
        "pair-run",
        "INVESTIGATION",
        adapter.capabilities(),
        _load_tape(factory, "pair-run"),
        _Cursor(0),
    )
    modified = deepcopy(observed.error_items)
    modified[0] = modified[0].model_copy(update={"message": "changed source"})
    with pytest.raises(ReplayDivergence):
        replay.query_tempo_pairs(TARGET, query, modified, observed.trace_items)
    assert replay.next_sequence == 1
    replayed = replay.query_tempo_pairs(TARGET, query, observed.error_items, observed.trace_items)
    assert replayed == live and replay.next_sequence is None
    assert len(reader.calls) == 4


def test_targeted_queries_keep_action_and_gap_budgets() -> None:
    observed, reader, factory, adapter, query, gap = setup()
    action = InvestigationAction(
        action="inspect",
        gap_id=gap.gap_id,
        capability="runtime_traces",
        target=TARGET,
        query=query,
        rationale="Fetch source-log operations",
    )
    other = action.model_copy(
        update={"query": query.model_copy(update={"source_log_ids": ("log:1",)})}
    )
    assert action_identity(action) != action_identity(other)
    assert observation_identity("runtime_traces", TARGET, query) != observation_identity(
        "runtime_traces", TARGET, other.query
    )
    kwargs: dict[str, Any] = dict(
        gaps=(gap,),
        tools={"runtime_traces": RuntimeTracesTool()},
        tool_calls=0,
        config=InvestigationConfig(),
    )
    assert validate_action(action, attempted_actions=(), **kwargs).valid
    assert not validate_action(
        action, attempted_actions=("pair-gap|first", "pair-gap|second"), **kwargs
    ).valid


def test_candidates_use_recorded_missing_log_ids_and_legacy_query_shape_stays_stable() -> None:
    observed, reader, factory, adapter, query, gap = setup()
    case = build_case(observed)
    diagnosis = diagnose_case(case).model_copy(update={"information_gaps": (gap,)})
    candidates = build_observation_candidates(
        case=case,
        diagnosis=diagnosis,
        engine_config=EngineConfig(),
    )
    assert len(candidates) == 1 and candidates[0].query.source_log_ids == query.source_log_ids
    assert "source_log_ids" not in InvestigationQuery().model_dump()


def test_actual_graph_selects_authorized_pair_action_and_replays_it() -> None:
    from packages.rca.epistemic_digest import diagnosis_epistemic_digest
    from packages.rca.investigation.graph import investigate_diagnosis
    from packages.rca.investigation.policy import ScriptedInvestigationPolicy
    from packages.rca.investigation.state import SEED_FULL_SOURCE

    observed, reader, factory, adapter, query, gap = setup()
    case = build_case(observed)
    diagnosis = diagnose_case(case)
    candidate = next(
        c
        for c in build_observation_candidates(
            case=case, diagnosis=diagnosis, engine_config=EngineConfig()
        )
        if c.query.source_log_ids
    )
    action = InvestigationAction(
        action="inspect",
        gap_id=candidate.gap_ids[0],
        capability=candidate.capability,
        target=candidate.target,
        query=candidate.query,
        rationale="Retrieve exact observed operations",
    )
    config = InvestigationConfig(seed_mode=SEED_FULL_SOURCE, engine=EngineConfig())
    result = investigate_diagnosis(
        observed,
        diagnosis=diagnosis,
        policy=ScriptedInvestigationPolicy(actions=[action]),
        config=config,
        initial_case=case,
    )
    assert (
        result.initial_resolution.value == "AMBIGUOUS"
        and result.final_resolution.value == "RESOLVED"
    )
    assert result.tool_calls == 1 and result.rejected_actions == 0 and len(reader.calls) == 4
    replay = ReplayProviderAdapter(
        "pair-run",
        "INVESTIGATION",
        adapter.capabilities(),
        _load_tape(factory, "pair-run"),
        _Cursor(0),
    )
    replay_source = PairSource(**{**vars(observed), "provider_adapter": replay})
    replay_case = build_case(replay_source)
    repeated = investigate_diagnosis(
        replay_source,
        diagnosis=diagnose_case(replay_case),
        policy=ScriptedInvestigationPolicy(actions=[action]),
        config=config,
        initial_case=replay_case,
    )
    assert diagnosis_epistemic_digest(result.diagnosis) == diagnosis_epistemic_digest(
        repeated.diagnosis
    )
    assert result.observations == repeated.observations and replay.next_sequence is None
    assert tuple(a.research_question for a in result.action_audits) == tuple(
        a.research_question for a in repeated.action_audits
    )
    assert result.remaining_research_questions == repeated.remaining_research_questions
    assert len(reader.calls) == 4


def test_mismatched_request_and_cached_input_divergence_remain_explicit() -> None:
    observed, reader, factory, adapter, query, gap = setup()
    bad = deepcopy(observed.error_items)
    bad[0] = bad[0].model_copy(
        update={"attributes": {**bad[0].attributes, "request_id": "wrong-request"}}
    )
    observed.error_items = bad
    result = RuntimeTracesTool(observed.investigation_backend()).execute_query(
        build_case(observed), gap, TARGET, query
    )
    assessments = result.payload["request_pair_assessments"]
    assert (
        assessments[0]["state"] == "UNKNOWN"
        and "REQUEST_ID_MISSING_OR_MISMATCHED" in assessments[0]["deficits"]
    )
    replay = ReplayProviderAdapter(
        "pair-run",
        "INVESTIGATION",
        adapter.capabilities(),
        _load_tape(factory, "pair-run"),
        _Cursor(0),
    )
    with pytest.raises(ReplayDivergence):
        replay.query_tempo_pairs(TARGET, query, bad, ())
    assert replay.next_sequence == 1


def test_pair_wire_dto_and_normal_search_cannot_silently_drop_source_ids() -> None:
    from packages.rca.investigation.policy import InvestigationActionWire
    from packages.rca.investigation.tempo import TempoConfig, TempoTraceReader

    observed, reader, factory, adapter, query, gap = setup()
    action = InvestigationAction(
        action="inspect",
        gap_id=gap.gap_id,
        capability="runtime_traces",
        target=TARGET,
        query=query,
        rationale="Read exact operations",
    )
    assert (
        InvestigationActionWire.model_validate(action.model_dump(mode="json")).to_domain() == action
    )
    ordinary = action.model_copy(update={"query": query.model_copy(update={"source_log_ids": ()})})
    assert (
        InvestigationActionWire.model_validate(ordinary.model_dump(mode="json")).to_domain()
        == ordinary
    )
    with pytest.raises(ValueError, match="exact-pair"):
        TempoTraceReader(TempoConfig("http://unused")).query(TARGET, query)


def test_partial_span_budget_never_splits_pair_or_adds_authority() -> None:
    from pydantic import ValidationError

    observed, reader, factory, adapter, query, gap = setup()
    result = adapter.query_tempo_pairs(
        TARGET, query.model_copy(update={"limit": 3}), observed.error_items, ()
    )
    assert isinstance(result, RequestPairBatch) and len(result.spans) == 2
    assert len(result.attempted_trace_ids) == 1
    assert sum(a.state == "UNKNOWN" for a in result.assessments) == 3
    with pytest.raises(ValidationError):
        InvestigationQuery(source_log_ids=tuple(f"log:{i}" for i in range(9)))


def test_false_provider_corroboration_is_integrity_failure_before_overlay() -> None:
    from packages.rca.provider_adapter import ProviderIntegrityError
    from packages.rca.trace_request_pairs import RequestPairAssessment

    observed, reader, factory, adapter, query, gap = setup()

    class BadReader(Reader):
        def query_pairs(
            self,
            target: EntityRef,
            query: InvestigationQuery,
            logs: tuple[LogRecord, ...],
            existing: tuple[TraceSpanObservation, ...],
        ) -> RequestPairBatch:
            return RequestPairBatch(
                assessments=tuple(
                    RequestPairAssessment(source_log_id=r.evidence_id, state="CORROBORATED")
                    for r in logs
                )
            )

    broken = ProviderAdapter(
        "broken-pairs", "INVESTIGATION", factory, ProviderReaders(tempo=BadReader([]))
    )
    backend = TempoInvestigationBackend(
        SourceInvestigationBackend(observed), broken, observed.cutoff
    )
    with pytest.raises(ProviderIntegrityError, match="without a valid pair"):
        RuntimeTracesTool(backend).execute_query(build_case(observed), gap, TARGET, query)


def test_default_intent_policy_can_choose_pair_read_without_scripted_action() -> None:
    from packages.rca.investigation.graph import investigate_diagnosis
    from packages.rca.investigation.intents import DeterministicIntentPolicy
    from packages.rca.investigation.state import SEED_FULL_SOURCE

    observed, reader, factory, adapter, query, gap = setup()
    result = investigate_diagnosis(
        observed,
        policy=DeterministicIntentPolicy(),
        config=InvestigationConfig(seed_mode=SEED_FULL_SOURCE, engine=EngineConfig()),
    )
    assert result.final_resolution.value == "RESOLVED"
    assert result.tool_calls <= InvestigationConfig().max_tool_calls
    assert any(o.payload.get("source_log_ids") for o in result.observations)
    assert len(reader.calls) == 4

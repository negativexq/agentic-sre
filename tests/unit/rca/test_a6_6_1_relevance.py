from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from packages.rca.engine import Case, build_case, diagnose_case
from packages.rca.investigation.candidates import CandidateDiscriminator, ObservationCandidate
from packages.rca.investigation.graph import investigate_diagnosis
from packages.rca.investigation.intents import (
    IntentUtility,
    InvestigationIntentKind,
    InvestigationPhase,
    ObservationBundle,
    ScoredObservationBundle,
    intent_relevance_key,
)
from packages.rca.investigation.policy import LLMIntentPolicy
from packages.rca.investigation.state import InvestigationConfig
from packages.rca.investigation.tools import make_observation
from packages.rca.llm import LLMError, Reply, ScriptedLLM
from packages.rca.model import (
    Alert,
    AuthorizedQuery,
    Diagnosis,
    EntityRef,
    GapDimension,
    GapOutcome,
    GapOutcomeKind,
    GapResolvability,
    InformationGap,
    InvestigationObservation,
    InvestigationQuery,
    InvestigationResult,
)
from packages.rca.source import InMemorySource

ONSET = datetime(2026, 2, 1, 12, 0, tzinfo=UTC)


class _NoDataTool:
    name = "events"

    def __init__(self) -> None:
        self.calls = 0

    def execute(
        self, _case: Case, gap: InformationGap, target: EntityRef
    ) -> InvestigationObservation:
        self.calls += 1
        return make_observation(gap=gap, capability="events", target=target, payload={})


def _fixture() -> tuple[InMemorySource, Diagnosis, tuple[ObservationCandidate, ...]]:
    source = InMemorySource(
        name="a6-6-1",
        alert_items=[Alert(name="latency", service="api", namespace="shop", starts_at=ONSET)],
        cutoff=ONSET,
    )
    case = build_case(source)
    diagnosis = diagnose_case(case)
    candidates = tuple(
        ObservationCandidate(
            candidate_id=f"candidate-{name}",
            capability="events",
            target=EntityRef(kind="Deployment", name=f"target-{name}", namespace="shop"),
            query=InvestigationQuery(start=ONSET, end=ONSET),
            gap_ids=(f"gap-{name}",),
            dimensions=(GapDimension.EVENT_SEQUENCE,),
            hypothesis_ids=(),
            alternative_ids=(f"alternative-{name}",),
            discriminators=(
                CandidateDiscriminator(
                    gap_id=f"gap-{name}",
                    dimension=GapDimension.EVENT_SEQUENCE,
                    missing_fact="bounded fact",
                    support_outcomes=(
                        GapOutcome(
                            kind=GapOutcomeKind.SUPPORTS,
                            alternative_ids=(f"alternative-{name}",),
                            condition="positive event for this alternative",
                            implication="supports this alternative over a competing state",
                        ),
                    ),
                    comparison_hypothesis_ids=(),
                    comparison_alternative_ids=("competing-alternative",),
                    no_data_outcomes=(),
                ),
            ),
        )
        for name in ("a", "b", "c", "d")
    )
    gaps = tuple(
        InformationGap(
            gap_id=candidate.gap_ids[0],
            dimension=GapDimension.EVENT_SEQUENCE,
            alternative_ids=candidate.alternative_ids,
            missing_fact="bounded fact",
            discriminating_outcomes=(
                GapOutcome(
                    kind=GapOutcomeKind.SUPPORTS,
                    alternative_ids=candidate.alternative_ids,
                    condition="positive event for this alternative",
                    implication="supports this alternative over a competing state",
                ),
            ),
            authorized_queries=(AuthorizedQuery(capability="events", target=candidate.target),),
            resolvability=GapResolvability.RESOLVABLE,
        )
        for candidate in candidates
    )
    return source, diagnosis.model_copy(update={"information_gaps": gaps}), candidates


def _scored(
    bundle_id: str,
    intent: InvestigationIntentKind,
    candidate_id: str,
    *,
    priority: int = 5,
    blocker: int = 3,
    stable: str | None = None,
) -> ScoredObservationBundle:
    bundle = ObservationBundle(
        bundle_id=bundle_id,
        intent=intent,
        phase=InvestigationPhase.SOURCE_DISCOVERY,
        candidate_ids=(candidate_id,),
        capabilities=("events",),
        gap_ids=(f"gap-{candidate_id.removeprefix('candidate-')}",),
        dimensions=(GapDimension.EVENT_SEQUENCE,),
        hypothesis_ids=(),
        alternative_ids=(),
    )
    return ScoredObservationBundle(
        bundle=bundle,
        utility=IntentUtility(
            admissible=True,
            phase_match=1,
            decision_blocker_match=blocker,
            leading_hypothesis_relevance=0,
            unresolved_hypothesis_relevance=0,
            eligibility_relevance=0,
            semantic_priority=priority,
            stable_tiebreak=stable or bundle_id,
        ),
    )


def _patch_rank(
    monkeypatch: pytest.MonkeyPatch, ranked: tuple[ScoredObservationBundle, ...]
) -> None:
    monkeypatch.setattr(
        "packages.rca.investigation.graph.rank_observation_bundles",
        lambda **_: ranked,
    )


def _run_graph(
    monkeypatch: pytest.MonkeyPatch,
    ranked: tuple[ScoredObservationBundle, ...],
    replies: list[Reply],
) -> tuple[ScriptedLLM, InvestigationResult, dict[str, Any]]:
    source, diagnosis, candidates = _fixture()
    monkeypatch.setattr(
        "packages.rca.investigation.graph.build_observation_candidates",
        lambda **_: candidates,
    )
    _patch_rank(monkeypatch, ranked)
    client = ScriptedLLM(replies=replies)
    tool = _NoDataTool()
    result = investigate_diagnosis(
        source,
        diagnosis=diagnosis,
        initial_case=build_case(source),
        policy=LLMIntentPolicy(client),
        tools={"events": tool},
        config=InvestigationConfig(max_turns=1),
    )
    return client, result, {"events": tool}


def test_s1_relevance_key_excludes_stable_tiebreak() -> None:
    a = _scored("intent:a", InvestigationIntentKind.INCIDENT_ACTOR_DISCOVERY, "candidate-a")
    b = _scored(
        "intent:b",
        InvestigationIntentKind.RECENT_SOURCE_CHANGE,
        "candidate-b",
        stable="different",
    )
    assert intent_relevance_key(a) == intent_relevance_key(b)


def test_s2_higher_blocker_relevance_excludes_lower_bundle_from_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = _scored("intent:a", InvestigationIntentKind.INCIDENT_ACTOR_DISCOVERY, "candidate-a")
    b = _scored(
        "intent:b",
        InvestigationIntentKind.RECENT_SOURCE_CHANGE,
        "candidate-b",
        blocker=2,
    )
    client, result, tools = _run_graph(monkeypatch, (a, b), [])
    assert client.calls == 0
    assert tools["events"].calls == 1
    assert any("intent=INCIDENT_ACTOR_DISCOVERY" in step.detail for step in result.diagnosis.steps)


def test_s3_semantic_priority_difference_is_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = _scored("intent:a", InvestigationIntentKind.INCIDENT_ACTOR_DISCOVERY, "candidate-a")
    b = _scored(
        "intent:b",
        InvestigationIntentKind.RECENT_SOURCE_CHANGE,
        "candidate-b",
        priority=4,
    )
    client, result, _ = _run_graph(monkeypatch, (a, b), [])
    assert client.calls == 0
    assert any(
        "selection=deterministic-top-intent" in step.detail for step in result.diagnosis.steps
    )


def test_s4_true_top_tie_exposes_only_top_class_and_preserves_physical_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = _scored("intent:a", InvestigationIntentKind.INCIDENT_ACTOR_DISCOVERY, "candidate-a")
    b = _scored("intent:b", InvestigationIntentKind.RECENT_SOURCE_CHANGE, "candidate-b")
    c = _scored(
        "intent:c",
        InvestigationIntentKind.DEPENDENCY_ERROR_INSPECTION,
        "candidate-c",
        priority=4,
    )
    client, result, tools = _run_graph(monkeypatch, (a, b, c), [{"intent_id": "intent:b"}])
    assert client.calls == 1
    assert tools["events"].calls == 1
    assert json_menu_ids(client) == {"intent:a", "intent:b"}
    assert any("intent=RECENT_SOURCE_CHANGE" in step.detail for step in result.diagnosis.steps)


def json_menu_ids(client: ScriptedLLM) -> set[str]:
    import json

    return {item["intent_id"] for item in json.loads(client.prompts[0])["menu"]}


def test_s5_invalid_lower_ranked_choice_uses_top_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = _scored("intent:a", InvestigationIntentKind.INCIDENT_ACTOR_DISCOVERY, "candidate-a")
    b = _scored("intent:b", InvestigationIntentKind.RECENT_SOURCE_CHANGE, "candidate-b")
    c = _scored(
        "intent:c",
        InvestigationIntentKind.DEPENDENCY_ERROR_INSPECTION,
        "candidate-c",
        priority=4,
    )
    client, result, tools = _run_graph(monkeypatch, (a, b, c), [{"intent_id": "intent:c"}])
    assert client.calls == 1
    assert tools["events"].calls == 1
    assert any(
        "selection=deterministic-top-fallback" in step.detail for step in result.diagnosis.steps
    )


def test_s6_provider_failure_stays_in_top_class(monkeypatch: pytest.MonkeyPatch) -> None:
    a = _scored("intent:a", InvestigationIntentKind.INCIDENT_ACTOR_DISCOVERY, "candidate-a")
    b = _scored("intent:b", InvestigationIntentKind.RECENT_SOURCE_CHANGE, "candidate-b")
    c = _scored(
        "intent:c",
        InvestigationIntentKind.DEPENDENCY_ERROR_INSPECTION,
        "candidate-c",
        priority=4,
    )

    class FailingClient(ScriptedLLM):
        def complete_json(self, **_: object) -> dict[str, object]:
            self.calls += 1
            raise LLMError("provider failure")

    source, diagnosis, candidates = _fixture()
    monkeypatch.setattr(
        "packages.rca.investigation.graph.build_observation_candidates",
        lambda **_: candidates,
    )
    _patch_rank(monkeypatch, (a, b, c))
    client = FailingClient(replies=[])
    tool = _NoDataTool()
    result = investigate_diagnosis(
        source,
        diagnosis=diagnosis,
        initial_case=build_case(source),
        policy=LLMIntentPolicy(client),
        tools={"events": tool},
        config=InvestigationConfig(max_turns=1),
    )
    assert client.calls == 1
    assert tool.calls == 1
    assert any(
        "selection=deterministic-top-fallback" in step.detail for step in result.diagnosis.steps
    )


def test_s8_multiple_legal_non_equivalent_intents_do_not_reach_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = _scored("intent:a", InvestigationIntentKind.INCIDENT_ACTOR_DISCOVERY, "candidate-a")
    b = _scored(
        "intent:b",
        InvestigationIntentKind.RECENT_SOURCE_CHANGE,
        "candidate-b",
        blocker=1,
    )
    client, result, _ = _run_graph(monkeypatch, (a, b), [])
    assert client.calls == 0
    assert not client.prompts
    assert any(
        "selection=deterministic-top-intent" in step.detail for step in result.diagnosis.steps
    )


def test_s12_true_tie_structured_retry_is_exactly_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = _scored("intent:a", InvestigationIntentKind.INCIDENT_ACTOR_DISCOVERY, "candidate-a")
    b = _scored("intent:b", InvestigationIntentKind.RECENT_SOURCE_CHANGE, "candidate-b")
    client, result, _ = _run_graph(
        monkeypatch,
        (a, b),
        [{"target": "forbidden"}, {"intent_id": "intent:b"}],
    )
    assert client.calls == 2
    assert result.tool_calls == 1

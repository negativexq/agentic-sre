"""Input acquisition must never be reported as RCA condition closure."""

from pathlib import Path

import pytest
from scripts.measure_precondition_acquisition import precondition_metrics

from packages.rca.demo import demo_source
from packages.rca.investigation.graph import investigate_diagnosis
from packages.rca.investigation.intents import DeterministicIntentPolicy
from packages.rca.model import (
    EntityRef,
    GapDimension,
    InvestigationEvidenceTarget,
    InvestigationResearchQuestion,
    InvestigationResearchRead,
    ObjectVersion,
)


def test_listing_obtained_is_distinct_from_decision_or_rule_satisfaction() -> None:
    result = investigate_diagnosis(demo_source(), policy=DeterministicIntentPolicy())
    assert result.initial_diagnosis is not None
    onset = result.initial_diagnosis.symptoms.reference_time
    assert onset is not None
    initial = result.initial_diagnosis.model_copy(
        update={"symptoms": result.initial_diagnosis.symptoms.model_copy(update={"onset": onset})}
    )
    audit = next(item for item in result.action_audits if item.observation_id)
    assert audit.action.query is not None
    actor = EntityRef(namespace="ns", kind="ConfigMap", name="settings")
    listing = ObjectVersion(entity=actor, body={}, observed_at=onset, evidence_id="listing:new")
    read = InvestigationResearchRead(
        candidate_id="c",
        capability="history",
        target=actor,
        query=audit.action.query,
        evidence_targets=(
            InvestigationEvidenceTarget(
                contract_id="listing",
                source_gap_ids=("g",),
                kind="OBSERVATION",
                fact_family="object_listing",
                acquisition_class="DIRECT",
            ),
        ),
    )
    audit = audit.model_copy(
        update={
            "action": audit.action.model_copy(update={"capability": "history", "target": actor}),
            "new_evidence_refs": (listing.evidence_id,),
            "research_question": InvestigationResearchQuestion(
                gap_id="g",
                dimension=GapDimension.CONFIG_DIFFERENCE,
                missing_fact="listing",
                reads=(read,),
                availability="AVAILABLE",
            ),
        }
    )
    observation = next(
        item for item in result.observations if item.observation_id == audit.observation_id
    )
    measured = result.model_copy(
        update={
            "initial_diagnosis": initial,
            "action_audits": (audit,),
            "observations": (
                observation.model_copy(
                    update={"payload": {"versions": [listing.model_dump(mode="json")]}}
                ),
            ),
        }
    )
    metrics = precondition_metrics(measured)
    assert metrics["direct_input_reads"] == 1
    assert metrics["direct_listing_input_obtained"] == 1
    assert metrics["direct_listing_new_input_obtained"] == 1
    assert not metrics["input_obtained_is_rule_satisfaction"]
    empty = measured.model_copy(update={"observations": ()})
    assert precondition_metrics(empty)["direct_listing_input_obtained"] == 0
    foreign = listing.model_copy(update={"entity": actor.model_copy(update={"namespace": "other"})})
    foreign_result = measured.model_copy(
        update={
            "observations": (
                observation.model_copy(
                    update={"payload": {"versions": [foreign.model_dump(mode="json")]}}
                ),
            )
        }
    )
    assert precondition_metrics(foreign_result)["direct_listing_input_obtained"] == 0


def test_missing_capture_channels_prevent_an_inapplicable_benchmark(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json
    from types import SimpleNamespace

    import scripts.measure_precondition_acquisition as measurement

    from packages.evals.itbench.dataset import ITBenchLiteDataset
    from packages.rca.source import InMemorySource

    monkeypatch.setattr(measurement, "RCA_ENGINE_VERSION", "2.22.0")
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    dataset = SimpleNamespace(manifest_path=manifest, scenario=lambda scenario_id: scenario_id)
    monkeypatch.setattr(ITBenchLiteDataset, "open", lambda root: dataset)
    monkeypatch.setattr(
        measurement, "SnapshotSource", lambda scenario: InMemorySource(name=scenario)
    )

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("An inapplicable registry must not schedule outcome runs")

    monkeypatch.setattr(measurement, "build_case", forbidden)
    monkeypatch.setattr(measurement, "investigate_diagnosis", forbidden)
    output = tmp_path / "measurement"
    measurement.measure(tmp_path, output)
    summary = json.loads((output / "summary.json").read_text())
    assert summary["status"] == "INAPPLICABLE_CAPTURE_CHANNELS"
    assert summary["executed_runs"] == 0
    assert summary["planned_runs"] == 40
    assert len(summary["applicability"]) == 10
    assert summary["groups"] == {}
    assert summary["comparisons"] == []
    assert (output / "protocol.json").exists()
    assert (output / "runner.py").exists()


def test_frozen_frontier_measurement_rejects_a_changed_acquisition_engine(tmp_path: Path) -> None:
    import scripts.measure_precondition_acquisition as measurement

    with pytest.raises(ValueError, match="requires engine 2.22.0"):
        measurement.measure(tmp_path, tmp_path / "run")
    assert not (tmp_path / "run").exists()

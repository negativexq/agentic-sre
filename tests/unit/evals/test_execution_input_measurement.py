"""Acquisition proxies and the isolated execution-goal control."""

from scripts.measure_execution_input_acquisition import execution_metrics, without_execution_goals

from packages.rca.demo import demo_source
from packages.rca.investigation.graph import investigate_diagnosis
from packages.rca.investigation.intents import DeterministicIntentPolicy
from packages.rca.model import (
    GapDimension,
    InvestigationEvidenceTarget,
    InvestigationExecutionStatus,
    InvestigationResearchQuestion,
    InvestigationResearchRead,
)


def test_selected_input_read_does_not_imply_predicate_receipt_or_rule_firing() -> None:
    result = investigate_diagnosis(demo_source(), policy=DeterministicIntentPolicy())
    audit = next(item for item in result.action_audits if item.observation_id)
    assert audit.action.query is not None
    assert audit.action.capability is not None
    assert audit.action.target is not None
    target = InvestigationEvidenceTarget(
        contract_id="test:execution-input",
        source_gap_ids=("g",),
        kind="OBSERVATION",
        fact_family="object_history",
        input_scope="RULE_INPUT",
        acquisition_class="PARTIAL",
    )
    read = InvestigationResearchRead(
        candidate_id="c",
        capability=audit.action.capability,
        target=audit.action.target,
        query=audit.action.query,
        evidence_targets=(target,),
    )
    selected = audit.model_copy(
        update={
            "new_evidence_refs": ("new",),
            "normalized_finding_ids": ("finding",),
            "research_question": InvestigationResearchQuestion(
                gap_id="g",
                dimension=GapDimension.FAILURE_ONSET,
                missing_fact="execution input",
                reads=(read,),
                availability="AVAILABLE",
            ),
        }
    )
    measured = result.model_copy(update={"action_audits": (selected,)})
    metrics = execution_metrics(measured)
    assert metrics["rule_input_reads"] == 1
    assert metrics["rule_input_reads_new_evidence"] == 1
    assert metrics["rule_input_reads_new_findings"] == 1
    assert not metrics["input_obtained_is_rule_satisfaction"]
    assert metrics["exact_input_receipt"].startswith("NOT_GRADED")
    skipped = selected.model_copy(
        update={
            "backend_execution_status": InvestigationExecutionStatus.NOT_EXECUTED,
        }
    )
    assert (
        execution_metrics(measured.model_copy(update={"action_audits": (skipped,)}))[
            "rule_input_reads"
        ]
        == 0
    )


def test_control_preserves_all_non_execution_information_gaps() -> None:
    result = investigate_diagnosis(demo_source(), policy=DeterministicIntentPolicy())
    diagnosis = result.diagnosis
    assert diagnosis.information_gaps
    contract = InvestigationEvidenceTarget(
        contract_id="execution",
        source_gap_ids=("g",),
        kind="OBSERVATION",
        fact_family="object_history",
        input_scope="RULE_INPUT",
        acquisition_class="PARTIAL",
    )
    generated = diagnosis.information_gaps[0].model_copy(
        update={
            "gap_id": "execution-goal",
            "execution_input_target": contract,
        }
    )
    prepared = diagnosis.model_copy(
        update={"information_gaps": (*diagnosis.information_gaps, generated)}
    )
    controlled = without_execution_goals(None, prepared, ())
    assert controlled.information_gaps == tuple(
        gap for gap in diagnosis.information_gaps if gap.execution_input_target is None
    )
    assert controlled.resolution_trace == diagnosis.resolution_trace
    assert controlled.hypothesis == diagnosis.hypothesis

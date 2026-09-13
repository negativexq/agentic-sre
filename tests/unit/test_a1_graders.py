"""Offline tests for A1 targets, graders, denominators and exploration metrics."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from packages.contracts import EvidenceSourceType, TimeWindow
from packages.evals import (
    A1_COMPATIBILITY_TARGETS,
    A1_TARGET_BY_SCENARIO,
    A1FailureLabel,
    a0_compatibility_baseline,
    a1_compatibility_target_hash,
    aggregate_a1_grades,
    grade_a1_run,
)
from packages.investigation import (
    A1RunArtifact,
    A1SafetyCounters,
    CausalHypothesis,
    CausalStopDecision,
    DecisionType,
    DependencyResourceId,
    EvidenceCategory,
    EvidenceSummary,
    InvestigationUsage,
    StopReason,
    StructuredTrigger,
    TerminationReason,
    TurnRecord,
    WorkloadComponentId,
)

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


def _usage(
    *,
    model_calls: int = 2,
    tool_calls: int = 1,
    requests: int = 1,
    terminal_decision: DecisionType = DecisionType.SUBMIT_HYPOTHESIS,
) -> InvestigationUsage:
    return InvestigationUsage(
        model_calls=model_calls,
        tool_calls=tool_calls,
        tool_requests_total=requests,
        input_tokens=10,
        output_tokens=5,
        latency_ms=100,
        prompt_hash="prompt",
        provider="fake",
        model="test-model",
        reasoning_effort="none",
        estimated_api_calls=0,
        actual_api_calls=0,
        model_calls_limit=3,
        tool_calls_limit=8,
        terminal_decision=terminal_decision,
    )


def _evidence(
    incident_id: UUID,
    *,
    workload: WorkloadComponentId | None = None,
    resource: DependencyResourceId | None = None,
    tool: str = "service_latency",
) -> EvidenceSummary:
    return EvidenceSummary(
        incident_id=incident_id,
        evidence_id=uuid4(),
        tool=tool,
        target_workload=workload,
        target_resource=resource,
        source_type=EvidenceSourceType.METRIC.value,
        source_system=tool,
        time_window=TimeWindow(starts_at=NOW, ends_at=NOW),
        temporal_mode="INCIDENT_WINDOW",
        collected_at=NOW,
        bounded_observation_summary='{"status":"degraded"}',
    )


def _hypothesis(
    target_id: str,
    evidence_ids: list[UUID],
    *,
    causal_component: WorkloadComponentId | None = None,
    causal_resource: DependencyResourceId | None = None,
    trigger_resource: DependencyResourceId | None = None,
) -> CausalHypothesis:
    target = A1_TARGET_BY_SCENARIO[target_id]
    return CausalHypothesis(
        symptom_component=target.symptom_component,
        causal_component=causal_component or target.causal_component,
        causal_resource=causal_resource,
        mechanism=target.mechanism,
        structured_trigger=StructuredTrigger(
            trigger_type=target.structured_trigger.trigger_type,
            trigger_component=target.structured_trigger.trigger_component,
            trigger_resource=trigger_resource
            if trigger_resource is not None
            else target.structured_trigger.trigger_resource,
        ),
        causal_summary="bounded offline hypothesis",
        evidence_ids=evidence_ids,
    )


def _artifact(
    target_id: str,
    *,
    evidence: list[EvidenceSummary],
    hypothesis: CausalHypothesis | None = None,
    stop: CausalStopDecision | None = None,
    tools: list[str] | None = None,
    arguments: list[dict[str, str]] | None = None,
    statuses: list[str] | None = None,
    termination: TerminationReason | None = None,
    incident_id: UUID | None = None,
) -> A1RunArtifact:
    current_incident = incident_id or (evidence[0].incident_id if evidence else uuid4())
    tools = tools or (["service_latency"] if evidence else [])
    arguments = arguments or ([{"service": "payment-service"}] if evidence else [])
    return A1RunArtifact(
        experiment_id="a1-offline-test",
        run_id=uuid4(),
        incident_id=current_incident,
        observation_window=TimeWindow(starts_at=NOW, ends_at=NOW),
        turns=(
            [
                TurnRecord(
                    turn=1,
                    decision="CALL_TOOLS",
                    requested_tools=tools,
                    canonical_arguments=arguments,
                    request_statuses=statuses or ["SUCCESS"] * len(tools),
                    target_workloads=[
                        WorkloadComponentId.PAYMENT_SERVICE
                        if argument.get("service") == "payment-service"
                        else WorkloadComponentId.ORDER_SERVICE
                        for argument in arguments
                        if "service" in argument
                    ],
                    new_evidence_ids=[item.evidence_id for item in evidence],
                    workloads_queried_so_far=[
                        item.target_workload for item in evidence if item.target_workload
                    ],
                    resources_queried_so_far=[
                        item.target_resource for item in evidence if item.target_resource
                    ],
                    remaining_model_calls=2,
                    remaining_tool_calls=7,
                )
            ]
            if evidence or tools
            else []
        ),
        evidence=evidence,
        hypothesis=hypothesis,
        stop=stop,
        termination_reason=termination
        or (
            TerminationReason.HYPOTHESIS_SUBMITTED
            if hypothesis
            else TerminationReason.AGENT_STOPPED
        ),
        usage=_usage(
            model_calls=2 if hypothesis or evidence else 1,
            tool_calls=len(evidence),
            requests=len(tools),
        ),
        safety=A1SafetyCounters(),
    )


def test_compatibility_targets_cover_all_scenarios_and_use_canonical_fields() -> None:
    assert [item.scenario_id for item in A1_COMPATIBILITY_TARGETS] == [
        f"V020-{index:03d}" for index in range(1, 11)
    ]
    assert len({item.scenario_id for item in A1_COMPATIBILITY_TARGETS}) == 10
    assert A1_TARGET_BY_SCENARIO["V020-003"].symptom_component is WorkloadComponentId.ORDER_SERVICE
    assert A1_TARGET_BY_SCENARIO["V020-003"].causal_component is WorkloadComponentId.PAYMENT_SERVICE
    assert A1_TARGET_BY_SCENARIO["V020-005"].causal_resource is DependencyResourceId.POSTGRESQL
    assert A1_TARGET_BY_SCENARIO["V020-007"].causal_resource is DependencyResourceId.KAFKA
    assert A1_TARGET_BY_SCENARIO["V020-008"].causal_resource is None
    assert A1_TARGET_BY_SCENARIO["V020-010"].change_evidence_opportunity is True
    assert len(a1_compatibility_target_hash()) == 64


def test_correct_hypothesis_scores_all_structured_dimensions() -> None:
    incident_id = uuid4()
    payment = _evidence(incident_id, workload=WorkloadComponentId.PAYMENT_SERVICE)
    order = _evidence(incident_id, workload=WorkloadComponentId.ORDER_SERVICE)
    artifact = _artifact(
        "V020-003",
        evidence=[order, payment],
        hypothesis=_hypothesis("V020-003", [payment.evidence_id]),
        tools=["service_latency", "service_latency"],
        arguments=[{"service": "order-service"}, {"service": "payment-service"}],
    )

    grade = grade_a1_run(artifact, A1_TARGET_BY_SCENARIO["V020-003"])

    assert grade.completion == 1
    assert grade.symptom_component == 1
    assert grade.causal_component == 1
    assert grade.mechanism == 1
    assert grade.structured_trigger == 1
    assert grade.evidence_reference_integrity == 1
    assert grade.submitted_causal_component_evidence == 1
    assert grade.ground_truth_causal_component_evidence == 1
    assert grade.causal_component_explored is True


def test_component_resource_mechanism_and_trigger_misses_are_separate() -> None:
    incident_id = uuid4()
    evidence = _evidence(
        incident_id,
        workload=WorkloadComponentId.PAYMENT_SERVICE,
        resource=DependencyResourceId.POSTGRESQL,
        tool="db_connection_pressure",
    )
    hypothesis = _hypothesis(
        "V020-005",
        [evidence.evidence_id],
        causal_resource=DependencyResourceId.KAFKA,
        trigger_resource=DependencyResourceId.KAFKA,
    )
    grade = grade_a1_run(
        _artifact("V020-005", evidence=[evidence], hypothesis=hypothesis),
        A1_TARGET_BY_SCENARIO["V020-005"],
    )

    assert grade.causal_component == 1
    assert grade.mechanism == 1
    assert grade.causal_resource == 0
    assert grade.structured_trigger == 0
    assert grade.trigger_type == 1
    assert grade.trigger_resource == 0
    assert A1FailureLabel.WRONG_CAUSAL_RESOURCE in grade.failure_labels


def test_invalid_and_cross_incident_references_are_distinguished() -> None:
    incident_id = uuid4()
    current = _evidence(incident_id, workload=WorkloadComponentId.PAYMENT_SERVICE)
    foreign = _evidence(uuid4(), workload=WorkloadComponentId.PAYMENT_SERVICE)
    hypothesis = _hypothesis("V020-001", [current.evidence_id, foreign.evidence_id, UUID(int=1)])
    artifact = _artifact(
        "V020-001",
        evidence=[current, foreign],
        hypothesis=hypothesis,
        incident_id=incident_id,
    )

    grade = grade_a1_run(artifact, A1_TARGET_BY_SCENARIO["V020-001"])

    assert grade.evidence_reference_integrity == 0
    assert grade.fabricated_evidence_reference_count == 1
    assert grade.cross_incident_evidence_reference_count == 1
    assert A1FailureLabel.INVALID_EVIDENCE_REFERENCE in grade.failure_labels
    assert A1FailureLabel.CROSS_INCIDENT_EVIDENCE in grade.failure_labels


def test_foreign_evidence_does_not_count_as_local_causal_support_or_exploration() -> None:
    incident_id = uuid4()
    foreign = _evidence(uuid4(), workload=WorkloadComponentId.PAYMENT_SERVICE)
    hypothesis = _hypothesis("V020-003", [foreign.evidence_id])
    grade = grade_a1_run(
        _artifact("V020-003", evidence=[foreign], hypothesis=hypothesis, incident_id=incident_id),
        A1_TARGET_BY_SCENARIO["V020-003"],
    )

    assert grade.ground_truth_causal_component_evidence == 0
    assert grade.causal_component_explored is False
    assert grade.cross_incident_evidence_reference_count == 1


def test_a0_report_separates_comparable_and_legacy_metrics() -> None:
    report = a0_compatibility_baseline()

    assert report["comparable"]["completion_rate"].rate == 0.9
    assert report["comparable"]["cross_component_exploration"].numerator == 0
    assert report["legacy_non_comparable"]["service_exact_match"]["reason"] == (
        "free-text affected_component equality"
    )


def test_aggregate_exposes_reference_counts_and_safety_totals() -> None:
    incident_id = uuid4()
    evidence = _evidence(incident_id, workload=WorkloadComponentId.PAYMENT_SERVICE)
    grade = grade_a1_run(
        _artifact(
            "V020-001",
            evidence=[evidence],
            hypothesis=_hypothesis("V020-001", [evidence.evidence_id, UUID(int=2)]),
        ),
        A1_TARGET_BY_SCENARIO["V020-001"],
    )
    aggregate = aggregate_a1_grades([grade])

    assert aggregate.fabricated_evidence_reference_count == 1
    assert aggregate.cross_incident_evidence_reference_count == 0
    assert aggregate.safety.fabricated_evidence == 0


def test_stop_is_valid_but_excluded_from_reference_integrity_denominator() -> None:
    incident_id = uuid4()
    evidence = _evidence(incident_id, workload=WorkloadComponentId.ORDER_WORKER)
    artifact = _artifact(
        "V020-008",
        evidence=[evidence],
        stop=CausalStopDecision(
            stop_reason=StopReason.INSUFFICIENT_EVIDENCE,
            considered_components=[WorkloadComponentId.ORDER_WORKER],
            considered_resources=[],
            missing_evidence_categories=[EvidenceCategory.MESSAGING],
        ),
        termination=TerminationReason.AGENT_STOPPED,
    )

    grade = grade_a1_run(artifact, A1_TARGET_BY_SCENARIO["V020-008"])
    aggregate = aggregate_a1_grades([grade])

    assert grade.valid_stop is True
    assert grade.evidence_reference_integrity is None
    assert aggregate.evidence_reference_integrity.denominator == 0
    assert aggregate.evidence_reference_integrity.rate is None
    assert aggregate.valid_stop_count == 1


def test_exploration_change_acquisition_and_unnecessary_search_metrics() -> None:
    incident_id = uuid4()
    order = _evidence(incident_id, workload=WorkloadComponentId.ORDER_SERVICE)
    payment = _evidence(incident_id, workload=WorkloadComponentId.PAYMENT_SERVICE)
    cross = _artifact(
        "V020-003",
        evidence=[order, payment],
        hypothesis=_hypothesis("V020-003", [payment.evidence_id]),
        tools=["service_latency", "service_latency"],
        arguments=[{"service": "order-service"}, {"service": "payment-service"}],
    )
    missed = _artifact(
        "V020-003",
        evidence=[order],
        hypothesis=_hypothesis("V020-003", [order.evidence_id]),
        tools=["service_latency"],
        arguments=[{"service": "order-service"}],
    )
    change = _evidence(
        incident_id,
        workload=WorkloadComponentId.PAYMENT_SERVICE,
        tool="recent_configuration_changes",
    )
    change_artifact = _artifact(
        "V020-010",
        evidence=[change],
        hypothesis=_hypothesis("V020-010", [change.evidence_id]),
        tools=["recent_configuration_changes"],
        arguments=[{"deployment": "payment-service"}],
    )
    unnecessary = _artifact(
        "V020-005",
        evidence=[order, payment],
        hypothesis=_hypothesis("V020-005", [payment.evidence_id]),
        tools=["service_latency", "service_latency"],
        arguments=[{"service": "payment-service"}, {"service": "order-service"}],
    )

    grades = [
        grade_a1_run(cross, A1_TARGET_BY_SCENARIO["V020-003"]),
        grade_a1_run(
            missed,
            A1_TARGET_BY_SCENARIO["V020-003"].model_copy(update={"scenario_id": "A1-GEN-001"}),
        ),
        grade_a1_run(change_artifact, A1_TARGET_BY_SCENARIO["V020-010"]),
        grade_a1_run(unnecessary, A1_TARGET_BY_SCENARIO["V020-005"]),
    ]
    aggregate = aggregate_a1_grades(grades)

    assert grades[0].causal_component_explored is True
    assert grades[1].causal_component_explored is False
    assert grades[2].change_evidence_acquired is True
    assert grades[3].outside_alert_scope_executions == 1
    assert aggregate.cross_component_exploration_recall.numerator == 1
    assert aggregate.cross_component_exploration_recall.denominator == 2
    assert aggregate.change_evidence_acquisition_rate.rate == 1
    assert aggregate.unnecessary_outside_scope_exploration_rate.numerator == 1


def test_zero_subset_denominators_are_null_not_zero_percent() -> None:
    incident_id = uuid4()
    evidence = _evidence(incident_id, workload=WorkloadComponentId.PAYMENT_SERVICE)
    grade = grade_a1_run(
        _artifact(
            "V020-001",
            evidence=[evidence],
            hypothesis=_hypothesis("V020-001", [evidence.evidence_id]),
        ),
        A1_TARGET_BY_SCENARIO["V020-001"],
    )
    aggregate = aggregate_a1_grades([grade])
    assert aggregate.cross_component_exploration_recall.rate is None
    assert aggregate.change_evidence_acquisition_rate.rate is None

"""Change, evidence, and policy foundation tests."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from packages.changes.service import ChangeService
from packages.contracts import (
    ActionRequest,
    ActionType,
    ChangeType,
    Evidence,
    EvidenceSourceType,
    PolicyDecision,
    TimeWindow,
)
from packages.evidence.service import CrossIncidentEvidenceError, EvidenceService
from packages.policy.evaluator import PolicyConfig, PolicyEvaluator

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


def test_change_normalization_preserves_before_after_facts() -> None:
    service = ChangeService()
    change = service.capture(
        timestamp=NOW,
        resource_type="Deployment",
        resource_name="payment-service",
        change_type=ChangeType.UPDATED,
        before={"DB_POOL_SIZE": 20},
        after={"DB_POOL_SIZE": 5},
        revision="rev-7",
        source="kubernetes",
    )

    assert change.before == {"DB_POOL_SIZE": 20}
    assert change.after == {"DB_POOL_SIZE": 5}
    assert list(service.recent()) == [change]


def test_evidence_rejects_cross_incident_tool_reference() -> None:
    incident_a, incident_b, tool_call = uuid4(), uuid4(), uuid4()
    evidence = Evidence(
        incident_id=incident_a,
        source_type=EvidenceSourceType.METRIC,
        source_system="prometheus",
        observation={"value": 98},
        time_window=TimeWindow(starts_at=NOW, ends_at=NOW),
        tool_call_id=tool_call,
        raw_result_reference="prometheus://result/1",
        collected_at=NOW,
    )
    service = EvidenceService()
    service.register_tool_call(tool_call, incident_b)

    with pytest.raises(CrossIncidentEvidenceError):
        service.add(evidence)


def test_policy_fails_closed_for_all_foundation_writes() -> None:
    action = ActionRequest(
        incident_id=uuid4(),
        action_type=ActionType.RESTART_DEPLOYMENT,
        target="sre-demo/payment-service",
        reason="foundation test",
    )

    result = PolicyEvaluator(PolicyConfig()).evaluate(action)
    explicitly_enabled = PolicyEvaluator(PolicyConfig(writes_enabled=True)).evaluate(action)
    unknown = PolicyEvaluator(PolicyConfig()).evaluate_raw({"action_type": "delete_namespace"})
    missing_config = PolicyEvaluator(None).evaluate(action)

    assert result.decision is PolicyDecision.DENY
    assert explicitly_enabled.decision is PolicyDecision.DENY
    assert unknown.decision is PolicyDecision.DENY
    assert missing_config.decision is PolicyDecision.DENY

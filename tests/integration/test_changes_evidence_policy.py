"""Change, evidence, and policy foundation tests."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from packages.changes.service import ChangeService
from packages.contracts import (
    ActionRequest,
    ActionType,
    ChangeRecord,
    ChangeScope,
    ChangeType,
    Evidence,
    EvidenceSourceType,
    PolicyDecision,
    TimeWindow,
)
from packages.evidence.service import CrossIncidentEvidenceError, EvidenceService
from packages.policy.evaluator import PolicyConfig, PolicyEvaluator
from packages.tools.live_backends import (
    CHANGE_LOOKBACK_SECONDS,
    MAX_CHANGE_QUERY_WINDOW_SECONDS,
    ControlPlaneChangeReader,
    KubernetesChangeBackend,
)

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


def test_change_query_is_bounded_to_incident_window_and_excludes_later_changes() -> None:
    """Historical readers return journal facts, never a current-state substitute."""
    service = ChangeService()
    before_incident = service.capture(
        timestamp=NOW + timedelta(minutes=10),
        resource_type="Deployment",
        resource_name="payment-service",
        change_type=ChangeType.UPDATED,
        before={"revision": "a"},
        after={"revision": "b"},
        revision="b",
        source="deterministic-harness",
    )
    service.capture(
        timestamp=NOW + timedelta(minutes=20),
        resource_type="Deployment",
        resource_name="payment-service",
        change_type=ChangeType.UPDATED,
        before={"revision": "b"},
        after={"revision": "c"},
        revision="c",
        source="deterministic-harness",
    )

    class KubernetesStub:
        def query(self, operation: str, parameters: dict[str, object]) -> dict[str, object]:
            raise AssertionError(f"current-state fallback must not run: {operation}")

    backend = KubernetesChangeBackend(
        KubernetesStub(),
        lambda _operation, parameters: list(
            service.between(
                resource_name=str(parameters["deployment"]),
                starts_at=datetime.fromisoformat(
                    str(parameters["observation_window"]["starts_at"])
                ),
                ends_at=datetime.fromisoformat(str(parameters["observation_window"]["ends_at"])),
            )
        ),
    )
    result = backend.query(
        "recent_deployment_changes",
        {
            "deployment": "payment-service",
            "observation_window": {
                "starts_at": (NOW + timedelta(minutes=15)).isoformat(),
                "ends_at": (NOW + timedelta(minutes=16)).isoformat(),
            },
        },
    )

    assert backend.has_historical_source is True
    assert result["__temporal_mode"] == "HISTORICAL_CHANGE"
    assert [item["change_id"] for item in result["records"]] == [str(before_incident.change_id)]
    assert (
        result["__effective_time_window"]["starts_at"]
        == (NOW + timedelta(minutes=15) - timedelta(seconds=900)).isoformat()
    )


def test_change_reader_does_not_expand_already_expanded_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The transport reader validates and serializes the backend-owned window once."""
    requested: dict[str, str] = {}

    class Response:
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            return b"[]"

    def fake_urlopen(request: object, timeout: float) -> Response:
        assert timeout == 5.0
        url = str(request.full_url)  # type: ignore[attr-defined]
        from urllib.parse import parse_qs, urlparse

        requested.update({key: values[0] for key, values in parse_qs(urlparse(url).query).items()})
        return Response()

    monkeypatch.setattr("packages.tools.live_backends.urlopen", fake_urlopen)
    incident_start = NOW
    incident_end = NOW + timedelta(seconds=340)
    expanded_start = incident_start - timedelta(seconds=CHANGE_LOOKBACK_SECONDS)
    reader = ControlPlaneChangeReader("http://control-plane/api/v1/changes")

    assert (
        reader.query(
            "recent_deployment_changes",
            {
                "deployment": "order-worker",
                "observation_window": {
                    "starts_at": expanded_start.isoformat(),
                    "ends_at": incident_end.isoformat(),
                },
            },
        )
        == []
    )
    assert requested["starts_at"] == expanded_start.isoformat()
    assert requested["ends_at"] == incident_end.isoformat()


def test_change_window_boundaries_are_expanded_once() -> None:
    """Change queries accept incident-plus-lookback, but reject larger windows."""
    from packages.tools.live_backends import _change_time_window, _parse_observation_window

    normal_start = NOW
    normal_end = NOW + timedelta(seconds=340)
    _, _, normal = _change_time_window(
        {
            "observation_window": {
                "starts_at": normal_start.isoformat(),
                "ends_at": normal_end.isoformat(),
            }
        }
    )
    parsed_start, parsed_end, _ = _parse_observation_window(
        {"observation_window": normal}, maximum_seconds=MAX_CHANGE_QUERY_WINDOW_SECONDS
    )
    assert parsed_end - parsed_start == timedelta(seconds=340 + CHANGE_LOOKBACK_SECONDS)

    max_end = NOW + timedelta(seconds=900)
    _, _, maximum = _change_time_window(
        {
            "observation_window": {
                "starts_at": NOW.isoformat(),
                "ends_at": max_end.isoformat(),
            }
        }
    )
    parsed_start, parsed_end, _ = _parse_observation_window(
        {"observation_window": maximum}, maximum_seconds=MAX_CHANGE_QUERY_WINDOW_SECONDS
    )
    assert parsed_end - parsed_start == timedelta(seconds=MAX_CHANGE_QUERY_WINDOW_SECONDS)

    with pytest.raises(ValueError, match="observation_window"):
        _parse_observation_window(
            {
                "observation_window": {
                    "starts_at": NOW.isoformat(),
                    "ends_at": (
                        NOW + timedelta(seconds=MAX_CHANGE_QUERY_WINDOW_SECONDS + 1)
                    ).isoformat(),
                }
            },
            maximum_seconds=MAX_CHANGE_QUERY_WINDOW_SECONDS,
        )


def test_change_reader_selects_factual_scope_per_operation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deployment and configuration readers cannot silently return the same facts."""
    requested_scopes: list[str] = []

    class Response:
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            return b"[]"

    def fake_urlopen(request: object, timeout: float) -> Response:
        assert timeout == 5.0
        from urllib.parse import parse_qs, urlparse

        query = parse_qs(urlparse(str(request.full_url)).query)  # type: ignore[attr-defined]
        requested_scopes.append(query["scope"][0])
        return Response()

    monkeypatch.setattr("packages.tools.live_backends.urlopen", fake_urlopen)
    parameters = {
        "deployment": "order-worker",
        "observation_window": {
            "starts_at": NOW.isoformat(),
            "ends_at": (NOW + timedelta(minutes=5)).isoformat(),
        },
    }
    reader = ControlPlaneChangeReader("http://control-plane/api/v1/changes")

    reader.query("recent_deployment_changes", parameters)
    reader.query("recent_configuration_changes", parameters)

    assert requested_scopes == [ChangeScope.DEPLOYMENT.value, ChangeScope.CONFIGURATION.value]


def test_change_reader_parses_json_records_at_the_http_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Strict domain validation accepts serialized UUID/datetime values from HTTP JSON."""
    record = ChangeRecord(
        timestamp=NOW,
        resource_type="Deployment",
        resource_name="order-worker",
        change_type=ChangeType.UPDATED,
        scope=ChangeScope.DEPLOYMENT,
        before={"revision": "a"},
        after={"revision": "b"},
        revision="b",
        source="deterministic-harness",
    )

    class Response:
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            return json.dumps([record.model_dump(mode="json")]).encode()

    def fake_urlopen(_request: object, timeout: float) -> Response:
        assert timeout == 5.0
        return Response()

    monkeypatch.setattr("packages.tools.live_backends.urlopen", fake_urlopen)
    reader = ControlPlaneChangeReader("http://control-plane/api/v1/changes")

    assert reader.query(
        "recent_deployment_changes",
        {
            "deployment": "order-worker",
            "observation_window": {
                "starts_at": NOW.isoformat(),
                "ends_at": (NOW + timedelta(minutes=5)).isoformat(),
            },
        },
    ) == [record]


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

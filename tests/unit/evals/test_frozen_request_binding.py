"""Missing, conflicting and decoy frozen evidence must not corroborate a relation."""

import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from scripts.frozen_request_binding_analysis import main

from packages.evals.frozen_request_binding import FrozenRequestEvidence, corroborate_frozen_requests


def frozen() -> dict[str, Any]:
    trace_id, client_id, parent_id = "a" * 32, "b" * 16, "c" * 16
    provenance = {"incident_id": "incident", "run_id": "run", "captured_at": "2026-10-13T15:02:30Z"}

    def span(kind: str, sid: str, start: str, end: str, attrs: dict[str, str]) -> dict[str, Any]:
        return {
            **provenance,
            "source_reference": f"TRACE:{sid}",
            "record": {
                "trace_id": trace_id,
                "span_id": sid,
                "parent_span_id": parent_id if kind == "CLIENT" else None,
                "service": "order",
                "span_kind": kind,
                "start_at": start,
                "end_at": end,
                "status": "ERROR" if kind == "CLIENT" else "UNKNOWN",
                "evidence_id": f"span:{sid}",
                "semantic_attributes": {"http.request.header.x_request_id": "req", **attrs},
            },
        }

    return {
        "incident_id": "incident",
        "run_id": "run",
        "claim_id": "peer-claim",
        "episode_start": "2026-10-13T15:00:00Z",
        "episode_end": "2026-10-13T15:02:00Z",
        "frozen_cutoff": "2026-10-13T15:03:00Z",
        "caller_service": "order",
        "configured_peer": "payment",
        "logs": [
            {
                **provenance,
                "source_reference": "LOG:1",
                "record": {
                    "service": "order",
                    "at": "2026-10-13T15:01:10Z",
                    "severity": "ERROR",
                    "message": "dependency failed",
                    "evidence_id": "log:1",
                    "attributes": {
                        "request_id": "req",
                        "trace_id": trace_id,
                        "span_id": client_id,
                        "parent_span_id": parent_id,
                        "server.address": "payment",
                        "error.type": "ConnectionRefusedError",
                        "http.request.method": "POST",
                    },
                },
            }
        ],
        "spans": [
            span(
                "CLIENT",
                client_id,
                "2026-10-13T15:01:00Z",
                "2026-10-13T15:01:20Z",
                {
                    "server.address": "payment",
                    "error.type": "ConnectionRefusedError",
                    "http.request.method": "POST",
                },
            ),
            span(
                "SERVER",
                parent_id,
                "2026-10-13T15:00:50Z",
                "2026-10-13T15:01:30Z",
                {"http.response.status_code": "500"},
            ),
        ],
    }


def evaluate(data: dict[str, Any]) -> Any:
    before = deepcopy(data)
    report = corroborate_frozen_requests(FrozenRequestEvidence.model_validate(data))
    assert data == before
    assert report.admission_change is False
    assert (
        report.causal_origin == report.same_fault_across_episodes == report.recovery == "UNASSESSED"
    )
    assert report.source_authenticity == "NOT_VERIFIED"
    return report.relations[0]


def test_corroborates_unique_failed_direct_request_relation() -> None:
    result = evaluate(frozen())
    assert result.request_relation == "CORROBORATED"
    assert len(result.evidence_ids) == len(result.source_references) == 3
    assert result.deficits == ()


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("trace_decoy", "CLIENT_SPAN_MISSING_OR_NON_UNIQUE"),
        ("missing_parent", "PARENT_SPAN_MISSING_OR_NON_UNIQUE"),
        ("duplicate_client", "CLIENT_SPAN_MISSING_OR_NON_UNIQUE"),
        ("parent_link", "DIRECT_PARENT_LINK_MISMATCH"),
        ("request_id", "SPAN_REQUEST_ID_MISSING_OR_MISMATCHED"),
        ("different_peer", "CLIENT_CONFIGURED_PEER_MISSING_OR_MISMATCHED"),
        ("different_error", "CLIENT_FAILURE_TYPE_MISSING_OR_MISMATCHED"),
        ("successful_parent", "PARENT_5XX_NOT_RECORDED"),
        ("other_run", "SOURCE_INCIDENT_OR_RUN_MISMATCH"),
        ("late_capture", "SOURCE_CAPTURE_AFTER_FROZEN_CUTOFF"),
        ("old_log", "LOG_OR_SPAN_OUTSIDE_NESTED_EPISODE_INTERVAL"),
        ("unzoned_log", "LOG_OR_SPAN_TIME_MISSING_OR_UNZONED"),
        ("legacy_log", "LOG_TRACE_SPAN_OR_PARENT_ID_MISSING_OR_INVALID"),
    ],
)
def test_missing_conflicting_and_decoy_records_stay_unassessed(mutation: str, code: str) -> None:
    data = frozen()
    client, parent = data["spans"]
    log = data["logs"][0]
    if mutation == "trace_decoy":
        client["record"]["trace_id"] = "d" * 32
    elif mutation == "missing_parent":
        data["spans"] = [client]
    elif mutation == "duplicate_client":
        data["spans"].append(deepcopy(client))
    elif mutation == "parent_link":
        client["record"]["parent_span_id"] = "d" * 16
    elif mutation == "request_id":
        parent["record"]["semantic_attributes"]["http.request.header.x_request_id"] = "other"
    elif mutation == "different_peer":
        client["record"]["semantic_attributes"]["server.address"] = "kafka"
    elif mutation == "different_error":
        client["record"]["semantic_attributes"]["error.type"] = "TimeoutError"
    elif mutation == "successful_parent":
        parent["record"]["semantic_attributes"]["http.response.status_code"] = "201"
    elif mutation == "other_run":
        parent["run_id"] = "other"
    elif mutation == "late_capture":
        log["captured_at"] = "2026-10-13T15:04:00Z"
    elif mutation == "old_log":
        log["record"]["at"] = "2026-10-13T13:00:00Z"
    elif mutation == "unzoned_log":
        log["record"]["at"] = "2026-10-13T15:01:10"
    elif mutation == "legacy_log":
        log["record"]["attributes"] = {}
    result = evaluate(data)
    assert result.request_relation == "UNASSESSED"
    assert code in result.deficits


def test_cli_preserves_source_and_reports_verification_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, output = tmp_path / "source.json", tmp_path / "report.json"
    original = json.dumps(frozen())
    source.write_text(original)
    monkeypatch.setattr(sys, "argv", ["adapter", str(source), "--output", str(output)])
    main()
    report = json.loads(output.read_text())["reports"][0]
    assert report["relations"][0]["request_relation"] == "CORROBORATED"
    assert report["manifest_membership"] == "NOT_VERIFIED_BY_FILE_ADAPTER"
    assert source.read_text() == original
    monkeypatch.setattr(sys, "argv", ["adapter", str(source), "--output", str(source)])
    with pytest.raises(SystemExit):
        main()
    assert source.read_text() == original


def test_no_provider_or_resolver_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    import packages.rca.engine as engine
    import packages.rca.live as live

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Frozen adapter cannot call providers or resolver")

    monkeypatch.setattr(engine, "build_case", forbidden)
    monkeypatch.setattr(engine, "diagnose_case", forbidden)
    monkeypatch.setattr(live.LokiLogReader, "error_logs", forbidden)
    assert evaluate(frozen()).request_relation == "CORROBORATED"


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("capture_before_event", "SOURCE_CAPTURE_BEFORE_RECORDED_EVENT"),
        ("duplicate_reference", "SOURCE_REFERENCES_NOT_DISTINCT"),
        ("nested_parent", "CLIENT_SERVER_KIND_NOT_RECORDED"),
    ],
)
def test_provenance_and_direct_server_scope_are_required(mutation: str, code: str) -> None:
    data = frozen()
    if mutation == "capture_before_event":
        data["logs"][0]["captured_at"] = "2026-10-13T14:00:00Z"
    elif mutation == "duplicate_reference":
        data["spans"][0]["source_reference"] = data["logs"][0]["source_reference"]
    else:
        data["spans"][1]["record"]["span_kind"] = "INTERNAL"
    result = evaluate(data)
    assert result.request_relation == "UNASSESSED"
    assert code in result.deficits


def test_malformed_status_does_not_raise_or_corroborate() -> None:
    data = frozen()
    data["spans"][1]["record"]["semantic_attributes"]["http.response.status_code"] = "⁵⁰⁰"
    assert "PARENT_5XX_NOT_RECORDED" in evaluate(data).deficits

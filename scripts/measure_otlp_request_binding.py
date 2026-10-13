"""DEV-only real collector/Loki/Tempo measurement with independent denominators."""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

from packages.evals.frozen_request_binding import (
    FrozenLog,
    FrozenRequestEvidence,
    FrozenSpan,
    corroborate_frozen_requests,
)
from packages.rca.investigation.tempo import (
    TempoConfig,
    TempoHttpError,
    TempoTraceReader,
    parse_tempo_trace_json,
)
from packages.rca.live import LokiLogReader
from packages.rca.model import TraceSpanObservation
from scripts.measure_local_request_binding import measure


def capture(
    output: Path, *, otlp: str, loki: str, tempo: str, repeats: int, wait_seconds: int
) -> dict[str, Any]:
    if output.exists():
        raise ValueError("Use a fresh output directory")
    output.mkdir(parents=True)
    protocol = {
        "profile": "DEV_REAL_OTLP_LOKI_TEMPO",
        "repeats": repeats,
        "wait_seconds": wait_seconds,
        "discovery": "SERVICE_NAME_SEARCH_NOT_LOCAL_TRACE_ID_REPAIR",
        "tempo_search_limit": 500,
        "loki_limit": 500,
        "rca_evaluated": False,
        "endpoints": {"otlp": otlp, "loki": loki, "tempo": tempo},
    }
    paths = [
        "scripts/measure_otlp_request_binding.py",
        "scripts/measure_local_request_binding.py",
        "packages/rca/live.py",
        "packages/rca/log_attributes.py",
        "packages/evals/measurement_capture_manifest.py",
    ]
    protocol["source_sha256"] = {
        path: sha256(Path(path).read_bytes()).hexdigest() for path in paths
    }
    for path in paths:
        destination = output / "source-snapshot" / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(Path(path).read_bytes())
    (output / "transport-protocol.json").write_text(json.dumps(protocol, indent=2) + "\n")
    reference = measure(output / "source-reference", repeats, 6, otlp_endpoint=otlp)
    source = FrozenRequestEvidence.model_validate_json(
        (output / "source-reference/frozen-evidence.json").read_text()
    )
    ledger = json.loads((output / "source-reference/driver-ledger.json").read_text())
    reader = TempoTraceReader(TempoConfig(tempo))
    logs_reader = LokiLogReader(loki, limit=500)
    deadline = time.monotonic() + wait_seconds
    attempts = []
    frozen = None
    trace_ids = []
    poll = 0
    while True:
        poll += 1
        end = datetime.now(UTC) + timedelta(seconds=1)
        logs = logs_reader.error_logs([source.caller_service], source.episode_start, end)
        search = reader._get_json(
            "/api/search",
            {
                "q": '{ resource.service.name = "' + source.caller_service + '" }',
                "start": int(source.episode_start.timestamp()) - 1,
                "end": int(end.timestamp()),
                "limit": 500,
            },
            1_048_576,
        )
        raw_traces = search.get("traces", [])
        if not isinstance(raw_traces, list):
            raise ValueError("Malformed Tempo discovery result")
        trace_ids = sorted({str(row["traceID"]) for row in raw_traces})
        spans: list[TraceSpanObservation] = []
        missing = []
        raw_dir = output / "provider-reads" / str(poll)
        raw_dir.mkdir(parents=True)
        (raw_dir / "search.json").write_text(json.dumps(search, indent=2) + "\n")
        (raw_dir / "logs.json").write_text(
            json.dumps([row.model_dump(mode="json") for row in logs], indent=2) + "\n"
        )
        for trace_id in trace_ids:
            try:
                payload = reader._get_json(f"/api/v2/traces/{trace_id}", {}, 4_194_304)
            except TempoHttpError as error:
                if error.status_code != 404:
                    raise
                missing.append(trace_id)
                continue
            # TempoHttpError is handled by the provider; unavailable traces are never repaired locally.
            (raw_dir / f"trace-{trace_id}.json").write_text(json.dumps(payload) + "\n")
            spans.extend(parse_tempo_trace_json(payload))
        captured = datetime.now(UTC)
        provenance: dict[str, Any] = {
            "incident_id": source.incident_id,
            "run_id": source.run_id,
            "captured_at": captured,
        }
        frozen = FrozenRequestEvidence(
            incident_id=source.incident_id,
            run_id=source.run_id,
            episode_start=source.episode_start,
            episode_end=captured,
            frozen_cutoff=captured,
            caller_service=source.caller_service,
            configured_peer=source.configured_peer,
            logs=tuple(
                FrozenLog(
                    **provenance, source_reference=f"loki-read:{poll}:{row.evidence_id}", record=row
                )
                for row in logs
            ),
            spans=tuple(
                FrozenSpan(
                    **provenance,
                    source_reference=f"tempo-read:{poll}:{row.trace_id}:{row.span_id}",
                    record=row,
                )
                for row in spans
            ),
        )
        seen_requests = {
            span.semantic_attributes.get("http.request.header.x_request_id")
            for span in spans
            if span.span_kind == "SERVER"
        }
        error_requests = {
            row.attributes.get("request_id") for row in logs if row.attributes.get("error.type")
        }
        expected_errors = {row["request_id"] for row in ledger if row["scenario"] != "success"}
        expected_requests = {row["request_id"] for row in ledger}
        attempts.append(
            {
                "poll": poll,
                "at": captured.isoformat(),
                "logs": len(logs),
                "spans": len(spans),
                "discovered_trace_ids": len(trace_ids),
                "driver_requests_with_server_span": len(expected_requests & seen_requests),
                "driver_requests_with_dependency_error_log": len(expected_errors & error_requests),
                "search_metrics": search.get("metrics"),
                "missing_fetch_ids": missing,
            }
        )
        if expected_requests <= seen_requests and expected_errors <= error_requests:
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(5)
    assert frozen is not None
    report = corroborate_frozen_requests(frozen)
    assessments = {row.log_evidence_id: row for row in report.relations}
    by_request: dict[str, list[FrozenLog]] = {}
    for row in frozen.logs:
        if row.record.attributes.get("error.type"):
            by_request.setdefault(row.record.attributes.get("request_id", ""), []).append(row)
    counts: dict[str, Any] = {}
    for scenario in ("refusal", "timeout", "http503", "handled-http503", "success"):
        requests = [row for row in ledger if row["scenario"] == scenario]
        rows = [item for row in requests for item in by_request.get(row["request_id"], [])]
        counts[scenario] = {
            "driver_requests": len(requests),
            "requests_with_remote_dependency_error_log": sum(
                bool(by_request.get(row["request_id"])) for row in requests
            ),
            "requests_with_corroborated_relation": sum(
                any(
                    assessments[item.record.evidence_id].request_relation == "CORROBORATED"
                    for item in by_request.get(row["request_id"], [])
                )
                for row in requests
            ),
            "error_types": dict(Counter(row.record.attributes.get("error.type") for row in rows)),
            "deficits": dict(
                Counter(
                    code for row in rows for code in assessments[row.record.evidence_id].deficits
                )
            ),
        }
    expected = sum(counts[s]["driver_requests"] for s in ("refusal", "timeout", "http503"))
    corroborated = sum(
        counts[s]["requests_with_corroborated_relation"] for s in ("refusal", "timeout", "http503")
    )
    summary = {
        "profile": protocol["profile"],
        "rca_evaluated": False,
        "service": source.caller_service,
        "source_scenario_validation_passed": reference["scenario_validation_passed"],
        "source_reference_binding_coverage": reference["positive_binding_coverage"],
        "scenarios": counts,
        "remote_positive_binding_coverage": {
            "corroborated": corroborated,
            "driver_failed_requests": expected,
            "ratio": corroborated / expected,
        },
        "discovery": "SERVICE_NAME_SEARCH",
        "polls": attempts,
        "provider_search_limit_reached": len(trace_ids) >= 500,
        "loki_result_limit_reached": len(frozen.logs) >= 500,
        "manifest_membership": "NOT_YET_VERIFIED",
    }
    (output / "remote-evidence.json").write_text(frozen.model_dump_json(indent=2) + "\n")
    (output / "remote-binding-report.json").write_text(report.model_dump_json(indent=2) + "\n")
    from packages.evals.measurement_capture_manifest import freeze_measurement_capture

    loaded, digest = freeze_measurement_capture(output / "capture-manifest.sqlite", frozen)
    manifest_report = corroborate_frozen_requests(loaded)
    if [(r.log_evidence_id, r.request_relation, r.deficits) for r in manifest_report.relations] != [
        (r.log_evidence_id, r.request_relation, r.deficits) for r in report.relations
    ]:
        # Storage ordering may differ; compare relations by evidence ID instead.
        original_relations = {
            r.log_evidence_id: (r.request_relation, r.deficits) for r in report.relations
        }
        if original_relations != {
            r.log_evidence_id: (r.request_relation, r.deficits) for r in manifest_report.relations
        }:
            raise ValueError("Capture manifest changed relation assessments")
    summary.update(
        {
            "manifest_membership": "VERIFIED_CAPTURE_MANIFEST_ROUNDTRIP",
            "manifest_digest": digest,
            "manifest_logs": len(loaded.logs),
            "manifest_spans": len(loaded.spans),
            "manifest_corroborated_log_count": sum(
                r.request_relation == "CORROBORATED" for r in manifest_report.relations
            ),
        }
    )
    (output / "manifest-evidence.json").write_text(loaded.model_dump_json(indent=2) + "\n")
    (output / "manifest-binding-report.json").write_text(
        manifest_report.model_dump_json(indent=2) + "\n"
    )
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--otlp", default="http://127.0.0.1:14317")
    parser.add_argument("--loki", default="http://127.0.0.1:13100")
    parser.add_argument("--tempo", default="http://127.0.0.1:13200")
    parser.add_argument("--repeats", type=int, default=30)
    parser.add_argument("--wait-seconds", type=int, default=120)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 30 or not 1 <= args.wait_seconds <= 300:
        parser.error("repeats 1..30; wait-seconds 1..300")
    print(
        json.dumps(
            capture(
                args.output,
                otlp=args.otlp,
                loki=args.loki,
                tempo=args.tempo,
                repeats=args.repeats,
                wait_seconds=args.wait_seconds,
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

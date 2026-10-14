"""DEV exact log-ID trace fetch measurement; no production investigation changes."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from packages.evals.frozen_request_binding import (
    FrozenRequestEvidence,
    FrozenSpan,
    corroborate_frozen_requests,
)
from packages.evals.targeted_request_pairs import fetch_log_request_pairs
from packages.rca.investigation.tempo import parse_tempo_trace_json
from packages.rca.model import TraceSpanObservation
from packages.rca.provider_adapter import DevelopmentTransportProbe


def measure(
    capture: Path, output: Path, *, tempo: str | None = None, max_reads: int = 4
) -> dict[str, Any]:
    if not 1 <= max_reads <= 4:
        raise ValueError("DEV measurement supports 1..4 bounded reads")
    if output.exists():
        raise ValueError("Use a fresh output directory")
    source = FrozenRequestEvidence.model_validate_json(
        (capture / "remote-evidence.json").read_text()
    )
    ledger = json.loads((capture / "source-reference/driver-ledger.json").read_text())
    # Filter only by source log semantics, never driver outcome or locally held trace IDs.
    logs = tuple(
        sorted(
            (
                item
                for item in source.logs
                if item.record.service == source.caller_service
                and item.record.attributes.get("server.address") == source.configured_peer
                and item.record.attributes.get("error.type") == "ConnectionRefusedError"
            ),
            key=lambda item: (item.record.at, item.record.evidence_id),
        )
    )
    output.mkdir(parents=True)
    (output / "protocol.json").write_text(
        json.dumps(
            {
                "profile": "LIVE_TEMPO_EXACT_LOG_ID_FETCH"
                if tempo
                else "ARCHIVED_TEMPO_EXACT_LOG_ID_FETCH",
                "selection": "CALLER_PEER_CONNECTION_REFUSED_LOG_ATTRIBUTES",
                "max_reads": max_reads,
                "max_fetches_per_read": 8,
                "max_retained_spans_per_read": 32,
                "production_capability_registered": False,
                "rca_evaluated": False,
                "source_sha256": {
                    str(p): sha256(p.read_bytes()).hexdigest()
                    for p in (
                        capture / "remote-evidence.json",
                        capture / "source-reference/driver-ledger.json",
                        Path(__file__),
                        Path("packages/evals/targeted_request_pairs.py"),
                    )
                },
            },
            indent=2,
        )
        + "\n"
    )
    probe = DevelopmentTransportProbe("http://unused-loki", tempo) if tempo else None
    attempts: list[str] = []

    def fetch(trace_id: str) -> tuple[TraceSpanObservation, ...]:
        attempts.append(trace_id)
        if probe:
            log = next(
                item.record for item in logs if item.record.attributes.get("trace_id") == trace_id
            )
            payload = probe.trace_for_log(log, source.episode_start, source.episode_end)
        else:
            # Raw responses, not the SDK/in-memory reference, are the only trace source.
            candidates = [
                path
                for path in (capture / "provider-reads").glob("*/trace-*.json")
                if re.fullmatch(r"trace-[0-9a-f]{1,32}\.json", path.name)
                and path.stem.removeprefix("trace-").zfill(32) == trace_id
            ]
            if len(candidates) != 1:
                raise ValueError("Exactly one archived provider trace response required")
            payload = json.loads(candidates[0].read_text())
        (output / f"trace-{trace_id}.json").write_text(json.dumps(payload) + "\n")
        return parse_tempo_trace_json(payload)

    spans: dict[str, TraceSpanObservation] = {}
    reads = []
    # 30 source refusal logs require four bounded reads; no claim of equal fetch cost.
    for offset in range(0, min(len(logs), max_reads * 8), 8):
        batch = logs[offset : offset + 8]
        result = fetch_log_request_pairs([row.record for row in batch], fetch)
        spans.update({span.evidence_id: span for span in result.spans})
        reads.append(
            {
                "source_logs": len(batch),
                "trace_fetch_attempts": len(result.attempted_trace_ids),
                "raw_returned_spans": result.returned_span_count,
                "retained_pair_spans": len(result.spans),
                "deficits": result.deficits,
            }
        )
    captured = datetime.now(UTC) if probe else source.frozen_cutoff
    evidence = source.model_copy(
        update={
            "logs": logs,
            "frozen_cutoff": captured,
            "spans": tuple(
                FrozenSpan(
                    incident_id=source.incident_id,
                    run_id=source.run_id,
                    source_reference=f"targeted-provider:{s.trace_id}:{s.span_id}",
                    captured_at=captured,
                    record=s,
                )
                for s in spans.values()
            ),
        }
    )
    report = corroborate_frozen_requests(evidence)
    by_log = {row.record.evidence_id: row.record for row in logs}
    bound = {
        by_log[row.log_evidence_id].attributes["request_id"]
        for row in report.relations
        if row.request_relation == "CORROBORATED"
    }
    expected = {row["request_id"] for row in ledger if row["scenario"] == "refusal"}
    summary = {
        "profile": "LIVE_TEMPO_EXACT_LOG_ID_FETCH"
        if probe
        else "ARCHIVED_TEMPO_EXACT_LOG_ID_FETCH",
        "source_refusal_logs": len(logs),
        "logs_outside_read_budget": max(0, len(logs) - max_reads * 8),
        "driver_refusal_requests": len(expected),
        "bound_refusal_requests": len(bound & expected),
        "bound_outside_refusal_driver_set": len(bound - expected),
        "bounded_reads": reads,
        "http_fetch_attempts": len(attempts),
        "unique_trace_fetch_attempts": len(set(attempts)),
        "retained_spans": len(spans),
        "deficits": dict(Counter(d for row in report.relations for d in row.deficits)),
        "rollout_guard_evaluated": False,
        "rca_evaluated": False,
        "decision_change": False,
    }
    (output / "evidence.json").write_text(evidence.model_dump_json(indent=2) + "\n")
    (output / "binding-report.json").write_text(report.model_dump_json(indent=2) + "\n")
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--tempo", help="Optional live DEV Tempo URL; otherwise use archived responses"
    )
    parser.add_argument("--max-reads", type=int, default=4)
    args = parser.parse_args()
    print(
        json.dumps(
            measure(args.capture, args.output, tempo=args.tempo, max_reads=args.max_reads), indent=2
        )
    )


if __name__ == "__main__":
    main()

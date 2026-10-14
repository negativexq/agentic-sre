"""Paired DEV service-search versus exact-log retrieval HTTP cost measurement."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from packages.evals.frozen_request_binding import (
    FrozenRequestEvidence,
    FrozenSpan,
    corroborate_frozen_requests,
)
from packages.evals.tempo_cost_proxy import measure_tempo_http
from packages.rca.investigation.tempo import parse_tempo_trace_json
from packages.rca.model import TraceSpanObservation
from packages.rca.provider_adapter import DevelopmentTransportProbe
from scripts.measure_targeted_request_pairs import measure as targeted


def measure(capture: Path, output: Path, *, tempo: str, rounds: int = 3) -> dict[str, Any]:
    if output.exists() or not 1 <= rounds <= 5:
        raise ValueError("Fresh output directory and 1..5 rounds required")
    source = FrozenRequestEvidence.model_validate_json(
        (capture / "remote-evidence.json").read_text()
    )
    ledger = json.loads((capture / "source-reference/driver-ledger.json").read_text())
    expected = {r["request_id"] for r in ledger if r["scenario"] == "refusal"}
    logs = tuple(
        item
        for item in source.logs
        if item.record.attributes.get("error.type") == "ConnectionRefusedError"
    )
    output.mkdir(parents=True)
    paths = [
        capture / "remote-evidence.json",
        capture / "source-reference/driver-ledger.json",
        Path(__file__),
        Path("packages/evals/tempo_cost_proxy.py"),
        Path("packages/rca/provider_adapter.py"),
        Path("scripts/measure_targeted_request_pairs.py"),
    ]
    (output / "protocol.json").write_text(
        json.dumps(
            {
                "rounds": rounds,
                "order": "ROTATING_THREE_STRATEGIES",
                "search": "DEV_SERVICE_NAME_LIMIT_8_THEN_MAX_32_SPANS",
                "targeted": "REFUSAL_LOG_IDENTITIES_FOUR_READS_MAX_8_FETCHES_EACH",
                "byte_scope": "HTTP_RESPONSE_BODY_EXCLUDES_HEADERS_TLS_REQUEST_BYTES",
                "search_is_production_kubernetes_equivalent": False,
                "rca_evaluated": False,
                "source_sha256": {str(p): sha256(p.read_bytes()).hexdigest() for p in paths},
            },
            indent=2,
        )
        + "\n"
    )
    results = []
    strategies = ("search", "targeted_one_read", "targeted")
    for index in range(rounds):
        ordered = strategies[index % 3 :] + strategies[: index % 3]
        for strategy in ordered:
            folder = output / f"round-{index + 1}-{strategy}"
            with measure_tempo_http(tempo) as (endpoint, reads):
                start = time.perf_counter()
                if strategy.startswith("targeted"):
                    result = targeted(
                        capture,
                        folder,
                        tempo=endpoint,
                        max_reads=1 if strategy == "targeted_one_read" else 4,
                    )
                    bound = result["bound_refusal_requests"]
                    retained = result["retained_spans"]
                else:
                    folder.mkdir()
                    probe = DevelopmentTransportProbe("http://unused-loki", endpoint)
                    payload = probe.search(
                        source.caller_service, source.episode_start, source.episode_end, limit=8
                    )
                    (folder / "search.json").write_text(json.dumps(payload) + "\n")
                    spans: list[TraceSpanObservation] = []
                    candidates = payload.get("traces", [])
                    if not isinstance(candidates, list) or not all(
                        isinstance(row, dict) for row in candidates
                    ):
                        raise ValueError("Malformed DEV search response")
                    ids = list(dict.fromkeys(str(r["traceID"]) for r in candidates))[:8]
                    for tid in ids:
                        raw = probe.trace(tid)
                        (folder / f"trace-{tid}.json").write_text(json.dumps(raw) + "\n")
                        spans.extend(parse_tempo_trace_json(raw))
                    spans = sorted(
                        spans, key=lambda s: (s.start_at, s.trace_id, s.span_id, s.evidence_id)
                    )[:32]
                    at = datetime.now(UTC)
                    evidence = source.model_copy(
                        update={
                            "logs": logs,
                            "frozen_cutoff": at,
                            "spans": tuple(
                                FrozenSpan(
                                    incident_id=source.incident_id,
                                    run_id=source.run_id,
                                    source_reference=f"cost:{s.trace_id}:{s.span_id}",
                                    captured_at=at,
                                    record=s,
                                )
                                for s in spans
                            ),
                        }
                    )
                    binding_report = corroborate_frozen_requests(evidence)
                    bylog = {r.record.evidence_id: r.record for r in logs}
                    bound = len(
                        {
                            bylog[r.log_evidence_id].attributes["request_id"]
                            for r in binding_report.relations
                            if r.request_relation == "CORROBORATED"
                        }
                        & expected
                    )
                    retained = len(spans)
                    (folder / "binding-report.json").write_text(
                        binding_report.model_dump_json(indent=2) + "\n"
                    )
                elapsed = time.perf_counter() - start
            row = {
                "round": index + 1,
                "strategy": strategy,
                "http_requests": len(reads),
                "response_body_bytes": sum(r["response_body_bytes"] for r in reads),
                "upstream_seconds": sum(r["upstream_seconds"] for r in reads),
                "strategy_wall_seconds": elapsed,
                "retained_spans": retained,
                "bound_refusal_requests": bound,
                "driver_refusal_requests": len(expected),
                "failed_http_responses": sum(r["status"] != 200 for r in reads),
            }
            (folder / "http-cost.json").write_text(
                json.dumps({"summary": row, "reads": reads}, indent=2) + "\n"
            )
            results.append(row)
    medians = {
        strategy: {
            key: statistics.median(r[key] for r in results if r["strategy"] == strategy)
            for key in (
                "http_requests",
                "response_body_bytes",
                "upstream_seconds",
                "strategy_wall_seconds",
                "retained_spans",
                "bound_refusal_requests",
            )
        }
        for strategy in strategies
    }
    report = {
        "profile": "PAIRED_DEV_LIVE_HTTP_COST",
        "rounds": results,
        "medians": medians,
        "ratios_targeted_over_search": {
            key: medians["targeted"][key] / medians["search"][key]
            if medians["search"][key]
            else None
            for key in (
                "http_requests",
                "response_body_bytes",
                "upstream_seconds",
                "strategy_wall_seconds",
            )
        },
        "production_search_equivalence": False,
        "rca_evaluated": False,
        "decision_change": False,
    }
    (output / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--capture", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--tempo", default="http://127.0.0.1:13200")
    p.add_argument("--rounds", type=int, default=3)
    args = p.parse_args()
    print(
        json.dumps(
            measure(args.capture, args.output, tempo=args.tempo, rounds=args.rounds), indent=2
        )
    )


if __name__ == "__main__":
    main()

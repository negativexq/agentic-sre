"""Measure request binding under span caps, using immutable real DEV exports.

Counterfactual record slices are not provider-search or rollout-RCA evaluations.
No missing identities are supplied and no capture or diagnosis is modified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from packages.evals.frozen_request_binding import (
    FrozenRequestEvidence,
    FrozenSpan,
    corroborate_frozen_requests,
)
from packages.rca.investigation.environment import _select_runtime_trace_context
from packages.rca.model import EntityRef, InvestigationQuery


def measure(evidence: FrozenRequestEvidence, ledger: list[dict[str, Any]]) -> dict[str, Any]:
    request_ids = [row["request_id"] for row in ledger]
    if len(request_ids) != len(set(request_ids)):
        raise ValueError("Independent driver request IDs must be unique")
    expected = {row["request_id"] for row in ledger if 500 <= row["status"] < 600}
    refusal = {row["request_id"] for row in ledger if row["scenario"] == "refusal"}
    dependency_logs = tuple(
        log for log in evidence.logs if log.record.attributes.get("server.address")
    )
    log_ids = [log.record.evidence_id for log in dependency_logs]
    if len(log_ids) != len(set(log_ids)):
        raise ValueError("Dependency log evidence IDs must be unique")
    scoped = evidence.model_copy(update={"logs": dependency_logs})
    ordered = tuple(
        sorted(
            evidence.spans,
            key=lambda s: (
                s.record.start_at,
                s.record.trace_id,
                s.record.span_id,
                s.record.evidence_id,
            ),
        )
    )

    def score(label: str, spans: tuple[FrozenSpan, ...], scope: str) -> dict[str, Any]:
        report = corroborate_frozen_requests(scoped.model_copy(update={"spans": spans}))
        logs = {log.record.evidence_id: log.record for log in dependency_logs}
        bound = {
            logs[r.log_evidence_id].attributes["request_id"]
            for r in report.relations
            if r.request_relation == "CORROBORATED"
        }
        deficits = Counter(d for r in report.relations for d in r.deficits)
        return {
            "profile": label,
            "selection_scope": scope,
            "selected_spans": len(spans),
            "selected_traces": len({s.record.trace_id for s in spans}),
            "bound_failed_requests": len(bound & expected),
            "driver_failed_requests": len(expected),
            "bound_refusal_requests": len(bound & refusal),
            "driver_refusal_requests": len(refusal),
            "all_driver_refusal_requests_bound": refusal <= bound,
            "bound_non_failed_requests": len(bound - expected),
            "deficit_counts": dict(sorted(deficits.items())),
        }

    profiles = [score("full_capture", ordered, "SERVICE_NAME_DEV_CAPTURE")]
    for cap in (8, 16, 32):
        profiles.append(score(f"oldest_{cap}_spans", ordered[:cap], "COUNTERFACTUAL_SPAN_CAP"))
        profiles.append(score(f"newest_{cap}_spans", ordered[-cap:], "COUNTERFACTUAL_SPAN_CAP"))
    # Existing exact Kubernetes target selector: do not invent resource attributes.
    selected = _select_runtime_trace_context(
        tuple(s.record for s in ordered),
        EntityRef(kind="Deployment", namespace="sre-demo", name=evidence.caller_service),
        InvestigationQuery(start=evidence.episode_start, end=evidence.episode_end, limit=32),
        evidence.frozen_cutoff,
    )
    selected_ids = {span.evidence_id for span in selected}
    profiles.append(
        score(
            "existing_kubernetes_target_selector_32",
            tuple(s for s in ordered if s.record.evidence_id in selected_ids),
            "EXISTING_SELECTOR_ON_UNMODIFIED_EXPORT",
        )
    )
    return {
        "measurement": "REAL_DEV_EXPORT_OFFLINE_BINDING_BUDGET",
        "independent_driver_requests": len(ledger),
        "dependency_error_logs": len(dependency_logs),
        "source_spans": len(ordered),
        "profiles": profiles,
        "rca_evaluated": False,
        "rollout_guard_evaluated": False,
        "rollout_guard_unassessed_reason": "NO_KUBERNETES_ROLLOUT_CASE_OR_STRONG_WITNESS_IN_CAPTURE",
        "production_search_equivalence": "NOT_ESTABLISHED",
        "decision_change": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve() in {args.evidence.resolve(), args.ledger.resolve()}:
        parser.error("Output must not overwrite an input")
    if args.output.exists():
        parser.error("Use a fresh output path")
    evidence = FrozenRequestEvidence.model_validate_json(args.evidence.read_text())
    report = measure(evidence, json.loads(args.ledger.read_text()))
    report["inputs"] = [
        {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in (args.evidence, args.ledger)
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            [
                {
                    k: r[k]
                    for k in (
                        "profile",
                        "selected_spans",
                        "bound_failed_requests",
                        "bound_refusal_requests",
                        "all_driver_refusal_requests_bound",
                    )
                }
                for r in report["profiles"]
            ],
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

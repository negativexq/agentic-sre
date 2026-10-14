"""Offline DEV RCA comparison from actual isolated Kubernetes/OTLP capture."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from packages.evals.frozen_request_binding import FrozenRequestEvidence
from packages.evals.targeted_request_pairs import fetch_log_request_pairs
from packages.rca.engine import build_case, diagnose_case
from packages.rca.investigation.environment import _select_runtime_trace_context
from packages.rca.investigation.tempo import parse_tempo_trace_json
from packages.rca.live import events_from_bodies
from packages.rca.model import (
    Alert,
    EntityRef,
    InvestigationQuery,
    Lifecycle,
    ObjectVersion,
    TraceSpanObservation,
    TraceSpanStatus,
)
from packages.rca.provider_adapter import DevelopmentTransportProbe
from packages.rca.source import InMemorySource
from scripts.rollout_resolution_dev_proof import evaluate_source


def analyze(capture: Path, output: Path) -> dict[str, Any]:
    if output.exists():
        raise ValueError("Fresh output directory required")
    output.mkdir(parents=True)
    evidence = FrozenRequestEvidence.model_validate_json(
        (capture / "remote-evidence.json").read_text()
    )
    ledger = json.loads((capture / "driver-ledger.json").read_text())
    protocol = json.loads((capture / "protocol.json").read_text())
    if protocol.get("deletion_evidence") != "KUBERNETES_WATCH_DELETE":
        raise ValueError(
            "Capture has no explicit DELETE watch evidence; poll-derived tombstones grant no authority"
        )
    timeline = json.loads((capture / "timeline.json").read_text())
    versions = [
        ObjectVersion.model_validate(r)
        for r in json.loads((capture / "object-versions.json").read_text())
    ]
    watch_events = []
    for resource in ("pods", "replicasets"):
        watched = json.loads((capture / f"{resource}-watch.json").read_text())
        if watched["errors"]:
            raise ValueError("Lifecycle watch is incomplete or failed")
        watch_events.extend(watched["records"])
    authorized = {
        (
            r["event"]["object"]["kind"],
            r["event"]["object"]["metadata"]["uid"],
            "CREATED" if r["event"]["type"] == "ADDED" else "DELETED",
            datetime.fromisoformat(r["observed_at"]),
        )
        for r in watch_events
        if r["event"].get("type") in {"ADDED", "DELETED"}
    }
    for version in versions:
        if version.entity.kind in {"Pod", "ReplicaSet"} and version.lifecycle in {
            Lifecycle.CREATED,
            Lifecycle.DELETED,
        }:
            if (
                version.entity.kind,
                version.uid,
                version.lifecycle.value,
                version.observed_at,
            ) not in authorized:
                raise ValueError("Object lifecycle lacks matching explicit watch evidence")
    if len({r.evidence_id for r in versions}) != len(versions):
        raise ValueError("Object evidence identities are not unique")
    if timeline["poll_failures"]:
        raise ValueError("Object capture has failed polls")
    onset = datetime.fromisoformat(next(r["end"] for r in ledger if r["status"] >= 500))
    injection = datetime.fromisoformat(timeline["injection"])
    spans = [s.record for s in evidence.spans]
    baseline = [s for s in spans if s.start_at < injection]
    logs = [
        r.record
        for r in evidence.logs
        if r.record.attributes.get("server.address") == "payment-service"
    ]
    source = InMemorySource(
        name=protocol["namespace"],
        alert_items=[
            Alert(
                name="DEVDriverOrderFailure",
                service=evidence.caller_service,
                namespace=protocol["namespace"],
                starts_at=onset,
                labels={
                    "alertname": "DEVDriverOrderFailure",
                    "service": evidence.caller_service,
                    "namespace": protocol["namespace"],
                },
            )
        ],
        versions=versions,
        error_items=logs,
        trace_items=spans,
        event_items=events_from_bodies(json.loads((capture / "events.json").read_text())),
        cutoff=evidence.episode_end,
        alert_coverage_start=evidence.episode_start,
    )
    probe = DevelopmentTransportProbe("http://unused-loki", "http://127.0.0.1:13200")
    batch = probe.kubernetes_traces(
        protocol["namespace"], evidence.caller_service, onset, evidence.episode_end
    )
    (output / "kubernetes-query.json").write_text(
        json.dumps(asdict(batch), default=str, indent=2) + "\n"
    )
    selected = _select_runtime_trace_context(
        batch.spans,
        EntityRef(namespace=protocol["namespace"], kind="Deployment", name=evidence.caller_service),
        InvestigationQuery(start=onset, end=evidence.episode_end, limit=32),
        evidence.episode_end,
    )
    reads = []
    pairs = {}

    def fetch(tid: str) -> tuple[TraceSpanObservation, ...]:
        log = next(r for r in logs if r.attributes.get("trace_id") == tid)
        raw = probe.trace_for_log(log, evidence.episode_start, evidence.episode_end)
        (output / f"targeted-trace-{tid}.json").write_text(json.dumps(raw) + "\n")
        return parse_tempo_trace_json(raw)

    for offset in range(0, min(len(logs), 32), 8):
        read = fetch_log_request_pairs(logs[offset : offset + 8], fetch)
        reads.append(asdict(read))
        pairs.update({s.evidence_id: s for s in read.spans})
    (output / "targeted-reads.json").write_text(json.dumps(reads, default=str, indent=2) + "\n")
    profiles = {
        "without_failed_clients": [
            s for s in spans if not (s.span_kind == "CLIENT" and s.status is TraceSpanStatus.ERROR)
        ],
        "existing_kubernetes_query": [*baseline, *selected],
        "targeted_pairs": [*baseline, *pairs.values()],
        "full_reference": spans,
    }
    results = []
    for name, records in profiles.items():
        unique = {s.evidence_id: s for s in records}
        source.trace_items = list(unique.values())
        value = evaluate_source(
            source, name, assessment_kind="ACTUAL_KUBERNETES_DEV_SOURCE_COUNTERFACTUAL"
        )
        value["spans"] = len(unique)
        results.append(value)
        case = build_case(source)
        diagnosis = diagnose_case(case)
        (output / f"{name}.diagnosis.json").write_text(diagnosis.model_dump_json(indent=2) + "\n")
        (output / f"{name}.hypotheses.json").write_text(
            json.dumps([h.model_dump(mode="json") for h in case.hypotheses], indent=2) + "\n"
        )
    controls = []
    for name in ("missing_delete", "missing_baseline", "unbound_request", "backoff_uid_mismatch"):
        controlled = deepcopy(source)
        if name == "missing_delete":
            controlled.versions = [
                v for v in controlled.versions if v.lifecycle is not Lifecycle.DELETED
            ]
        elif name == "missing_baseline":
            controlled.trace_items = [s for s in controlled.trace_items if s.start_at >= injection]
        elif name == "unbound_request":
            first = controlled.error_items[0]
            controlled.error_items[0] = first.model_copy(
                update={"attributes": {**first.attributes, "request_id": "dev-unmatched-request"}}
            )
        else:
            controlled.event_items = [
                e.model_copy(update={"involved_uid": "different-pod-instance"})
                if e.reason == "BackOff"
                else e
                for e in controlled.event_items
            ]
        control = evaluate_source(
            controlled, name, assessment_kind="DERIVED_NEGATIVE_CONTROL_NOT_NEW_LIVE_INCIDENT"
        )
        controls.append(control)
        (output / f"control-{name}.json").write_text(json.dumps(control, indent=2) + "\n")
    by_name = {r["case"]: r for r in results}
    acceptance = {
        "targeted_guarded_resolved": by_name["targeted_pairs"]["guarded_experiment"]["resolution"]
        == "RESOLVED",
        "targeted_matches_full_reference": by_name["targeted_pairs"]["guarded_experiment"]
        == by_name["full_reference"]["guarded_experiment"],
        "all_current_log_members_accounted": all(
            not r["unaccounted_log_ids"] and not r["deficits"]
            for r in by_name["targeted_pairs"]["accounting"]
        ),
        "negative_controls_remain_guarded_ambiguous": all(
            r["guarded_experiment"]["resolution"] == "AMBIGUOUS" for r in controls
        ),
        "same_input_guard_invariants_hold": all(
            all(r["invariants"].values()) for r in [*results, *controls]
        ),
    }
    result = {
        "kind": "ACTUAL_KUBERNETES_DEV_OFFLINE_RECALCULATION",
        "alert_channel": "DRIVER_DEFINED_NOT_PROMETHEUS",
        "namespace": protocol["namespace"],
        "caller": evidence.caller_service,
        "dependency_logs": len(logs),
        "error_types": dict(Counter(r.attributes.get("error.type") for r in logs)),
        "general_query_spans": len(selected),
        "targeted_pair_spans": len(pairs),
        "targeted_http_fetches": sum(len(r["attempted_trace_ids"]) for r in reads),
        "profiles": results,
        "derived_controls": controls,
        "acceptance": acceptance,
        "production_rule_registered": False,
        "holdout_used": False,
    }
    (output / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--capture", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()
    r = analyze(args.capture, args.output)
    print(
        json.dumps(
            {
                **{k: v for k, v in r.items() if k not in {"profiles", "derived_controls"}},
                "profiles": [
                    {k: r[k] for k in ("case", "production", "guarded_experiment", "spans")}
                    for r in r["profiles"]
                ],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

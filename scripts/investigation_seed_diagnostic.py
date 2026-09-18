#!/usr/bin/env python3
"""Print the bounded seed access and gap state for one snapshot incident."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from packages.evals.itbench.dataset import ITBenchLiteDataset
from packages.evals.itbench.source import SnapshotSource
from packages.rca.engine import build_case, diagnose_case
from packages.rca.investigation.environment import InitialObservationView, SeedPolicy, initial_view


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario")
    parser.add_argument("--root", type=Path, default=Path(".local/itbench-lite"))
    parser.add_argument("--latest-only", action="store_true")
    parser.add_argument("--no-events", action="store_true")
    args = parser.parse_args()

    default = SeedPolicy()
    policy = SeedPolicy(
        name=(
            "latest-only-no-events"
            if args.no_events
            else "latest-only-plus-5m-events"
            if args.latest_only
            else default.name
        ),
        history_window=None if args.latest_only or args.no_events else default.history_window,
        event_before=None if args.no_events else default.event_before,
        event_after=None if args.no_events else default.event_after,
    )
    dataset = ITBenchLiteDataset.open(args.root)
    source = SnapshotSource(dataset.scenario(args.scenario))
    view = initial_view(source, policy=policy)
    case = build_case(view)
    diagnosis = diagnose_case(case)
    assert isinstance(view, InitialObservationView)
    access = view.access_ledger()

    print(args.scenario)
    print(f"\nSEED POLICY\n  {policy.name}")
    print(f"\nINITIAL SEED\n  entities visible: {len(view.object_history())}")
    for key, refs in access.items():
        print(f"  {key}: {len(refs)}")
    print("\nFindings")
    for kind, count in sorted(Counter(item.kind.value for item in case.findings).items()):
        print(f"  {kind}: {count}")
    print(f"\nHypotheses: {len(case.hypotheses)}")
    print(f"Resolution: {diagnosis.resolution.value}")
    print("Information gaps")
    for gap in diagnosis.information_gaps:
        print(f"  {gap.dimension.value}: {gap.resolvability.value}")

    full = source.object_history()
    hidden_history = {
        version.evidence_id for versions in full.values() for version in versions
    } - set(access["initial_history_refs"])
    hidden_events = {item.evidence_id for item in source.events()} - set(
        access["initial_event_refs"]
    )
    hidden_logs = {item.evidence_id for item in source.error_logs()} - set(
        access["initial_log_refs"]
    )
    latest = {entity: versions[-1] for entity, versions in full.items() if versions}
    pods = tuple(entity for entity in latest if entity.kind == "Pod")
    hidden_resource = {
        item.evidence_id
        for item in source.resource_pressure(pods, datetime.min.replace(tzinfo=UTC))
    } - set(access["initial_metric_refs"])
    hidden_traffic = {item.evidence_id for item in source.traffic_observations()} - set(
        access["initial_traffic_refs"]
    )
    print("\nFULL SNAPSHOT OUTSIDE SEED")
    print(f"  history refs hidden: {len(hidden_history)}")
    print(f"  event refs hidden: {len(hidden_events)}")
    print(f"  log refs hidden: {len(hidden_logs)}")
    print(f"  resource-pressure refs hidden: {len(hidden_resource)}")
    print(f"  traffic refs hidden: {len(hidden_traffic)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

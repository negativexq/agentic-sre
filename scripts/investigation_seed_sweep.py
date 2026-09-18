#!/usr/bin/env python3
"""Compare generic bounded seed policies by deterministic investigability."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from packages.evals.itbench.dataset import ITBenchLiteDataset
from packages.evals.itbench.source import SnapshotSource
from packages.rca.investigation.environment import SeedPolicy
from packages.rca.investigation.opportunity_audit import (
    IncidentOpportunityAudit,
    audit_incident,
)


@dataclass(frozen=True)
class PolicySummary:
    name: str
    resolved: int
    ambiguous: int
    insufficient: int
    hypotheses: int
    gaps: int
    novel: int
    findings: int
    hypothesis_changes: int
    resolution_changes: int


def _policies() -> tuple[SeedPolicy, ...]:
    return (
        SeedPolicy(
            name="current-10m-history-plus-5m-events",
            history_window=timedelta(minutes=10),
            event_before=timedelta(minutes=5),
            event_after=timedelta(minutes=5),
        ),
        SeedPolicy(
            name="latest-only-plus-5m-events",
            history_window=None,
            event_before=timedelta(minutes=5),
            event_after=timedelta(minutes=5),
        ),
        SeedPolicy(
            name="latest-only-plus-2m-events",
            history_window=None,
            event_before=timedelta(minutes=2),
            event_after=timedelta(minutes=2),
        ),
        SeedPolicy(
            name="latest-only-no-events",
            history_window=None,
            event_before=None,
            event_after=None,
        ),
    )


def _summary(name: str, audits: Sequence[IncidentOpportunityAudit]) -> PolicySummary:
    return PolicySummary(
        name=name,
        resolved=sum(item.initial_resolution == "RESOLVED" for item in audits),
        ambiguous=sum(item.initial_resolution == "AMBIGUOUS" for item in audits),
        insufficient=sum(item.initial_resolution == "INSUFFICIENT_EVIDENCE" for item in audits),
        hypotheses=sum(len(item.initial_hypotheses) for item in audits),
        gaps=sum(len(item.initial_resolvable_gaps) for item in audits),
        novel=sum(any(op.new_raw_refs for op in item.opportunities) for item in audits),
        findings=sum(any(op.new_findings for op in item.opportunities) for item in audits),
        hypothesis_changes=sum(
            any(op.hypotheses_after != op.hypotheses_before for op in item.opportunities)
            for item in audits
        ),
        resolution_changes=sum(
            any(op.effect == "RESOLUTION_CHANGED" for op in item.opportunities) for item in audits
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(".local/itbench-lite"))
    parser.add_argument("--scenario", action="append", dest="scenarios", required=True)
    args = parser.parse_args()
    dataset = ITBenchLiteDataset.open(args.root)

    print(
        "policy | RESOLVED | AMBIGUOUS | INSUFFICIENT | hypotheses | "
        "resolvable gaps | novel telemetry | new Findings | hypothesis changes | resolution changes"
    )
    for policy in _policies():
        audits = [
            audit_incident(SnapshotSource(dataset.scenario(scenario)), seed_policy=policy)
            for scenario in args.scenarios
        ]
        summary = _summary(policy.name, audits)
        print(
            f"{summary.name} | {summary.resolved} | {summary.ambiguous} | "
            f"{summary.insufficient} | {summary.hypotheses} | {summary.gaps} | "
            f"{summary.novel}/{len(audits)} | {summary.findings}/{len(audits)} | "
            f"{summary.hypothesis_changes}/{len(audits)} | "
            f"{summary.resolution_changes}/{len(audits)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

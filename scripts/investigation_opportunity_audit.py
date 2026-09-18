#!/usr/bin/env python3
"""Audit one-step bounded-investigation opportunities without ground truth."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from packages.evals.itbench.dataset import ITBenchLiteDataset
from packages.evals.itbench.source import SnapshotSource
from packages.rca.investigation.opportunity_audit import audit_incident


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(".local/itbench-lite"))
    parser.add_argument("--scenario", action="append", dest="scenarios", required=True)
    parser.add_argument("--json", type=Path, help="optional compact JSON output")
    args = parser.parse_args()

    dataset = ITBenchLiteDataset.open(args.root)
    audits = [audit_incident(SnapshotSource(dataset.scenario(item))) for item in args.scenarios]
    counts = {
        classification: sum(item.classification == classification for item in audits)
        for classification in sorted({item.classification for item in audits})
    }
    novel = sum(any(item.new_raw_refs for item in audit.opportunities) for audit in audits)
    findings = sum(any(item.new_findings for item in audit.opportunities) for audit in audits)
    hypotheses = sum(
        any(item.hypotheses_after != item.hypotheses_before for item in audit.opportunities)
        for audit in audits
    )
    resolutions = sum(
        any(item.effect == "RESOLUTION_CHANGED" for item in audit.opportunities) for audit in audits
    )

    print("Investigation opportunity audit")
    for audit in audits:
        print(f"\n{audit.incident_id}")
        print(
            f"  initial={audit.initial_resolution} hypotheses={len(audit.initial_hypotheses)} "
            f"resolvable_gaps={len(audit.initial_resolvable_gaps)}"
        )
        for item in audit.opportunities:
            print(
                f"  {item.dimension} {item.capability}/{item.target}: "
                f"raw={item.raw_records} new_refs={len(item.new_raw_refs)} "
                f"findings={len(item.new_findings)} effect={item.effect}"
            )
        print(f"  classification={audit.classification}")
    print("\nAggregate classification")
    for classification, count in counts.items():
        print(f"  {classification}: {count}")
    print(
        "Aggregate opportunity ceiling: "
        f"novel telemetry available={novel}/{len(audits)}; "
        f"new Finding possible={findings}/{len(audits)}; "
        f"hypothesis change possible={hypotheses}/{len(audits)}; "
        f"resolution change possible={resolutions}/{len(audits)}"
    )
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(
                {
                    "audits": [audit.as_dict() for audit in audits],
                    "classification": counts,
                    "opportunity_ceiling": {
                        "novel_telemetry_cases": novel,
                        "new_finding_cases": findings,
                        "hypothesis_change_cases": hypotheses,
                        "resolution_change_cases": resolutions,
                    },
                },
                indent=2,
                sort_keys=True,
                default=str,
            )
            + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Create the post-hoc E10.1 bottleneck attribution from frozen artifacts."""

from __future__ import annotations

import json
from pathlib import Path

from packages.evals.itbench.dataset import ITBenchLiteDataset
from packages.evals.itbench.e10_1_attribution import build_e10_1_attribution

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    dataset = ITBenchLiteDataset.open(ROOT / ".local/itbench-lite")
    payload = build_e10_1_attribution(
        dataset,
        ROOT / ".local/itbench-e10-predictions",
        ROOT / "docs/benchmarks/itbench-e10-local-results.json",
    )
    output = ROOT / "docs/benchmarks/itbench-e10-failure-attribution.json"
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "# E10.1 frozen failure attribution",
        "",
        "This is post-hoc analysis of immutable E10 artifacts. It made zero provider calls and did not alter E10 evidence.",
        "",
        f"- scenarios: {payload['scenario_count']}",
        f"- provider calls: {payload['provider_calls']}",
        "",
        "## Primary bottlenecks",
        "",
        "| category | count | fraction |",
        "|---|---:|---:|",
    ]
    for category, values in payload["categories"].items():
        lines.append(f"| {category} | {values['count']} | {values['fraction']:.3f} |")
    lines.extend(
        [
            "",
            "Fields not present in the frozen E10 trace are recorded as `NOT_OBSERVABLE_FROM_FROZEN_ARTIFACT`; no historical state is fabricated.",
        ]
    )
    (ROOT / "docs/benchmarks/itbench-e10-failure-attribution.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    print(json.dumps({"status": "E10_1_ATTRIBUTION_WRITTEN", "provider_calls": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

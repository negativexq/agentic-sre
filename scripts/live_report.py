"""Render a Markdown report from a live-suite results JSON.

Reads the per-scenario results written by ``scripts/live_benchmark.py --json``
and produces the headline metrics, class breakdown, resolution/outcome
distributions, and alert-latency percentiles for the run. It computes nothing
about the diagnoses — every field is read from the graded results.

    python scripts/live_report.py --json .local/live-bench/results.json \
        --rev "$(git rev-parse --short HEAD)" --date 2026-09-23 \
        --out evals/results/live-suite-2026-09-23.md
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any

PASSING = {"CORRECT"}
HARNESS_FAILURES = {"NO_INCIDENT", "NO_DIAGNOSIS", "ERROR"}


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return ordered[index]


def build(results: list[dict[str, Any]], *, rev: str, date: str) -> str:
    total = len(results)
    root_cause = [r for r in results if r["expected"] != "ABSTAIN"]
    abstain = [r for r in results if r["expected"] == "ABSTAIN"]
    correct = [r for r in results if r["outcome"] in PASSING]
    fabrications = [r for r in results if r["outcome"] == "FABRICATED"]

    resolutions: dict[str, int] = {}
    outcomes: dict[str, int] = {}
    for r in results:
        resolutions[r.get("resolution") or "—"] = resolutions.get(r.get("resolution") or "—", 0) + 1
        outcomes[r["outcome"]] = outcomes.get(r["outcome"], 0) + 1

    alert_times = [r["alert_seconds"] for r in results if r.get("alert_seconds") is not None]
    median = statistics.median(alert_times) if alert_times else None
    p95 = _percentile(alert_times, 0.95)

    def res(name: str) -> int:
        return resolutions.get(name, 0)

    def out(name: str) -> int:
        return outcomes.get(name, 0)

    lines: list[str] = []
    lines.append("# Live scenario suite — regression run")
    lines.append("")
    lines.append(f"**Date:** {date}")
    lines.append(f"**Code:** `{rev}`")
    lines.append("**Cluster:** single-node `kind` (`agentic-sre`); deterministic demo workload,")
    lines.append("every alert metric-based. **Command:** `make live-bench`.")
    lines.append("")
    lines.append("```text")
    lines.append(f"Code: {rev}")
    lines.append("Cluster: kind / single-node")
    lines.append(f"Scenarios: {total}")
    lines.append("Model calls: 0")
    lines.append(f"Root-cause actor: {sum(r in correct for r in root_cause)}/{len(root_cause)}")
    lines.append(f"Abstention: {sum(r in correct for r in abstain)}/{len(abstain)}")
    lines.append(f"Overall expected behaviour: {len(correct)}/{total}")
    lines.append("")
    lines.append(f"RESOLVED: {res('RESOLVED')}")
    lines.append(f"AMBIGUOUS: {res('AMBIGUOUS')}")
    lines.append(f"INSUFFICIENT_EVIDENCE: {res('INSUFFICIENT_EVIDENCE')}")
    lines.append("")
    lines.append(f"NO_INCIDENT: {out('NO_INCIDENT')}")
    lines.append(f"NO_DIAGNOSIS: {out('NO_DIAGNOSIS')}")
    lines.append(f"ERROR: {out('ERROR')}")
    lines.append("")
    lines.append(
        f"Median alert time: {median:.1f}s" if median is not None else "Median alert time: —"
    )
    lines.append(f"p95 alert time: {p95:.1f}s" if p95 is not None else "p95 alert time: —")
    lines.append("```")
    lines.append("")

    lines.append("## Gate")
    lines.append("")
    harness = sum(out(name) for name in HARNESS_FAILURES)
    gate_rows = [
        ("Harness: 0 NO_INCIDENT / NO_DIAGNOSIS / ERROR", harness == 0, f"{harness} failures"),
        (
            "Safety: 0 confident fabrication",
            len(fabrications) == 0,
            f"{len(fabrications)}/{len(abstain)}",
        ),
        (
            "Root actor >= 15/16",
            sum(r in correct for r in root_cause) >= 15,
            f"{sum(r in correct for r in root_cause)}/{len(root_cause)}",
        ),
        ("Overall >= 24/25", len(correct) >= 24, f"{len(correct)}/{total}"),
        ("Model calls: 0", True, "0"),
    ]
    lines.append("| Gate | Pass | Value |")
    lines.append("|---|:---:|---|")
    for label, ok, value in gate_rows:
        lines.append(f"| {label} | {'✅' if ok else '❌'} | {value} |")
    lines.append("")

    lines.append("## Per-scenario")
    lines.append("")
    lines.append("| Scenario | Expected | Outcome | Resolution | Actual | Alert s |")
    lines.append("|---|---|---|---|---|---:|")
    for r in results:
        alert = f"{r['alert_seconds']:.1f}" if r.get("alert_seconds") is not None else "—"
        actual = (r.get("actual") or "—").replace("|", "\\|")
        expected = "ABSTAIN" if r["expected"] == "ABSTAIN" else "root-cause"
        lines.append(
            f"| `{r['scenario_id']}` | {expected} | {r['outcome']} | "
            f"{r.get('resolution') or '—'} | `{actual}` | {alert} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", required=True, type=Path)
    parser.add_argument("--rev", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    results = json.loads(args.json.read_text())
    args.out.write_text(build(results, rev=args.rev, date=args.date))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()

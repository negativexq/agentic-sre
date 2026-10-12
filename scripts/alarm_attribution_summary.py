"""Summary of ``scripts.alarm_attribution_audit`` output: alert classes, and the blockers of every diagnosis with
one strong supported claim before and after Service coverage from an explained alert (counterfactual, not a rule).

    python -m scripts.alarm_attribution_summary DIR
"""

from __future__ import annotations

import glob
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


def truth() -> dict[str, set[str]]:
    """The cause actors of each testbed run: used only to label the result, never in a decision."""
    out = {}
    for path in glob.glob(".local/testbed/*/*/*/run.json"):
        suite, scenario, repeat = path.split("/")[-4:-1]
        database = "testbed_" + f"{suite}-{scenario}-{repeat}".replace("-", "_")
        links = json.loads(Path(path).read_text())["chain"]["links"]
        out[database] = {link["actor"] for link in links if link["role"] == "cause"}
    return out


def blockers(row: dict[str, Any], uncovered: set[str] | None) -> str:
    return (
        ("K" if uncovered else "")
        + ("R" if row["unresolved"] else "")
        + ("F" if row["limiting"] else "")
    ) or "none"


def main() -> None:
    rows = [json.loads(line) for line in (Path(sys.argv[1]) / "rows.jsonl").open()]
    causes = truth()
    summary: dict[str, Any] = {"runs": len(rows), "errors": sum("error" in r for r in rows)}
    alerts = [(r, a) for r in rows for a in r.get("alerts", ())]
    summary["alert_classes"] = Counter(a["class"] for _, a in alerts)
    summary["alert_classes_by_name"] = {
        f"{name} {cls}": n
        for (name, cls), n in Counter((a["alert"], a["class"]) for _, a in alerts).items()
    }
    summary["unknown_reasons"] = Counter(
        u.split(":")[0] for _, a in alerts for u in a.get("unknown", ())
    )
    summary["start_is"] = Counter(a.get("start_is") for _, a in alerts if "start_is" in a)
    summary["pending_longer_than_for"] = sum(
        bool(a.get("pending_longer_than_for")) for _, a in alerts
    )
    before: Counter[str] = Counter()
    after: Counter[str] = Counter()
    gains = []
    for row in rows:
        if "error" in row or not (len(row["supported"]) == 1 and row["strong"] == row["supported"]):
            continue
        claim = row["claims"][0]
        uncovered = set(claim["uncovered"])
        failed = {a["service"] for a in row["alerts"] if a["class"] != "EXPLAINS"}
        explained = {a["service"] for a in row["alerts"] if a["class"] == "EXPLAINS"} - failed
        left = uncovered - explained
        before[blockers(row, uncovered)] += 1
        after[blockers(row, left)] += 1
        if uncovered and not left and not row["unresolved"] and not row["limiting"]:
            gains.append(
                {
                    "database": row["database"],
                    "run": row["run"],
                    "root": row["root"],
                    "root_is_cause": row["root"] in causes.get(row["database"], set()),
                    "uncovered": sorted(uncovered),
                }
            )
    summary["single_strong_blockers_before"] = before
    summary["single_strong_blockers_after"] = after
    summary["uncovered_kinds"] = Counter(
        ",".join(sorted({u.split("/")[1] for u in r["claims"][0]["uncovered"]}))
        for r in rows
        if "error" not in r
        and len(r["supported"]) == 1
        and r["strong"] == r["supported"]
        and r["claims"][0]["uncovered"]
    )
    summary["would_resolve"] = gains
    print(json.dumps(summary, indent=1, default=str))


if __name__ == "__main__":
    main()

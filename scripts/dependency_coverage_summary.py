"""Summarize the paired C audit without feeding ground truth to the engine."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    rows = [json.loads(line) for line in (args.directory / "rows.jsonl").read_text().splitlines()]
    truth = {}
    for path in Path(".local/testbed").glob("*/*/*/run.json"):
        suite, scenario, repeat = path.parts[-4:-1]
        database = "testbed_" + f"{suite}-{scenario}-{repeat}".replace("-", "_").replace(".", "_")
        record = json.loads(path.read_text())
        truth[database] = {
            link["actor"]
            for link in record.get("chain", {}).get("links", [])
            if link["role"] in {"cause", "execution"}
        }
    groups = {}
    for name, population in (
        ("all", rows),
        (
            "recent",
            [
                r
                for r in rows
                if re.match(r"testbed_(holdout|dev)(19|20|21|22|23)(_|[a-z])", r["database"])
            ],
        ),
    ):
        counts: Counter[str] = Counter()
        dependencies: Counter[str] = Counter()
        reads: Counter[str] = Counter()
        transitions: Counter[str] = Counter()
        changed = []
        for row in population:
            counts["rows"] += 1
            if "error" in row:
                counts["errors"] += 1
                continue
            before, after = row["baseline"], row["variant"]
            transitions[f"{before['resolution']} -> {after['resolution']}"] += 1
            for key in row["changed"]:
                counts[f"changed_{key}"] += 1
            c = [a for a in before["answers"] if a["state"] == "ANSWERED_DECLARED_UNUSED"]
            if c:
                counts["diagnoses_with_c_closure"] += 1
                counts["c_closure_answers"] += len(c)
                for read in row["trace_reads"]:
                    reads[read["completeness"]] += 1
                alternatives = {a["alternative_id"]: a for a in before["answers"]}
                # Alternative IDs are retained; caller/read metadata is in rows.jsonl.
                for answer in c:
                    dependencies[alternatives[answer["alternative_id"]]["rule_id"]] += 1
            if "resolution" in row["changed"]:
                roots = truth.get(row["database"])
                counts["resolution_changes_truth_missing"] += not bool(roots)
                counts["resolution_changes_root_on_truth_chain"] += bool(
                    roots and before["root"] in roots
                )
                changed.append(
                    {
                        "database": row["database"],
                        "run": row["run"],
                        "alerts": row["alerts"],
                        "root": before["root"],
                        "truth_available": bool(roots),
                        "root_on_cause_or_execution_chain": before["root"] in roots
                        if roots
                        else None,
                        "before": before["resolution"],
                        "after": after["resolution"],
                        "reopened_frontier": sorted(
                            set(after["frontier"]) - set(before["frontier"])
                        ),
                    }
                )
        groups[name] = {
            "counts": dict(counts),
            "transitions": dict(transitions),
            "c_answer_rules": dict(dependencies),
            "read_metadata_in_affected_diagnoses": dict(reads),
            "resolution_changes": changed,
        }
    output = args.directory / "analysis.json"
    output.write_text(json.dumps(groups, indent=2))
    print(
        json.dumps(
            {
                k: {"counts": v["counts"], "transitions": v["transitions"]}
                for k, v in groups.items()
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

"""Summarize saved predictions; labels score actor identity, never mechanism truth."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from packages.evals.itbench.benchmark import _matches
from packages.evals.itbench.contracts import ITBenchGroundTruth


def diagnostic(d: dict[str, Any], truth: ITBenchGroundTruth) -> dict[str, Any]:
    trace = d["resolution_trace"]
    claims = [*([d["hypothesis"]] if d.get("hypothesis") else []), *d["alternative_hypotheses"]]
    matching = {
        h["hypothesis_id"]
        for h in claims
        if _matches("/".join(h["causal_actor"][k] for k in ("namespace", "kind", "name")), truth)
    }
    supported = set(trace["plausible_hypotheses"])
    strong = set(trace.get("mechanism_verified_hypotheses", []))
    answers = trace.get("frontier_answers", [])
    return {
        "status": trace["diagnosis_status"],
        "resolution": d["resolution"],
        "represented": bool(matching),
        "admitted": bool(matching.intersection(trace["admitted_hypotheses"])),
        "matching_possible_claims": len(matching & supported),
        "nonmatching_possible_claims": len(supported - matching),
        "matching_strong_claims": len(matching & strong),
        "nonmatching_strong_claims": len(strong - matching),
        "explained_claims": len(trace.get("explained_hypotheses", [])),
        "explanation_relations": len(trace.get("explanations", [])),
        "unresolved_claims": len(trace["unresolved_hypotheses"]),
        "independent_observed_causes": len(trace.get("independent_mechanism_causes", [])),
        "frontier_answered": sum(a["state"] == "ANSWERED_ROLE_TRANSFERRED" for a in answers),
        "frontier_partially_answered": sum(
            a["state"] == "OPEN" and bool(a["evidence_ids"]) for a in answers
        ),
        "answered_claim_roles": sum(
            len(a["affected_claims"]) for a in answers if a["evidence_ids"]
        ),
        "frontier_open": sum(a["state"] == "OPEN" for a in answers)
        if answers
        else len(trace.get("frontier_bindings", [])),
        "frontier_progress": dict(Counter(a["investigation_state"] for a in answers)),
        "recovery": d.get("incident_recovery", "NOT_ASSESSED"),
    }


def active_metrics(result: dict[str, Any]) -> dict[str, Any]:
    audits = result["action_audits"]
    pairs = ("resolution", "leading_actor", "hypothesis_states", "gap_states")
    physical = [
        json.dumps({k: a["action"][k] for k in ("capability", "target", "query")}, sort_keys=True)
        for a in audits
        if a["action"]["action"] == "inspect"
    ]
    return {
        "queries": result["tool_calls"],
        "decision_changing_queries_reported": sum(
            bool(a["decision_state_changed"]) for a in audits
        ),
        "decision_changing_queries_common_projection": sum(
            any(a[k + "_before"] != a[k + "_after"] for k in pairs) for a in audits
        ),
        "duplicate_physical_reads": len(physical) - len(set(physical)),
        "no_data": result["no_data_observations"],
        "stop": result["stop_reason"],
        "frontier_questions_in_queries": sum(bool(a.get("discriminator")) for a in audits),
    }


def aggregate(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    values = [r[key] for r in rows]
    numeric = [k for k, v in values[0].items() if isinstance(v, (int, bool))]
    return {
        **{k: sum(v[k] for v in values) for k in numeric},
        "statuses": dict(Counter(v["status"] for v in values)),
        "resolutions": dict(Counter(v["resolution"] for v in values)),
        "matching_possible_cases": sum(v["matching_possible_claims"] > 0 for v in values),
        "nonmatching_possible_cases": sum(v["nonmatching_possible_claims"] > 0 for v in values),
        "frontier_progress": dict(
            sum((Counter(v["frontier_progress"]) for v in values), Counter())
        ),
        "recovery": dict(Counter(v["recovery"] for v in values)),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    baseline = json.loads(args.baseline.with_suffix(".json").read_text())
    after = {r["scenario"]: r for r in json.loads((args.after / "summary.json").read_text())}
    assert len(baseline) == len(after) == 35, "require complete seen35 runs"
    rows = []
    for row in sorted(baseline, key=lambda r: r["scenario"]):
        sid = row["scenario"]
        truth = ITBenchGroundTruth.model_validate_json(json.dumps(row["labels"]))
        before = json.loads((args.baseline / f"{sid}.json").read_text())
        current = json.loads((args.after / f"{sid}-snapshot.json").read_text())
        early = json.loads((args.baseline / f"{sid}-active.json").read_text())
        late = json.loads((args.after / f"{sid}-active.json").read_text())
        rows.append(
            {
                "scenario": sid,
                "scoreable": row["scoreable"],
                "matching_observed_entities": row["matching_observed_entities"],
                "root_labels": [g for g in row["labels"]["root_cause_groups"] if g["root_cause"]],
                "aliases": row["labels"]["aliases"],
                "baseline_snapshot": diagnostic(before["diagnosis"], truth),
                "after_snapshot": diagnostic(current["diagnosis"], truth),
                "baseline_active": diagnostic(early["diagnosis"], truth),
                "after_active": diagnostic(late["diagnosis"], truth),
                "baseline_queries": active_metrics(early),
                "after_queries": active_metrics(late),
                "final_replay_equal": after[sid]["source_replay_parity"],
                "all_transitions_replay_equal": after[sid]["transition_replay_parity"],
            }
        )
    totals = {
        key: aggregate(rows, key)
        for key in ("baseline_snapshot", "after_snapshot", "baseline_active", "after_active")
    }
    for phase in ("baseline_queries", "after_queries"):
        totals[phase] = {k: sum(r[phase][k] for r in rows) for k in rows[0][phase] if k != "stop"}
        totals[phase]["stop"] = dict(Counter(r[phase]["stop"] for r in rows))
    document = {
        "format": "m21.causal-closure-measurement.v1",
        "baseline_commit": "d9b7cf702dd7385dcd4e57700d391b292a30b008",
        "after_engine": "2.1.0",
        "set": "seen35 development/regression; no held-out generalization claim",
        "label_scope": "Actor identity only; nonmatching support is not proven false mechanism",
        "baseline_source_replay": "NOT_RECORDED",
        "scoreable": sum(r["scoreable"] for r in rows),
        "unmatchable": [r["scenario"] for r in rows if not r["scoreable"]],
        "final_replay_cases": sum(r["final_replay_equal"] for r in rows),
        "transition_replay_cases": sum(r["all_transitions_replay_equal"] for r in rows),
        "totals": totals,
        "cases": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, indent=2) + "\n")


if __name__ == "__main__":
    main()

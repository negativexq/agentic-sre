"""Explain remaining-claim questions from saved predictions, without labels.

These are investigation questions, not production elimination rules. In
particular, an initiating observation does not prove independence and a bridge
is not execution proof. Handles both v2 baseline and v3 snapshot artifacts.
"""

from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path
from typing import Any

QUESTIONS = {
    "ROLLOUT_RESTART": (
        "What triggered this exact workload revision/UID restart, and did the change precede onset?",
        "controller operation attribution and bounded object-history interval; consumption of the changed config revision must be positively observed",
    ),
    "CONFIG_CHANGE": (
        "Was this exact config revision consumed by the failing workload operation?",
        "runtime/config-consumption record naming revision or resource version and the affected operation; config reference plus failure alone is insufficient",
    ),
    "FAULT_INJECTION": (
        "Which exact target instance had this fault applied during the failure?",
        "Applied/controller execution record with target UID and execution interval, plus a matching observed failure mechanism; an object or selector alone is insufficient",
    ),
    "FAILURE_EVENT": (
        "Is this specific failure a returned or rejected effect, or a separate local failure?",
        "paired runtime return with exact bindings or an explicit admission rejection naming the actor; other local errors must remain separately assessed",
    ),
    "CONTAINER_FAILURE": (
        "What caused this container instance to terminate in this episode?",
        "UID/container-bound termination reason, resource measurements or controller event covering the actual failure interval",
    ),
    "RESOURCE_PRESSURE": (
        "Did observed demand or an enforced constraint cause this pressure episode?",
        "resource-specific demand/limit measurements at the exact instance and interval, including observed rejection/throttling/termination where applicable",
    ),
    "DEPENDENCY_ERRORS": (
        "Which observed remote failure produced this local operation error?",
        "paired error return or directly attributed dependency log with exact actor binding and time; dependency name alone is insufficient",
    ),
    "SPEC_CHANGE": (
        "Which changed field was executed by the affected instance during the symptom?",
        "revision-bound execution/control-plane outcome for the changed mechanism, not only a desired-spec diff",
    ),
    "IMAGE_CHANGE": (
        "Did this exact image/revision produce the observed failing operation?",
        "runtime revision/image identity, rollout interval and execution/failure record tied to the changed code path",
    ),
}


def inventory(document: dict[str, Any]) -> list[dict[str, Any]]:
    diagnosis = document["diagnosis"]
    trace = diagnosis["resolution_trace"]
    claims = document.get("claims") or [
        *([diagnosis["hypothesis"]] if diagnosis.get("hypothesis") else []),
        *diagnosis.get("alternative_hypotheses", []),
    ]
    by_id = {h["hypothesis_id"]: h for h in claims}
    audits = {a["hypothesis_id"]: a for a in trace["hypothesis_audits"]}
    result = []
    for hid in sorted(set(trace["unresolved_hypotheses"]) | set(trace["plausible_hypotheses"])):
        h = by_id[hid]
        local = [
            f
            for f in h["findings"]
            if f["entity"] == h["causal_actor"]
            and f.get("entity_instance") == h.get("actor_instance")
            and f.get("incident_onset") == h.get("episode_onset")
        ]
        kinds = sorted({f["kind"] for f in local})
        gaps = [g for g in diagnosis["information_gaps"] if hid in g["hypothesis_ids"]]
        result.append(
            {
                "claim_id": hid,
                "actor": h["causal_actor"],
                "instance": h.get("actor_instance"),
                "episode": h.get("episode_onset"),
                "mechanism": h.get("mechanism"),
                "admission": audits[hid]["admission"],
                "admission_reasons": audits[hid]["admission_reasons"],
                "symptoms": h["symptom_entities"],
                "incident_paths": h["causal_paths"],
                "support": audits[hid]["root_support"],
                "competes_with": [other for other in trace["plausible_hypotheses"] if other != hid],
                "independence": "UNDETERMINED_UNLESS_POSITIVE_EXECUTION_PROOF",
                "possible_effect": not any(f["temporal_role"] == "INITIATING" for f in local),
                "same_actor_episode_representations": [
                    {
                        "claim_id": o["hypothesis_id"],
                        "instance": o.get("actor_instance"),
                        "mechanism": o.get("mechanism"),
                    }
                    for o in by_id.values()
                    if o["hypothesis_id"] != hid
                    and o["causal_actor"] == h["causal_actor"]
                    and o.get("episode_onset") == h.get("episode_onset")
                ],
                "questions": [
                    {
                        "finding_kind": k,
                        "question": QUESTIONS.get(
                            k,
                            (
                                "What executed this observed mechanism?",
                                "positive actor-instance execution attribution",
                            ),
                        )[0],
                        "required_evidence": QUESTIONS.get(
                            k, ("", "positive actor-instance execution attribution")
                        )[1],
                    }
                    for k in kinds
                ],
                "recorded_local_evidence": sorted({e for f in local for e in f["evidence_ids"]}),
                "authorized_reads": gaps,
                "access": "BOUNDED_READS_AUTHORIZED_NOT_GUARANTEED_TO_CONTAIN_NEEDED_FACT"
                if gaps
                else "NO_CURRENT_AUTHORIZED_READ_FOR_THIS_CLAIM",
                "existing_explanations": [
                    r for r in trace.get("explanations", []) if r["explained_claim"] == hid
                ],
                "missing_execution_fact_available": "NOT_ESTABLISHED_BY_CURRENT_RECORD",
            }
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = []
    for path in sorted(args.directory.glob("Scenario-*.json")):
        if "-active" in path.name or "-reads" in path.name:
            continue
        document = json.loads(path.read_text())
        if "diagnosis" in document:
            rows.append(
                {"scenario": path.stem.removesuffix("-snapshot"), "rivals": inventory(document)}
            )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(rows, indent=2) + "\n").encode()
    args.output.write_bytes(
        gzip.compress(encoded, mtime=0) if args.output.suffix == ".gz" else encoded
    )


if __name__ == "__main__":
    main()

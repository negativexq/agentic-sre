"""Re-run the already-seen 35 snapshot cases; this is NOT a blind benchmark.

Ground truth is opened only after prediction, solely for evaluation. This does
not execute the active investigation loop or measure provider cost.
Run with PYTHONPATH=. .venv/bin/python scripts/causal_claim_regression.py --output PATH
"""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from packages.evals.itbench.benchmark import _matches, load_split
from packages.evals.itbench.dataset import ITBenchLiteDataset
from packages.evals.itbench.source import SnapshotSource
from packages.rca.engine import RCA_ENGINE_VERSION, build_case, diagnose_case
from packages.rca.epistemic_digest import diagnosis_epistemic_digest


def evaluate(job: tuple[str, str, str]) -> dict[str, Any]:
    root, scenario, split = job
    dataset = ITBenchLiteDataset.open(Path(root))
    case = build_case(SnapshotSource(dataset.scenario(scenario)))
    diagnosis = diagnose_case(case)
    trace = diagnosis.resolution_trace
    assert trace is not None
    digest = diagnosis_epistemic_digest(diagnosis)
    repeated_digest = diagnosis_epistemic_digest(diagnose_case(case))
    # Evaluation boundary: truth never enters build_case/diagnose_case.
    truth = dataset.load_ground_truth(scenario)
    roots = {h.hypothesis_id for h in case.hypotheses if _matches(h.causal_actor.canonical, truth)}
    supported = set(trace.plausible_hypotheses)
    admitted = set(trace.admitted_hypotheses)
    return {
        "scenario": scenario,
        "split": split,
        "engine": RCA_ENGINE_VERSION,
        "claims": len(case.hypotheses),
        "root_preserved": bool(roots),
        "root_admitted": bool(roots & admitted),
        "root_claims": sorted(roots),
        "root_context_reasons": [
            a.admission_reasons
            for a in trace.hypothesis_audits
            if a.hypothesis_id in roots and a.hypothesis_id not in admitted
        ],
        "false_support": len(supported - roots),
        "correct_support": len(supported & roots),
        "resolution": diagnosis.resolution.value,
        "diagnosis": trace.diagnosis_status,
        "wrong_resolved": diagnosis.resolution.value == "RESOLVED"
        and not bool(roots & set(trace.leading_hypothesis_ids)),
        "unresolved": len(trace.unresolved_hypotheses),
        "context": len(trace.context_hypotheses),
        "frontier": len(trace.material_frontier_ids),
        "real_rival_ambiguity": bool(supported and trace.unresolved_hypotheses),
        # Requires a counterfactual run with context removed; a single trace
        # cannot measure this. Covered separately by the invariance test.
        "context_blockage": None,
        "digest": digest,
        "repeated_decision_digest_equal": digest == repeated_digest,
        "active_investigation_reads": None,
        "provider_cost": None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default=".local/itbench-lite")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    jobs = [(args.dataset, sid, split) for split in ("dev", "test") for sid in load_split(split)]
    rows = []
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(evaluate, job) for job in jobs]
        for future in as_completed(futures):
            row = future.result()
            rows.append(row)
            rows.sort(key=lambda item: item["scenario"])
            args.output.write_text(json.dumps(rows, indent=2) + "\n")
            print(
                row["scenario"],
                row["diagnosis"],
                row["correct_support"],
                row["false_support"],
                flush=True,
            )


if __name__ == "__main__":
    main()

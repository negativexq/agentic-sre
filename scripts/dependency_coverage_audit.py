"""Read-only C-rule counterexample and census of exported stored diagnoses.

Run from the repository: python -m scripts.dependency_coverage_audit.
The counterexample reuses unit fixtures, not customer or HOLDOUT observations.
The census reads exports as recorded; it does not recalculate or grade them.
"""

from __future__ import annotations

import importlib
import json
import sys
from collections import Counter
from pathlib import Path

from packages.rca.engine import RCA_ENGINE_VERSION
from packages.rca.epistemic_digest import diagnosis_epistemic_digest
from packages.rca.model import CausalHop, Confidence, Diagnosis, Symptoms
from packages.rca.resolution import resolve_hypotheses


def counterexample() -> dict[str, object]:
    # Reuse the existing positive execution fixture and dependency read fixture.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests/unit/rca"))
    dependency = importlib.import_module("test_dependency_declared_unused")
    closure = importlib.import_module("test_causal_closure")
    AT, fixture = closure.AT, closure.fixture

    source, target, event = fixture()
    caller = dependency.ORDER
    event = event.model_copy(update={"entity": caller})
    origin = source.findings[0].model_copy(
        update={"related": (caller,), "details": {"rejected": [caller.canonical]}}
    )
    source = source.model_copy(
        update={
            "findings": (origin,),
            "initiating_findings": (origin,),
            "symptom_entities": (caller,),
            "linked_symptoms": (caller.canonical,),
            "causal_paths": (
                (CausalHop(source=source.causal_actor, relation="quota_blocks", target=caller),),
            ),
        }
    )
    assert target.actor_instance is not None
    instance = target.actor_instance.model_copy(update={"entity": caller})
    target = target.model_copy(
        update={
            "causal_actor": caller,
            "actor_instance": instance,
            "findings": (
                target.findings[0].model_copy(
                    update={"entity": caller, "entity_instance": instance}
                ),
            ),
            "symptom_entities": (caller,),
        }
    )
    shift = AT - dependency.ONSET
    observed = [*dependency.traffic(), dependency.span(58, kind="CLIENT", address="cache")]
    observed = [
        span.model_copy(update={"start_at": span.start_at + shift, "end_at": span.end_at + shift})
        for span in observed
    ]
    read = dependency.READS[0].model_copy(
        update={
            "starts_at": dependency.READS[0].starts_at + shift,
            "ends_at": dependency.READS[0].ends_at + shift,
        }
    )
    rows = []
    for completeness in ("BEST_EFFORT", "TRUNCATED", "FAILED"):
        for label, spans in (("call_observed", observed), ("call_omitted", observed[:-1])):
            trace = resolve_hypotheses(
                (source, target),
                events=(event,),
                structural_alternatives=(dependency.question(dependency.CACHE),),
                trace_spans=spans,
                latest=dependency.LATEST,
                trace_reads=(read.model_copy(update={"completeness": completeness}),),
                causal_onset=AT,
            )
            diagnosis = Diagnosis(
                incident_id="synthetic-coverage-audit",
                decision_semantics="m21.v3",
                root_cause=source.causal_actor,
                confidence=Confidence.VERIFIED,
                resolution=trace.state,
                summary="Synthetic decision-boundary audit; not a recorded incident",
                symptoms=Symptoms(
                    onset=AT,
                    last_seen=AT,
                    services=("order",),
                    namespaces=("shop",),
                    alert_names=("synthetic",),
                ),
                hypothesis=source,
                alternative_hypotheses=(target,),
                resolution_trace=trace,
            )
            rows.append(
                {
                    "input": label,
                    "completeness": completeness,
                    "resolution": trace.state.value,
                    "material_frontier_ids": trace.material_frontier_ids,
                    "answers": [a.model_dump(mode="json") for a in trace.frontier_answers],
                    "digest": diagnosis_epistemic_digest(diagnosis),
                    "strong_claims": trace.mechanism_verified_hypotheses,
                }
            )
    return {"kind": "synthetic", "engine": RCA_ENGINE_VERSION, "results": rows}


def stored_census() -> dict[str, object]:
    counts: Counter[str] = Counter()
    affected = []
    for path in sorted(Path(".local/testbed").rglob("diagnoses.json")):
        for row in json.loads(path.read_text()):
            document = row["document"]
            counts["diagnoses"] += 1
            answers = (document.get("resolution_trace") or {}).get("frontier_answers", [])
            unused = [a for a in answers if a.get("state") == "ANSWERED_DECLARED_UNUSED"]
            if unused:
                resolution = document.get("resolution", "UNKNOWN")
                counts["diagnoses_with_c_answer"] += 1
                counts[f"c_answer_resolution_{resolution}"] += 1
                affected.append(
                    {
                        "export": str(path),
                        "alert": row.get("alert"),
                        "resolution": resolution,
                        "answers": unused,
                    }
                )
    return {
        "kind": "stored_exports_not_recalculated_not_graded",
        "counts": dict(counts),
        "affected": affected,
    }


if __name__ == "__main__":
    print(
        json.dumps({"counterexample": counterexample(), "stored_census": stored_census()}, indent=2)
    )

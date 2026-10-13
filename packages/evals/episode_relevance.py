"""Explain historical dependency manifestations without changing RCA authority.

An assigned incident_onset is context, not an observed continuation. Temporal
proximity and sampled successful calls establish neither cause nor recovery.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from typing import Any


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def analyze_episode_relevance(
    document: Mapping[str, Any],
    *,
    observations: Mapping[str, Mapping[str, Any]] | None = None,
    **metadata: Any,
) -> dict[str, Any]:
    """Assess dependency-error timing only; never filter or re-admit a claim.

    Optional observations are frozen source records indexed by evidence ID, with
    an `at` event timestamp. Aggregate finding timestamps are reported separately
    and never substituted for timestamps of their cited source records.
    """
    source = deepcopy(dict(document))
    records = deepcopy(dict(observations or {}))
    trace = source.get("resolution_trace") or {}
    unresolved = trace.get("unresolved_hypotheses") if isinstance(trace, Mapping) else None
    claims = [
        source.get("hypothesis"),
        *(source.get("alternative_hypotheses") or []),
        *(source.get("ambiguous_hypotheses") or []),
    ]
    assessments = []
    seen: set[str] = set()
    for claim in claims:
        if not isinstance(claim, Mapping):
            continue
        hid = claim.get("hypothesis_id")
        if not isinstance(hid, str) or hid in seen:
            continue
        seen.add(hid)
        # Old initiating config/deployment changes are deliberately out of scope.
        if claim.get("mechanism") != "MANIFESTATION_ONLY":
            continue
        onset = _time(claim.get("episode_onset"))
        for index, finding in enumerate(claim.get("findings") or []):
            if not isinstance(finding, Mapping) or finding.get("kind") != "DEPENDENCY_ERRORS":
                continue
            at = _time(finding.get("at"))
            ids = finding.get("evidence_ids") or []
            ids = [eid for eid in ids if isinstance(eid, str)]
            timing = (
                "UNASSESSED"
                if at is None or onset is None
                else "BEFORE_EPISODE_ONSET"
                if at < onset
                else "AT_OR_AFTER_EPISODE_ONSET"
            )
            source_times = []
            for eid in ids:
                observation = records.get(eid)
                event_at = (
                    _time(observation.get("at")) if isinstance(observation, Mapping) else None
                )
                source_times.append(
                    {
                        "evidence_id": eid,
                        "event_at": observation.get("at")
                        if isinstance(observation, Mapping)
                        else None,
                        "timing": "UNASSESSED"
                        if event_at is None or onset is None
                        else "BEFORE_EPISODE_ONSET"
                        if event_at < onset
                        else "AT_OR_AFTER_EPISODE_ONSET",
                    }
                )
            deficits = [
                "REQUEST_OR_INSTANCE_BOUND_CONTINUATION_NOT_ASSESSED",
                "ONGOING_OR_INTERMITTENT_MECHANISM_NOT_ASSESSED",
            ]
            if onset is None or at is None:
                deficits.append("TIME_MISSING_INVALID_OR_UNZONED")
            if not ids or any(row["timing"] == "UNASSESSED" for row in source_times):
                deficits.append("CITED_SOURCE_EVENT_TIMES_NOT_FULLY_RECORDED")
            assessments.append(
                {
                    "claim_id": hid,
                    "finding_index": index,
                    "actor": claim.get("causal_actor"),
                    "recorded_unresolved_rival": hid in unresolved
                    if isinstance(unresolved, list)
                    else None,
                    "episode_onset": claim.get("episode_onset"),
                    "finding_at": finding.get("at"),
                    "aggregate_finding_timing": timing,
                    "assigned_incident_onset": finding.get("incident_onset"),
                    "assigned_onset_is_continuation_proof": False,
                    "source_observations": source_times,
                    "evidence_ids": ids,
                    "relation_evidence_ids": claim.get("relation_evidence_ids") or [],
                    "structural_path_recorded": bool(claim.get("causal_paths")),
                    "current_episode_binding": "UNASSESSED",
                    "continuation": "UNASSESSED",
                    "recovery": "UNASSESSED",
                    "evidence_deficits": deficits,
                    "explanation": "Historical aggregate failure observation; continuation into the current episode requires separate evidence."
                    if timing == "BEFORE_EPISODE_ONSET"
                    else "Timing alone does not establish a current causal role or mechanism continuity.",
                }
            )
    return {
        "report_version": "episode-relevance.v1",
        "authority": "EXPLANATORY_ONLY",
        "incident_id": source.get("incident_id"),
        "recorded_resolution": source.get("resolution"),
        "metadata": deepcopy(metadata),
        "scope": "MANIFESTATION_ONLY/DEPENDENCY_ERRORS",
        "assessments": assessments,
        "summary": {
            "assessed_finding_count": len(assessments),
            "aggregate_timing_counts": dict(
                Counter(a["aggregate_finding_timing"] for a in assessments)
            ),
        },
        "non_consequences": [
            "NO_CLAIM_REMOVAL_OR_ADMISSION_CHANGE",
            "NO_FRONTIER_ANSWER_OR_RESOLUTION_CHANGE",
            "NO_RECOVERY_OR_GLOBAL_HEALTH_ASSERTION",
            "NO_EXCLUSION_OF_OLD_INITIATING_OR_PERSISTENT_FAULTS",
        ],
    }

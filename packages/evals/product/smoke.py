"""Live Kind smokes of the product-resolution harness (M19-6.12).

Three smokes accept the live harness before F7; none is a DEV scenario
(``tier`` is ``None``) and none evaluates a proof:

* ``smoke-noop``: nothing staged. Expected terminal: ``NoR1`` with no incident.
* ``smoke-readiness``: ``SetReadiness(order-service, 40s)`` at T0 with traffic
  on. Expected terminal: ``NoR1`` with no incident; the action verified.
* ``smoke-chain``: a real incident whose R1 opens a product-created requirement
  and a scheduler ``EVIDENCE_DEADLINE`` R2. Pre-history ``SetReadiness(order, 40s)``
  at T−10m (the PR-01 ``H_B`` shape, an A1 continuity obligation) and PR-03's
  qualified true root at T0 (protocol §4, ``service_selector_drift``). Expected
  terminal: ``RUN_OK`` with a v2 artifact (proofs NOT_EVALUATED).

``capture_evidence`` records, before teardown and with reads only, what the
acceptance and G19.6 need; ``accept`` judges a smoke from its result and that
evidence. An unexpected outcome is a finding to report, never something to tune
away.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from packages.evals.product.actions import PatchService, SetReadiness
from packages.evals.product.runner import RunResult, RunStatus
from packages.evals.product.spec import Expectation, Phase, ProductScenario
from packages.storage.models import (
    DiagnosisRow,
    EvidenceRequirementRow,
    IncidentRow,
    LifecycleObservationRow,
)

READINESS = SetReadiness("order-service", True, timedelta(seconds=40))
# PR-03's qualified true root (protocol §4): the payment Service selects no Pod.
CHAIN_ROOT = PatchService("payment-service", {"app": "payment-service-retired"})

SMOKES: dict[str, ProductScenario] = {
    item.scenario_id: item
    for item in (
        ProductScenario("smoke-noop", (), Expectation(), description="nothing staged"),
        ProductScenario(
            "smoke-readiness",
            (Phase(timedelta(0), (READINESS,)),),
            Expectation(),
            description="readiness loss at T0 with traffic on",
        ),
        ProductScenario(
            "smoke-chain",
            (
                Phase(timedelta(minutes=-10), (READINESS,)),
                Phase(timedelta(0), (CHAIN_ROOT,)),
            ),
            Expectation(),
            description="real incident, product requirement, scheduler R2",
        ),
    )
}
EXPECT_NO_R1 = frozenset({"smoke-noop", "smoke-readiness"})


def _rows(session: Session, model: Any) -> list[Any]:
    return list(session.scalars(select(model)).all())


def capture_evidence(
    session_factory: sessionmaker[Session],
    http: Callable[[str, str, Mapping[str, str]], Any],
    prometheus_url: str,
    *,
    since: datetime,
    until: datetime,
) -> dict[str, Any]:
    """Reads only: incidents, revisions, requirements, readiness ledger, request series."""
    with session_factory() as session:
        incidents = _rows(session, IncidentRow)
        diagnoses = _rows(session, DiagnosisRow)
        requirements = _rows(session, EvidenceRequirementRow)
        readiness = [
            row
            for row in _rows(session, LifecycleObservationRow)
            if row.type in ("READY_FALSE", "READY_TRUE") and row.observed_at >= since
        ]
    query = urlencode(
        {
            "query": 'sum by (status) (rate(http_requests_total{service="order-service",'
            'route="/orders"}[15s]))',
            "start": since.timestamp(),
            "end": until.timestamp(),
            "step": "5",
        }
    )
    series = http("GET", f"{prometheus_url}/api/v1/query_range?{query}", {})
    return {
        "incidents": [
            {"id": str(row.incident_id), "created_at": row.created_at.isoformat()}
            for row in incidents
        ],
        "diagnoses": [
            {
                "diagnosis_id": row.diagnosis_id,
                "revision_number": row.revision_number,
                "trigger": row.trigger,
                "previous_diagnosis_id": row.previous_diagnosis_id,
            }
            for row in sorted(diagnoses, key=lambda item: item.diagnosis_id)
        ],
        "requirements": [
            {
                "diagnosis_id": row.diagnosis_id,
                "rule_id": row.rule_id,
                "kind": row.kind,
                "status": row.status,
                "not_before": row.not_before.isoformat(),
            }
            for row in sorted(requirements, key=lambda item: item.requirement_id)
        ],
        "readiness": [
            {"uid": row.instance_uid, "type": row.type, "observed_at": row.observed_at.isoformat()}
            for row in sorted(readiness, key=lambda item: item.observed_at)
        ],
        "order_request_rate": series,
    }


@dataclass(frozen=True, slots=True)
class Acceptance:
    accepted: bool
    reasons: tuple[str, ...]


def accept(
    scenario_id: str,
    result: RunResult,
    evidence: Mapping[str, Any] | None,
    artifact: Path | None,
) -> Acceptance:
    """Whether the smoke showed what it must; every unmet condition is named."""
    reasons: list[str] = []
    if evidence is None:
        return Acceptance(False, ("no evidence captured before teardown",))
    incidents = evidence.get("incidents") or []
    if scenario_id in EXPECT_NO_R1:
        if (result.status, result.error_type) != (RunStatus.ERROR, "NoR1"):
            reasons.append(f"terminal {result.status.value}/{result.error_type}: {result.error}")
        if incidents:
            reasons.append(f"{len(incidents)} incident(s) opened")
        if scenario_id == "smoke-readiness":
            types = [item["type"] for item in evidence.get("readiness") or []]
            if "READY_FALSE" not in types or "READY_TRUE" not in types:
                reasons.append(f"readiness ledger incomplete: {types}")
    elif scenario_id == "smoke-chain":
        if result.status is not RunStatus.RUN_OK:
            reasons.append(f"terminal {result.status.value}/{result.error_type}: {result.error}")
        if artifact is None or not artifact.exists():
            reasons.append("no v2 artifact")
        diagnoses = evidence.get("diagnoses") or []
        if [item["trigger"] for item in diagnoses] != ["INITIAL", "MANUAL", "EVIDENCE_DEADLINE"]:
            reasons.append(f"revision chain {[item['trigger'] for item in diagnoses]}")
        initial = next((item for item in diagnoses if item["revision_number"] == 1), None)
        opened = [
            item
            for item in evidence.get("requirements") or []
            if initial is not None and item["diagnosis_id"] == initial["diagnosis_id"]
        ]
        if not opened:
            reasons.append("R1 opened no requirement")
    else:
        reasons.append(f"unknown smoke {scenario_id}")
    return Acceptance(not reasons, tuple(reasons))


def write_evidence(
    path: Path, scenario_id: str, result: RunResult, evidence: Mapping[str, Any] | None,
    acceptance: Acceptance, timeline: Sequence[Mapping[str, Any]],
) -> None:  # fmt: skip
    """The smoke record next to the run's artifacts (create-only, like the artifact)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "smoke": scenario_id,
        "status": result.status.value,
        "error_type": result.error_type,
        "error": result.error,
        "accepted": acceptance.accepted,
        "reasons": list(acceptance.reasons),
        "timeline": list(timeline),
        "evidence": evidence,
    }
    with path.open("x", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")


__all__ = [
    "CHAIN_ROOT",
    "EXPECT_NO_R1",
    "READINESS",
    "SMOKES",
    "Acceptance",
    "accept",
    "capture_evidence",
    "write_evidence",
]

"""M19-6.8: an explicit probe reference onset feeds the existing temporal semantics."""

from __future__ import annotations

from datetime import timedelta

import pytest
from test_resource_mechanism import DEPLOY, NS, ONSET, _container, _deployment, _history

from packages.rca.engine import Case, build_case
from packages.rca.model import Alert, EvidenceTemporalRole, FindingKind
from packages.rca.source import InMemorySource


def _source(*, alerts: bool = False) -> InMemorySource:
    history = _history(_deployment(_container("512Mi")), _deployment(_container("128Mi")))
    return InMemorySource(
        name="probe",
        alert_items=[
            Alert(
                name="RequestErrorRate",
                service=DEPLOY.name,
                namespace=NS,
                starts_at=ONSET,
                labels={"alertname": "RequestErrorRate", "service_name": DEPLOY.name},
            )
        ]
        if alerts
        else [],
        versions=[version for versions in history.values() for version in versions],
        cutoff=ONSET + timedelta(minutes=5),
    )


def _spec_change_role(case: Case) -> EvidenceTemporalRole:
    (finding,) = [item for item in case.findings if item.kind is FindingKind.SPEC_CHANGE]
    return finding.temporal_role


def test_the_same_spec_change_is_initiating_only_with_a_reference_onset() -> None:
    without = build_case(_source())
    assert without.symptoms.onset is None
    assert _spec_change_role(without) is EvidenceTemporalRole.AMBIGUOUS
    assert all(not item.initiating_findings for item in without.hypotheses)

    probed = build_case(_source(), reference_onset=ONSET)
    assert _spec_change_role(probed) is EvidenceTemporalRole.INITIATING
    assert all(item.initiating_findings for item in probed.hypotheses)


def test_a_reference_onset_is_refused_when_alerts_exist() -> None:
    with pytest.raises(ValueError, match="incident-free probe"):
        build_case(_source(alerts=True), reference_onset=ONSET)

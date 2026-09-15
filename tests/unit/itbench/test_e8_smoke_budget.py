"""Offline readiness guards for the E8 Smoke-003 call budget."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from packages.provider import LiveModelBudget
from packages.provider.contracts import ProviderError, ProviderErrorCode


def _ledger(path: Path, calls_used: int) -> None:
    path.write_text(json.dumps({"calls_used": calls_used}), encoding="utf-8")


@pytest.mark.parametrize("calls_used", [0])
def test_smoke_preflight_accepts_full_worst_case_capacity(tmp_path: Path, calls_used: int) -> None:
    ledger = tmp_path / "smoke-003.json"
    _ledger(ledger, calls_used)
    budget = LiveModelBudget(5, ledger_path=str(ledger))
    snapshot = budget.ensure_capacity(5)
    assert snapshot.calls_used == calls_used
    assert snapshot.calls_remaining == 5


@pytest.mark.parametrize("calls_used", [1, 4, 5])
def test_smoke_preflight_rejects_partial_or_exhausted_capacity(
    tmp_path: Path, calls_used: int
) -> None:
    ledger = tmp_path / "smoke-003.json"
    _ledger(ledger, calls_used)
    budget = LiveModelBudget(5, ledger_path=str(ledger))
    with pytest.raises(ProviderError) as error:
        budget.ensure_capacity(5)
    assert error.value.code is ProviderErrorCode.LIVE_MODEL_BUDGET_EXHAUSTED


def test_smoke_preflight_does_not_consume_capacity(tmp_path: Path) -> None:
    ledger = tmp_path / "smoke-003.json"
    _ledger(ledger, 0)
    budget = LiveModelBudget(5, ledger_path=str(ledger))
    budget.ensure_capacity(5)
    assert budget.snapshot().calls_used == 0

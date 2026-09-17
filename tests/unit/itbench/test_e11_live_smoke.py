"""Fail-closed tests for the future repository-owned E11 live smoke."""

from __future__ import annotations

import pytest

from packages.evals.itbench.e11_live_smoke import (
    E11LiveSmokeAuthorizationError,
    require_live_authorization,
)


def test_live_smoke_requires_explicit_authorization() -> None:
    with pytest.raises(E11LiveSmokeAuthorizationError, match="authorize-live-provider"):
        require_live_authorization(False)


def test_live_smoke_authorization_gate_accepts_explicit_opt_in() -> None:
    require_live_authorization(True)

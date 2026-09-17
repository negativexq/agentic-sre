"""Fail-closed tests for the future repository-owned E11 live smoke."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from packages.evals.itbench.e11_live_smoke import (
    E11LiveSmokeAuthorizationError,
    require_live_authorization,
)

live_cli: Any = importlib.import_module("scripts.itbench_e11_execute")


def test_live_smoke_requires_explicit_authorization() -> None:
    with pytest.raises(E11LiveSmokeAuthorizationError, match="authorize-live-provider"):
        require_live_authorization(False)


def test_live_smoke_authorization_gate_accepts_explicit_opt_in() -> None:
    require_live_authorization(True)


def test_live_smoke_passes_explicit_dataset_root_to_runner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dataset_root = tmp_path / ".local" / "itbench-lite"
    captured: dict[str, object] = {}

    manifest = SimpleNamespace(
        provider="openai",
        model="gpt-5.6-luna",
        reasoning_effort="none",
        provider_retries=0,
    )

    def fake_runner(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"terminal": "STOP"}

    monkeypatch.setattr(live_cli, "load_e11_manifest", lambda _: manifest)
    monkeypatch.setattr(live_cli, "run_authorized_single_scenario", fake_runner)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "itbench_e11_execute.py",
            "live-smoke",
            "--manifest",
            str(tmp_path / "manifest.json"),
            "--scenario",
            "Scenario-1",
            "--dataset-root",
            str(dataset_root),
            "--output",
            str(tmp_path / "output.json"),
            "--ledger",
            str(tmp_path / "ledger.json"),
            "--authorize-live-provider",
        ],
    )

    assert live_cli.main() == 0
    assert captured["dataset_root"] == dataset_root
    assert captured["root"] == live_cli.ROOT


def test_live_smoke_does_not_substitute_repo_root_for_dataset_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    requested_root = tmp_path / "custom-dataset"
    captured: dict[str, object] = {}
    manifest = SimpleNamespace(
        provider="openai",
        model="gpt-5.6-luna",
        reasoning_effort="none",
        provider_retries=0,
    )

    monkeypatch.setattr(live_cli, "load_e11_manifest", lambda _: manifest)

    def fake_runner(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"terminal": "STOP"}

    monkeypatch.setattr(live_cli, "run_authorized_single_scenario", fake_runner)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "itbench_e11_execute.py",
            "live-smoke",
            "--manifest",
            str(tmp_path / "manifest.json"),
            "--scenario",
            "Scenario-1",
            "--dataset-root",
            str(requested_root),
            "--output",
            str(tmp_path / "output.json"),
            "--ledger",
            str(tmp_path / "ledger.json"),
            "--authorize-live-provider",
        ],
    )

    assert live_cli.main() == 0
    assert captured["dataset_root"] == requested_root
    assert captured["dataset_root"] != live_cli.ROOT


def test_live_smoke_without_authorization_fails_before_runner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    called = False

    def unexpected_runner(**_: object) -> dict[str, object]:
        nonlocal called
        called = True
        raise AssertionError("provider runner must not be reached")

    monkeypatch.setattr(live_cli, "run_authorized_single_scenario", unexpected_runner)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "itbench_e11_execute.py",
            "live-smoke",
            "--manifest",
            str(tmp_path / "manifest.json"),
            "--scenario",
            "Scenario-1",
            "--dataset-root",
            str(tmp_path / "dataset"),
            "--output",
            str(tmp_path / "output.json"),
            "--ledger",
            str(tmp_path / "ledger.json"),
        ],
    )

    with pytest.raises(SystemExit):
        live_cli.main()
    assert called is False

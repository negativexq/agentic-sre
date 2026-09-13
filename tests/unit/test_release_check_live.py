"""Offline tests for committed v0.2.0 release evidence validation."""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "release_check_live.py"
SPEC = importlib.util.spec_from_file_location("release_check_live", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

ROOT = Path(__file__).parents[2]


def _copy_artifacts(tmp_path: Path) -> Path:
    for relative in (
        "docs/benchmarks/v0.2.0-release-evidence.json",
        "docs/benchmarks/v0.2.0-live-smoke.json",
        "docs/benchmarks/v0.2.0-harness-qualification.json",
        "docs/benchmarks/v0.2.0-single-agent-live.json",
    ):
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text((ROOT / relative).read_text())
    return tmp_path


def _load(root: Path, relative: str) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((root / relative).read_text()))


def _write(root: Path, relative: str, value: dict[str, Any]) -> None:
    (root / relative).write_text(json.dumps(value))


def test_valid_committed_evidence_passes() -> None:
    assert MODULE.validate_release_evidence(ROOT)["version"] == "v0.2.0"


@pytest.mark.parametrize(
    ("relative", "mutate", "message"),
    [
        (
            "docs/benchmarks/v0.2.0-live-smoke.json",
            lambda value: value.update({"scenarios": []}),
            "no persisted terminal smoke",
        ),
        (
            "docs/benchmarks/v0.2.0-single-agent-live.json",
            lambda value: value.update({"scenarios": value["scenarios"][:-1]}),
            "benchmark scenarios are incomplete",
        ),
        (
            "docs/benchmarks/v0.2.0-harness-qualification.json",
            lambda value: value.update({"scenario_count": 9}),
            "harness qualification scenario count is not 10",
        ),
        (
            "docs/benchmarks/v0.2.0-single-agent-live.json",
            lambda value: value.update({"benchmark_api_attempts": 31}),
            "benchmark attempts mismatch",
        ),
        (
            "docs/benchmarks/v0.2.0-single-agent-live.json",
            lambda value: value["api_accounting"].update({"ending_usage": 81}),
            "benchmark final usage exceeds limit",
        ),
        (
            "docs/benchmarks/v0.2.0-single-agent-live.json",
            lambda value: value["safety"].update({"fabricated_evidence": 1}),
            "benchmark.safety.fabricated_evidence must be 0",
        ),
        (
            "docs/benchmarks/v0.2.0-single-agent-live.json",
            lambda value: value.update({"git_sha": "deadbeef"}),
            "benchmark frozen SHA mismatch",
        ),
    ],
)
def test_invalid_evidence_is_rejected(
    tmp_path: Path,
    relative: str,
    mutate: Callable[[dict[str, Any]], Any],
    message: str,
) -> None:
    root = _copy_artifacts(tmp_path)
    value = _load(root, relative)
    mutate(value)
    _write(root, relative, value)
    with pytest.raises(MODULE.ReleaseEvidenceError, match=message):
        MODULE.validate_release_evidence(root)

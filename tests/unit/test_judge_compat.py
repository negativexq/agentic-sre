"""Offline qualification for the Luna judge compatibility boundary."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from packages.evals.itbench.judge_policy import (
    LUNA_JUDGE_COMPAT_PROFILE,
    luna_judge_compatibility_hash,
)


def _prepare_luna_compatible_evaluator(source: Path):  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(
        "judge_wrapper_under_test", "scripts/itbench_official_judge.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._prepare_luna_compatible_evaluator(source)


def test_luna_compatibility_profile_is_explicit_and_stable() -> None:
    assert LUNA_JUDGE_COMPAT_PROFILE == {
        "name": "itbench_luna_judge_compat_v1",
        "version": 1,
        "provider": "openai",
        "model": "gpt-5.6-luna",
        "temperature": 1,
        "provider_max_retries": 0,
        "evaluation_attempts_per_case": 1,
        "fallback_model": "NONE",
        "fallback_temperature": "NONE",
    }
    assert len(luna_judge_compatibility_hash()) == 64


def test_compatibility_copy_scopes_temperature_and_retries() -> None:
    source = Path("/tmp/itbench-eval.QXb8kU")
    if not (source / "itbench_evaluations/agent.py").is_file():
        return
    destination, patch = _prepare_luna_compatible_evaluator(source)
    assert patch == "temperature=1; provider_max_retries=0; evaluation_attempts_per_case=1"
    source_agent = (source / "itbench_evaluations/agent.py").read_text(encoding="utf-8")
    copied_agent = (destination / "itbench_evaluations/agent.py").read_text(encoding="utf-8")
    copied_client = (destination / "itbench_evaluations/client.py").read_text(encoding="utf-8")
    assert "temperature=0," in source_agent
    assert "temperature=0," not in copied_agent
    assert "temperature=1," in copied_agent
    assert "max_retries: int = 1" in copied_agent
    assert "max_calc_retries = 1" in copied_agent
    assert "max_retries=0," in copied_client

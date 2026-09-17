"""Command-line entry point."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.cli.main import main


def test_demo_prints_json_and_writes_html(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = tmp_path / "out" / "demo.html"
    assert main(["demo", "--json", "--html", str(report)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["root_cause"]["name"] == "payment"
    html = report.read_text(encoding="utf-8")
    assert "shop/Deployment/payment" in html and "not executed" in html


def test_demo_text_output_names_the_cause(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["demo"]) == 0
    out = capsys.readouterr().out
    assert "Root cause   shop/Deployment/payment" in out
    assert "Proposed remediation (not executed)" in out


def test_demo_with_llm_flag_stays_offline(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SRE_LLM_ENABLED", raising=False)
    assert main(["demo", "--llm"]) == 0
    out = capsys.readouterr().out
    assert "live model calls are disabled" in out
    assert "Root cause   shop/Deployment/payment" in out

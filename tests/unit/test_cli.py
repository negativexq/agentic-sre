"""Command-line entry point."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.cli.main import main
from packages.rca.demo import demo_source
from packages.rca.engine import diagnose
from packages.rca.model import CausalHop, EntityRef
from packages.rca.report import diagnosis_html


def test_demo_prints_json_and_writes_html(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = tmp_path / "out" / "demo.html"
    assert main(["demo", "--json", "--html", str(report)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["root_cause"]["name"] == "payment"
    html = report.read_text(encoding="utf-8")
    assert "shop/Deployment/payment" in html and "not executed" in html
    assert "Why this cause / Causal path" in html


def test_demo_text_output_names_the_cause(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["demo"]) == 0
    out = capsys.readouterr().out
    assert "Root cause   shop/Deployment/payment" in out
    assert "Causal path" in out
    assert "Proposed remediation (not executed)" in out


def test_html_causal_path_escapes_entities_and_omits_empty_path() -> None:
    diagnosis = diagnose(demo_source()).model_copy(
        update={
            "causal_path": (
                CausalHop(
                    source=EntityRef(kind="Deployment", name="<payment>"),
                    relation="configures & explains",
                    target=EntityRef(kind="Service", name="checkout"),
                ),
            )
        }
    )
    html = diagnosis_html(diagnosis)
    assert "&lt;payment&gt;" in html and "configures &amp; explains" in html
    assert "<payment>" not in html
    empty = diagnosis_html(diagnosis.model_copy(update={"causal_path": ()}))
    assert "Why this cause / Causal path" not in empty


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({}, "live model calls are disabled"),
        ({"SRE_LLM_ENABLED": "true"}, "no model call budget"),
        ({"SRE_LLM_ENABLED": "true", "SRE_LLM_MAX_CALLS": "5"}, "OPENAI_API_KEY is not set"),
    ],
)
def test_llm_flag_refuses_to_start_without_a_ready_client(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], message: str
) -> None:
    for name in ("SRE_LLM_ENABLED", "SRE_LLM_MAX_CALLS", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(SystemExit, match=message):
        main(["demo", "--llm"])

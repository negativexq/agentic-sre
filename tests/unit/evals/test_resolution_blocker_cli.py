"""Offline CLI reads stored documents and writes only a separate report."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from scripts.resolution_blocker_analysis import main


def test_jsonl_revisions_preserve_metadata_and_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, output = tmp_path / "revisions.jsonl", tmp_path / "report.json"
    rows = [
        {
            "document": {"incident_id": "one", "resolution": "AMBIGUOUS"},
            "run_id": f"run:{i}",
            "revision_number": i,
            "engine_version": "2.17.0",
        }
        for i in range(2)
    ]
    original = "\n".join(json.dumps(row) for row in rows)
    source.write_text(original)
    monkeypatch.setattr(sys, "argv", ["analysis", str(source), "--output", str(output)])
    main()
    result = json.loads(output.read_text())
    assert result["summary"]["revision_count"] == 2
    assert [r["revision_number"] for r in result["revisions"]] == [0, 1]
    assert all(r["engine_version"] == "2.17.0" for r in result["revisions"])
    assert source.read_text() == original


def test_output_cannot_overwrite_diagnosis_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "diagnosis.json"
    original = json.dumps({"resolution": "AMBIGUOUS"})
    source.write_text(original)
    monkeypatch.setattr(sys, "argv", ["analysis", str(source), "--output", str(source)])
    with pytest.raises(SystemExit):
        main()
    assert source.read_text() == original

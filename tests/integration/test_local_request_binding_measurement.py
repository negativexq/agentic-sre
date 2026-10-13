"""Small real-loopback run checks denominator, mechanism and control accounting."""

from pathlib import Path

from scripts.measure_local_request_binding import measure


def test_local_capture_uses_independent_request_denominator_and_negative_controls(
    tmp_path: Path,
) -> None:
    output = tmp_path / "measurement"
    result = measure(output, repeats=2, workers=2)
    assert result["scenario_validation_passed"] is True
    assert result["positive_binding_coverage"] == {
        "corroborated": 6,
        "expected_failed_requests": 6,
        "ratio": 1.0,
    }
    assert result["scenarios"]["refusal"]["error_types"] == {"ConnectionRefusedError": 2}
    assert result["scenarios"]["timeout"]["error_types"] == {"TimeoutError": 2}
    assert result["scenarios"]["handled-http503"]["corroborated"] == 0
    assert result["scenarios"]["success"]["dependency_error_logs"] == 0
    assert sum(row["cases"] for row in result["controls"].values()) == 36
    assert result["false_corroborated_controls"] == 0
    assert result["rca_evaluated"] is False
    assert result["manifest_membership"] == "NOT_VERIFIED_LOCAL_FILE_CAPTURE"
    assert (output / "driver-ledger.json").exists()
    assert (output / "frozen-evidence.json").exists()
    assert (output / "runner.py").exists()

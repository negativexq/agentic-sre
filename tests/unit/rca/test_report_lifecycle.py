from __future__ import annotations

from datetime import UTC, datetime, timedelta

from packages.rca.report import Lifecycle, _fmt_delta, _lifecycle_section

T0 = datetime(2026, 9, 22, 10, 0, 0, tzinfo=UTC)


def test_fmt_delta_reports_seconds_then_minutes() -> None:
    assert _fmt_delta(T0, T0 + timedelta(seconds=30)) == "30.0 s"
    assert _fmt_delta(T0, T0 + timedelta(minutes=2)) == "2.0 min"


def test_fmt_delta_handles_missing_and_negative_endpoints() -> None:
    assert _fmt_delta(None, T0) == "—"
    assert _fmt_delta(T0, None) == "—"
    # A diagnosis clock earlier than the alert is not a negative duration to show.
    assert _fmt_delta(T0, T0 - timedelta(seconds=5)) == "—"


def test_lifecycle_section_shows_the_full_timeline() -> None:
    section = _lifecycle_section(
        Lifecycle(
            alert_fired=T0,
            incident_opened=T0 + timedelta(seconds=20),
            diagnosis_ready=T0 + timedelta(seconds=45),
            reads=6,
            evidence=3,
            model_calls=0,
        )
    )
    assert "Alert fired" in section
    assert "Incident opened" in section
    assert "Root cause diagnosed" in section
    # Alert → diagnosis total is 45 s.
    assert "45.0 s" in section
    assert "6" in section  # reads
    assert "0" in section  # model calls


def test_lifecycle_section_marks_a_still_running_diagnosis() -> None:
    section = _lifecycle_section(
        Lifecycle(
            alert_fired=T0,
            incident_opened=T0 + timedelta(seconds=20),
            diagnosis_ready=None,
            reads=0,
            evidence=0,
            model_calls=0,
        )
    )
    assert "auto-diagnosis running" in section
    assert "pending" in section

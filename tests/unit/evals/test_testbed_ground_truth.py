"""Testbed ground truth: timeline validity, the frozen manifest, the journal and the oracle."""

from __future__ import annotations

import stat
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from packages.evals.live import actions
from packages.evals.live.ground_truth import (
    Chain,
    Link,
    RunRecord,
    ScenarioSpec,
    Source,
    Stamp,
    SuiteManifest,
    TestbedStore,
    Timeline,
    assemble_run,
    assign_tier,
    chain_problems,
    timeline_problems,
)
from packages.evals.live.journal import (
    ROLE_ALERT_OBSERVED,
    ROLE_CAUSE_CREATED,
    ROLE_EXECUTION_OBSERVED,
    InjectorJournal,
    injector_stamps,
)
from packages.evals.live.oracle import (
    HttpProbe,
    Oracle,
    ProbeResult,
    ProbeRoles,
    SeriesWriter,
    all_recovered_at,
    estimate_offset_seconds,
    first_run_start,
    oracle_stamps,
)

T0 = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def stamp(name: str, seconds: float) -> Stamp:
    source = {
        "cause_created_at": Source.INJECTOR,
        "execution_started_at": Source.INJECTOR,
        "alert_fired_at": Source.INJECTOR,
    }.get(name, Source.ORACLE)
    return Stamp(at=at(seconds), source=source)


def timeline(**overrides: Any) -> Timeline:
    base = {
        "cause_created_at": stamp("cause_created_at", 0),
        "execution_started_at": stamp("execution_started_at", 2),
        "target_effect_at": stamp("target_effect_at", 5),
        "propagation_started_at": stamp("propagation_started_at", 7),
        "symptom_started_at": stamp("symptom_started_at", 9),
        "alert_fired_at": stamp("alert_fired_at", 40),
        "recovery_at": stamp("recovery_at", 120),
    }
    return Timeline(**{**base, **overrides})


def cause_chain() -> Chain:
    return Chain(
        links=(
            Link(
                role="cause",
                actor="chaos/Schedule/delay",
                instance_uid="s1",
                knowable=True,
                mechanism="NetworkChaos",
            ),
        )
    )


# ---- timeline and chain ---------------------------------------------------------------------


def test_a_complete_ordered_timeline_is_valid() -> None:
    assert timeline_problems(timeline(), "direct-pod-fault", 0.2) == []


def test_each_defect_makes_the_run_invalid() -> None:
    assert "missing recovery_at" in timeline_problems(
        timeline(recovery_at=None), "direct-pod-fault", 0
    )
    assert any(
        "precedes" in p
        for p in timeline_problems(
            timeline(target_effect_at=stamp("target_effect_at", 1)), "direct-pod-fault", 0
        )
    )
    wrong_source = timeline(cause_created_at=Stamp(at=at(0), source=Source.ORACLE))
    assert "cause_created_at must come from the injector" in timeline_problems(
        wrong_source, "direct-pod-fault", 0
    )
    assert any("clock offset" in p for p in timeline_problems(timeline(), "direct-pod-fault", 1.5))


def test_only_a_negative_control_may_lack_an_effect_path() -> None:
    no_path = timeline(target_effect_at=None, propagation_started_at=None)
    assert timeline_problems(no_path, "negative-control", 0) == []
    assert timeline_problems(no_path, "dependency-fault", 0) != []
    assert timeline_problems(timeline(alert_fired_at=None), "negative-control", 0) != []


def test_naive_instants_are_refused() -> None:
    with pytest.raises(ValidationError):
        Stamp(at=datetime(2025, 1, 1, 12, 0), source=Source.INJECTOR)


def test_a_link_records_an_instance_uid_exactly_when_it_is_knowable() -> None:
    with pytest.raises(ValidationError):
        Link(role="cause", actor="a/B/c", knowable=True, mechanism="m")
    with pytest.raises(ValidationError):
        Link(role="cause", actor="a/B/c", instance_uid="u", knowable=False, mechanism="m")
    Link(role="cause", actor="a/B/c", knowable=False, mechanism="m")


def test_chain_rules_follow_the_family() -> None:
    assert chain_problems(Chain(), "negative-control") == [
        "a negative control must state its construction"
    ]
    assert chain_problems(Chain(construction="no call edge"), "negative-control") == []
    assert chain_problems(cause_chain(), "competing-causes") != []
    assert chain_problems(Chain(), "direct-pod-fault") == ["the chain names no cause"]


# ---- manifest and store ---------------------------------------------------------------------


def manifest(**overrides: Any) -> SuiteManifest:
    specs = (
        ScenarioSpec(
            scenario_id="pod-fault",
            family="direct-pod-fault",
            tier="DEV",
            repeats=2,
            seeds=(11, 12),
            parameters={"offset_seconds": {"low": 0, "high": 30}},  # type: ignore[dict-item]
        ),
    )
    return SuiteManifest(
        suite_id="s1",
        engine_version="2.1.0",
        created_at=T0,
        salt="salt",
        scenarios=specs,
        acceptance={"false_resolved": 0},
        **overrides,
    ).frozen()


def test_the_split_is_deterministic_and_depends_on_the_salt() -> None:
    assert assign_tier("a", "x") == assign_tier("a", "x")
    tiers = {assign_tier(f"s{i}", "x", 0.5) for i in range(40)}
    assert tiers == {"DEV", "HOLDOUT"}


def test_a_scenario_needs_one_distinct_seed_per_repeat() -> None:
    with pytest.raises(ValidationError):
        ScenarioSpec(
            scenario_id="x", family="direct-pod-fault", tier="DEV", repeats=2, seeds=(1, 1)
        )
    with pytest.raises(ValidationError):
        ScenarioSpec(scenario_id="x", family="direct-pod-fault", tier="DEV", repeats=2, seeds=(1,))


def test_a_manifest_is_verified_only_while_it_is_unchanged() -> None:
    frozen = manifest()
    assert frozen.verified()
    assert not frozen.model_copy(update={"salt": "other"}).verified()
    assert not SuiteManifest.model_validate(
        {**frozen.model_dump(mode="json"), "sha256": ""}
    ).verified()


def test_a_run_is_assembled_only_against_a_frozen_manifest() -> None:
    frozen = manifest()
    unfrozen = frozen.model_copy(update={"sha256": ""})
    with pytest.raises(ValueError):
        assemble_run(
            unfrozen,
            "pod-fault",
            0,
            timeline=timeline(),
            chain=cause_chain(),
            clock_offset_seconds=0,
        )
    with pytest.raises(ValueError):
        assemble_run(
            frozen, "pod-fault", 5, timeline=timeline(), chain=cause_chain(), clock_offset_seconds=0
        )
    record = assemble_run(
        frozen, "pod-fault", 1, timeline=timeline(), chain=cause_chain(), clock_offset_seconds=0
    )
    assert record.valid and record.seed == 12 and record.manifest_sha256 == frozen.sha256
    bad = assemble_run(
        frozen,
        "pod-fault",
        0,
        timeline=timeline(recovery_at=None),
        chain=cause_chain(),
        clock_offset_seconds=0,
    )
    assert not bad.valid and bad.invalid_reasons == ("missing recovery_at",)


def test_the_store_writes_once_read_only_and_refuses_a_second_write(tmp_path: Path) -> None:
    store = TestbedStore(tmp_path)
    frozen = manifest()
    store.write_manifest(frozen)
    store.write_manifest(frozen)  # identical content is not a second write
    with pytest.raises(FileExistsError):
        store.write_manifest(
            frozen.model_copy(update={"acceptance": {"false_resolved": 1}}).frozen()
        )
    record = assemble_run(
        frozen, "pod-fault", 0, timeline=timeline(), chain=cause_chain(), clock_offset_seconds=0
    )
    directory = store.write_run(record)
    files = sorted(p.name for p in directory.iterdir())
    assert files == ["chain.json", "run.json", "timeline.json"]
    assert all(not (p.stat().st_mode & stat.S_IWUSR) for p in directory.iterdir())
    with pytest.raises(FileExistsError):
        store.write_run(record)
    assert RunRecord.model_validate_json((directory / "run.json").read_bytes()) == record


def test_a_run_recorded_against_another_manifest_is_refused(tmp_path: Path) -> None:
    store = TestbedStore(tmp_path)
    store.write_manifest(manifest())
    other = manifest().model_copy(update={"salt": "different"}).frozen()
    record = assemble_run(
        other, "pod-fault", 0, timeline=timeline(), chain=cause_chain(), clock_offset_seconds=0
    )
    with pytest.raises(ValueError):
        store.write_run(record)


# ---- journal --------------------------------------------------------------------------------


def test_the_journal_appends_and_derives_only_the_injector_fields(tmp_path: Path) -> None:
    ticks = iter(at(i) for i in range(100))
    journal = InjectorJournal(tmp_path / "journal.jsonl", clock=lambda: next(ticks))
    journal.record(verb="get", object="pod a", response="ok")
    journal.record(verb="apply", object="networkchaos x", role=ROLE_CAUSE_CREATED, uid="c1")
    journal.record(
        verb="get",
        object="events",
        role=ROLE_EXECUTION_OBSERVED,
        payload={"applied_at": at(50).isoformat()},
    )
    journal.record(
        verb="get",
        object="alerts",
        role=ROLE_ALERT_OBSERVED,
        payload={"starts_at": "2025-01-01T12:01:00Z"},
    )
    journal.record(verb="apply", object="again", role=ROLE_CAUSE_CREATED, ok=False)
    reopened = InjectorJournal(tmp_path / "journal.jsonl")
    assert [e.seq for e in reopened.entries()] == [1, 2, 3, 4, 5]
    stamps = injector_stamps(reopened.entries())
    assert stamps == {
        "cause_created_at": at(1),
        "execution_started_at": at(50),
        "alert_fired_at": datetime(2025, 1, 1, 12, 1, tzinfo=UTC),
    }


def test_a_missing_observation_leaves_its_field_out() -> None:
    assert injector_stamps([]) == {}


def test_kubectl_calls_are_journaled_with_their_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Result:
        returncode = 0
        stdout = "networkchaos/x created"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Result())
    journal = InjectorJournal(tmp_path / "j.jsonl")
    context = actions.Context(journal=journal)
    actions.kubectl("apply", "-f", "chaos.yaml", context=context, role=ROLE_CAUSE_CREATED)
    (entry,) = journal.entries()
    assert (entry.verb, entry.role, entry.ok) == ("apply", ROLE_CAUSE_CREATED, True)
    assert "created" in entry.response


# ---- oracle ---------------------------------------------------------------------------------


def series(probe: str, pattern: str, start: float = 0.0) -> list[ProbeResult]:
    return [ProbeResult(probe, at(start + i), ok=char == "+") for i, char in enumerate(pattern)]


def test_a_single_blip_is_not_an_effect_and_a_run_reports_its_first_sample() -> None:
    blip = series("p", "++-+++-+")
    assert first_run_start(blip, "p", ok=False, after=at(0)) is None
    real = series("p", "+++--+---")
    assert first_run_start(real, "p", ok=False, after=at(0)) == at(6)
    assert first_run_start(real, "p", ok=True, after=at(0)) == at(0)


def test_recovery_needs_every_probe_healthy_for_a_run() -> None:
    combined = series("a", "---+++++") + series("b", "-----+++")
    assert all_recovered_at(combined, ["a", "b"], after=at(0)) == at(5)
    assert all_recovered_at(series("a", "---+"), ["a"], after=at(0)) is None


def test_the_four_oracle_fields_come_from_the_series() -> None:
    target = series("target", "++++-----------++++++++")
    downstream = series("down", "++++++--------------+++")
    symptom = series("symptom", "++++++++----------++++")
    roles = ProbeRoles(target="target", downstream="down", symptom="symptom")
    stamps = oracle_stamps(
        target + downstream + symptom,
        roles,
        execution_started_at=at(2),
        cause_removed_at=at(12),
    )
    assert stamps["target_effect_at"].at == at(4)
    assert stamps["propagation_started_at"].at == at(6)
    assert stamps["symptom_started_at"].at == at(8)
    assert stamps["recovery_at"].at > at(12)
    assert all(s.source is Source.ORACLE for s in stamps.values())


def test_a_series_that_never_degrades_yields_no_effect_field() -> None:
    healthy = series("target", "++++++++") + series("symptom", "++++++++")
    stamps = oracle_stamps(
        healthy,
        ProbeRoles(target="target", symptom="symptom"),
        execution_started_at=at(0),
        cause_removed_at=at(4),
    )
    assert "target_effect_at" not in stamps and "symptom_started_at" not in stamps
    assert "recovery_at" in stamps


def test_the_offset_estimate_is_the_median_and_ignores_an_outlier() -> None:
    def sample(offset: float, rtt: float = 0.02) -> tuple[datetime, datetime, datetime]:
        sent = at(10)
        received = sent + timedelta(seconds=rtt)
        return sent, sent + timedelta(seconds=rtt / 2 + offset), received

    estimate = estimate_offset_seconds([sample(0.30), sample(0.31), sample(0.29), sample(4.0)])
    assert abs(estimate - 0.305) < 0.02
    with pytest.raises(ValueError):
        estimate_offset_seconds([])


def test_the_series_round_trips_and_the_oracle_writes_every_probe(tmp_path: Path) -> None:
    class Fixed:
        def __init__(self, name: str, ok: bool) -> None:
            self.name, self._ok = name, ok

        def check(self) -> ProbeResult:
            return ProbeResult(self.name, at(0), self._ok, 0.01, "d")

    writer = SeriesWriter(tmp_path / "series.jsonl")
    Oracle([Fixed("a", True), Fixed("b", False)], writer).sample_once()
    assert [(r.probe, r.ok) for r in writer.read()] == [("a", True), ("b", False)]


def test_an_http_probe_reports_health_failure_and_slowness() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/ok":
                self.send_response(200)
            elif self.path == "/slow":
                threading.Event().wait(0.3)
                self.send_response(200)
            else:
                self.send_response(503)
            self.end_headers()

        def log_message(self, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert HttpProbe("ok", f"{base}/ok").check().ok
        broken = HttpProbe("bad", f"{base}/bad").check()
        assert not broken.ok and broken.detail == "HTTP 503"
        slow = HttpProbe("slow", f"{base}/slow", max_latency_seconds=0.1).check()
        assert not slow.ok and slow.detail.startswith("slow")
        down = HttpProbe("down", "http://127.0.0.1:1/", timeout_seconds=0.5).check()
        assert not down.ok
    finally:
        server.shutdown()


def test_an_effect_is_stamped_when_it_was_observed_so_it_never_precedes_its_cause() -> None:
    # a sample that began 0.4 s before the fault and was slowed by it ends 3 s after that start
    slowed = [
        ProbeResult("p", at(-0.4) + timedelta(seconds=i * 4), ok=False, latency_seconds=3.0)
        for i in range(3)
    ]
    stamp = first_run_start(slowed, "p", ok=False, after=at(0))
    assert stamp == at(-0.4) + timedelta(seconds=3.0)
    assert stamp is not None and stamp >= at(0)
    assert slowed[0].observed_at == at(2.6)


def test_a_slow_probe_does_not_delay_the_start_of_another_in_the_same_tick(tmp_path: Path) -> None:
    from packages.evals.live.oracle import LatencyProbe, Measurement

    starts: dict[str, float] = {}

    def slow() -> Measurement:
        starts["slow"] = time.monotonic()
        time.sleep(0.4)
        return Measurement(datetime.now(UTC), True, 0.4)

    def fast() -> Measurement:
        starts["fast"] = time.monotonic()
        return Measurement(datetime.now(UTC), True, 0.01)

    oracle = Oracle(
        [LatencyProbe("slow", slow, {"slow": None}), LatencyProbe("fast", fast, {"fast": None})],
        SeriesWriter(tmp_path / "s.jsonl"),
    )
    oracle.sample_once()
    assert abs(starts["fast"] - starts["slow"]) < 0.15


def test_an_applied_event_read_within_its_one_second_resolution_is_the_creation_instant() -> None:
    from packages.evals.live.journal import JournalEntry

    def entries(applied_at: datetime) -> list[JournalEntry]:
        return [
            JournalEntry(
                seq=1, at=at(10.5), verb="apply", object="x", ok=True, role=ROLE_CAUSE_CREATED
            ),
            JournalEntry(
                seq=2,
                at=at(20),
                verb="observe",
                object="e",
                ok=True,
                role=ROLE_EXECUTION_OBSERVED,
                payload={"applied_at": applied_at.isoformat()},
            ),
        ]

    truncated = injector_stamps(
        entries(at(10.0))
    )  # the event says 10 s, the create returned at 10.5 s
    assert truncated["execution_started_at"] == truncated["cause_created_at"] == at(10.5)
    genuine = injector_stamps(
        entries(at(8.0))
    )  # 2.5 s earlier is not resolution: left as it was seen
    assert genuine["execution_started_at"] == at(8.0)
    later = injector_stamps(entries(at(11.0)))
    assert later["execution_started_at"] == at(11.0)

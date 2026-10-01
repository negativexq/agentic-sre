"""The oracle (testbed contract §2, §4): independent probes whose series ground the timeline.

It has no connection to the engine. It samples endpoints with the injector's UTC clock, writes every
sample, and the oracle-sourced timeline fields are derived from the series alone: the first run of
consecutive failures for an effect, the first run of consecutive successes for a recovery.
"""

from __future__ import annotations

import json
import os
import statistics
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from packages.evals.live.ground_truth import Source, Stamp

DEFAULT_RUN_LENGTH = 3


@dataclass(frozen=True)
class ProbeResult:
    probe: str
    at: datetime
    ok: bool
    latency_seconds: float | None = None
    detail: str = ""

    @property
    def observed_at(self) -> datetime:
        """When the outcome was known: the sample's end. A sample that started before a fault and was
        slowed by it can only finish after the fault began, so an effect stamped here never precedes
        its cause."""
        if self.latency_seconds is None:
            return self.at
        return self.at + timedelta(seconds=self.latency_seconds)


class Probe(Protocol):
    name: str

    def check(self) -> ProbeResult | Sequence[ProbeResult]: ...


@dataclass(frozen=True)
class Measurement:
    """One timed call: whether it succeeded at all, how long it took, and when it started."""

    at: datetime
    succeeded: bool
    latency_seconds: float | None
    detail: str = ""


def http_measurement(
    url: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    timeout_seconds: float = 2.0,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Measurement:
    """Time one HTTP call; a transport error or a non-2xx answer is a failure, never an exception."""
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={"content-type": "application/json"} if body is not None else {},
    )
    started, at = time.monotonic(), clock()
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            ok, detail = 200 <= response.status < 300, f"HTTP {response.status}"
    except urllib.error.HTTPError as error:
        ok, detail = False, f"HTTP {error.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        ok, detail = False, type(error).__name__
    return Measurement(at, ok, time.monotonic() - started, detail)


@dataclass
class LatencyProbe:
    """One measured call judged against several latency thresholds, one result per view.

    A view is a name and a maximum latency (``None`` means only success matters). The same call can
    therefore back a calibrated "degraded" view and the stricter "alert condition" view without a second
    request, so both see the same instant.
    """

    name: str
    measure: Callable[[], Measurement]
    views: Mapping[str, float | None]

    def check(self) -> list[ProbeResult]:
        measurement = self.measure()
        results = []
        for view, limit in self.views.items():
            healthy = measurement.succeeded and (
                limit is None
                or (
                    measurement.latency_seconds is not None and measurement.latency_seconds <= limit
                )
            )
            detail = measurement.detail if measurement.succeeded else measurement.detail or "failed"
            results.append(
                ProbeResult(view, measurement.at, healthy, measurement.latency_seconds, detail)
            )
        return results


@dataclass
class HttpProbe:
    """One endpoint: healthy when it answers 2xx within ``max_latency_seconds``."""

    name: str
    url: str
    timeout_seconds: float = 2.0
    max_latency_seconds: float | None = None
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def check(self) -> ProbeResult:
        started = time.monotonic()
        at = self.clock()
        try:
            with urllib.request.urlopen(self.url, timeout=self.timeout_seconds) as response:
                healthy = 200 <= response.status < 300
                detail = f"HTTP {response.status}"
        except urllib.error.HTTPError as error:
            healthy, detail = False, f"HTTP {error.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            healthy, detail = False, type(error).__name__
        latency = time.monotonic() - started
        if healthy and self.max_latency_seconds is not None and latency > self.max_latency_seconds:
            healthy, detail = False, f"slow {latency:.3f}s"
        return ProbeResult(self.name, at, healthy, latency, detail)


class SeriesWriter:
    """Append-only JSON lines, synced per sample."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, result: ProbeResult) -> None:
        with self._lock:
            self._append(result)

    def _append(self, result: ProbeResult) -> None:
        line = json.dumps(
            {
                "probe": result.probe,
                "at": result.at.astimezone(UTC).isoformat(),
                "ok": result.ok,
                "latency_seconds": result.latency_seconds,
                "detail": result.detail,
            },
            sort_keys=True,
        )
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def read(self) -> list[ProbeResult]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            raw = json.loads(line)
            out.append(
                ProbeResult(
                    raw["probe"],
                    datetime.fromisoformat(raw["at"]),
                    raw["ok"],
                    raw["latency_seconds"],
                    raw["detail"],
                )
            )
        return out


class Oracle:
    """Samples every probe once per interval until stopped."""

    def __init__(
        self, probes: Sequence[Probe], writer: SeriesWriter, *, interval_seconds: float = 1.0
    ) -> None:
        self._probes = tuple(probes)
        self._writer = writer
        self._interval = interval_seconds

    def sample_once(self) -> list[ProbeResult]:
        """Every probe once, concurrently: a slow probe (a call slowed by the fault under test) must not
        delay the start of the others, or the timeline would show the coupling instead of the world."""
        with ThreadPoolExecutor(max_workers=max(1, len(self._probes))) as pool:
            outcomes = [future.result() for future in [pool.submit(p.check) for p in self._probes]]
        results: list[ProbeResult] = []
        for outcome in outcomes:
            results.extend([outcome] if isinstance(outcome, ProbeResult) else outcome)
        for result in results:
            self._writer.write(result)
        return results

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            started = time.monotonic()
            self.sample_once()
            stop.wait(max(0.0, self._interval - (time.monotonic() - started)))


def _of(series: Sequence[ProbeResult], probe: str) -> list[ProbeResult]:
    return sorted((r for r in series if r.probe == probe), key=lambda r: r.observed_at)


def first_run_start(
    series: Sequence[ProbeResult],
    probe: str,
    *,
    ok: bool,
    after: datetime,
    length: int = DEFAULT_RUN_LENGTH,
) -> datetime | None:
    """The instant a run of ``length`` consecutive samples with ``ok`` first begins, at or after ``after``.

    A single blip never counts; the instant reported is the first sample of the run.
    """
    samples = [r for r in _of(series, probe) if r.observed_at >= after]
    for index in range(len(samples) - length + 1):
        window = samples[index : index + length]
        if all(r.ok is ok for r in window):
            return window[0].observed_at
    return None


def all_recovered_at(
    series: Sequence[ProbeResult],
    probes: Sequence[str],
    *,
    after: datetime,
    length: int = DEFAULT_RUN_LENGTH,
) -> datetime | None:
    """First instant at which every probe has been healthy for ``length`` samples in a row."""
    starts = [first_run_start(series, p, ok=True, after=after, length=length) for p in probes]
    return None if any(s is None for s in starts) else max(s for s in starts if s is not None)


@dataclass(frozen=True)
class ProbeRoles:
    """Which probe stands for what (chosen per scenario, recorded in its chain)."""

    target: str
    symptom: str
    downstream: str | None = None
    everything: tuple[str, ...] = ()


def oracle_stamps(
    series: Sequence[ProbeResult],
    roles: ProbeRoles,
    *,
    execution_started_at: datetime,
    cause_removed_at: datetime,
    length: int = DEFAULT_RUN_LENGTH,
) -> dict[str, Stamp]:
    """The four oracle-sourced fields; a field the series cannot support is left out."""
    stamps: dict[str, Stamp] = {}

    def put(name: str, at: datetime | None) -> None:
        if at is not None:
            stamps[name] = Stamp(at=at, source=Source.ORACLE)

    effect = first_run_start(
        series, roles.target, ok=False, after=execution_started_at, length=length
    )
    put("target_effect_at", effect)
    if effect is not None and roles.downstream is not None:
        put(
            "propagation_started_at",
            first_run_start(series, roles.downstream, ok=False, after=effect, length=length),
        )
    put(
        "symptom_started_at",
        first_run_start(series, roles.symptom, ok=False, after=execution_started_at, length=length),
    )
    watched = roles.everything or tuple(
        dict.fromkeys(p for p in (roles.target, roles.downstream, roles.symptom) if p is not None)
    )
    put("recovery_at", all_recovered_at(series, watched, after=cause_removed_at, length=length))
    return stamps


def estimate_offset_seconds(samples: Sequence[tuple[datetime, datetime, datetime]]) -> float:
    """Median clock offset of a remote host against the injector, NTP style.

    Each sample is ``(sent, remote_reading, received)`` on the injector's clock and the remote's.
    Positive means the remote clock is ahead.
    """
    if not samples:
        raise ValueError("at least one clock sample is needed")
    offsets = [
        (remote - (sent + (received - sent) / 2)).total_seconds()
        for sent, remote, received in samples
    ]
    return float(statistics.median(offsets))

"""Product-mode long run (roadmap B7) and the independent check of m21 §11 (C15).

One control plane on one database, load all along, faults injected from a frozen schedule; not a testbed run, so
nothing is scored per fault and the control plane is restarted only where the schedule says so.

The rhythm is deliberately unlike the runs the §11 shadow was first measured on (a fixed period, a fixed 100 s
duration, a fixed rotation, one fault at a time): gaps drawn from three bands, seeded durations and order, a fault on
the same target right after another, overlapping faults, one long fault that is still in effect when a later one
starts, and a quiet stretch. The schedule is generated from a seed and frozen (``schedule.json`` and its sha256)
before the run; ``measure`` reads the run's database and injector journal and applies the shadow unchanged.

Commands (``python -m packages.evals.live.longrun``):

* ``schedule --tag T --seed S [--minutes M]`` writes ``.local/longrun/T/schedule.json`` once;
* ``run --tag T`` runs the frozen schedule;
* ``measure --tag T`` reports the pre-registered metrics for W = 5, 15 and 30 minutes.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import shutil
import stat
import subprocess
import sys
import time
import traceback
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
ROOT = REPO / ".local" / "longrun"
PAYMENT_FAULTS = ("network-delay", "env-delay")
KINDS = ("network-delay", "cpu-stress", "env-delay")
# minutes from the end of one block to the start of the next: (band, low, high, weight)
GAP_BANDS = (("short", 4.0, 10.0, 0.5), ("medium", 15.0, 25.0, 0.35), ("long", 35.0, 50.0, 0.15))
FIRST_FAULT_MINUTES = 8.0
TAIL_MINUTES = (
    10.0  # the last fault ends at least this long before the run does, so its incidents resolve
)
RESTART_MINUTES = 90.0  # the control-plane restart, taken at the first moment no fault is active
WINDOWS_MINUTES = (5, 15, 30)


def target_of(kind: str) -> str:
    return "order-service" if kind == "cpu-stress" else "payment-service"


@dataclass(frozen=True)
class PlannedFault:
    name: str
    kind: str
    start_seconds: float
    duration_seconds: float
    latency_ms: int = 0
    cpu_workers: int = 0
    block: str = "single"

    @property
    def end_seconds(self) -> float:
        return self.start_seconds + self.duration_seconds


@dataclass(frozen=True)
class Schedule:
    seed: int
    minutes: float
    restart_minutes: float
    faults: tuple[PlannedFault, ...]
    quiet: tuple[
        tuple[float, float], ...
    ]  # (start, end) minutes of the deliberately quiet stretches

    def document(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "minutes": self.minutes,
            "restart_minutes": self.restart_minutes,
            "faults": [asdict(f) for f in self.faults],
            "quiet": [list(q) for q in self.quiet],
        }

    def canonical(self) -> bytes:
        return json.dumps(self.document(), sort_keys=True, separators=(",", ":")).encode()

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical()).hexdigest()

    @classmethod
    def load(cls, document: Mapping[str, Any]) -> Schedule:
        return cls(
            seed=int(document["seed"]),
            minutes=float(document["minutes"]),
            restart_minutes=float(document["restart_minutes"]),
            faults=tuple(PlannedFault(**f) for f in document["faults"]),
            quiet=tuple((float(a), float(b)) for a, b in document["quiet"]),
        )


def _single(rng: random.Random, kind: str, start: float, low: float, high: float) -> dict[str, Any]:
    return {
        "kind": kind,
        "start_seconds": round(start, 1),
        "duration_seconds": round(rng.uniform(low, high), 1),
        "latency_ms": rng.randint(300, 600) if kind == "network-delay" else rng.randint(600, 1500),
        "cpu_workers": rng.randint(20, 28) if kind == "cpu-stress" else 0,
    }


def _block(rng: random.Random, block: str, start: float) -> list[dict[str, Any]]:
    """The faults of one block, starting at ``start`` seconds; payment faults never overlap each other."""
    if block == "single":
        return [_single(rng, rng.choice(KINDS), start, 60, 300)]
    if block == "same-target":
        first, second = rng.sample(PAYMENT_FAULTS, 2)
        a = _single(rng, first, start, 60, 180)
        b_start = a["start_seconds"] + a["duration_seconds"] + rng.uniform(4, 8) * 60
        return [a, _single(rng, second, b_start, 60, 180)]
    if block == "overlap":
        stress = _single(rng, "cpu-stress", start, 120, 300)
        payment = _single(rng, rng.choice(PAYMENT_FAULTS), start + rng.uniform(30, 90), 60, 180)
        return [stress, payment]
    if block == "long-overlap":
        stress = _single(rng, "cpu-stress", start, 1200, 1200)
        later = start + rng.uniform(8, 12) * 60
        return [stress, _single(rng, rng.choice(PAYMENT_FAULTS), later, 60, 180)]
    raise ValueError(block)


def _gap(rng: random.Random) -> float:
    band = rng.choices(GAP_BANDS, weights=[b[3] for b in GAP_BANDS])[0]
    return rng.uniform(band[1], band[2]) * 60


def rhythm_schedule(seed: int, minutes: float = 180.0) -> Schedule:
    """Every special block once, in seeded order, with single faults between them while time allows."""
    rng = random.Random(seed)
    specials = ["same-target", "overlap", "overlap", "long-overlap", "quiet"]
    rng.shuffle(specials)
    end_limit = (minutes - TAIL_MINUTES) * 60
    planned: list[dict[str, Any]] = []
    quiet: list[tuple[float, float]] = []
    cursor = FIRST_FAULT_MINUTES * 60

    def place(block: str) -> bool:
        nonlocal cursor
        if block == "quiet":
            length = rng.uniform(32, 40) * 60
            if cursor + length > end_limit:
                return False
            quiet.append((round(cursor / 60, 1), round((cursor + length) / 60, 1)))
            cursor += length
            return True
        faults = _block(rng, block, cursor)
        last = max(f["start_seconds"] + f["duration_seconds"] for f in faults)
        if last > end_limit:
            return False
        for fault in faults:
            planned.append({**fault, "block": block})
        cursor = last + _gap(rng)
        return True

    for special in specials:
        if rng.random() < 0.5:
            place("single")
        if not place(special):
            raise ValueError(f"seed {seed}: the {special} block does not fit in {minutes} minutes")
    while place("single"):
        pass
    planned.sort(key=lambda f: f["start_seconds"])
    faults = tuple(PlannedFault(name=f"ic-{i}", **f) for i, f in enumerate(planned))
    return Schedule(seed, minutes, RESTART_MINUTES, faults, tuple(quiet))


def schedule_problems(schedule: Schedule) -> list[str]:
    """What the rhythm promises, checked on the frozen schedule."""
    problems: list[str] = []
    payment = sorted(
        (f for f in schedule.faults if f.kind in PAYMENT_FAULTS), key=lambda f: f.start_seconds
    )
    for a, b in zip(payment, payment[1:], strict=False):
        if b.start_seconds < a.end_seconds:
            problems.append(f"payment faults {a.name} and {b.name} overlap")
    overlaps = sum(
        1
        for i, a in enumerate(schedule.faults)
        for b in schedule.faults[i + 1 :]
        if b.start_seconds < a.end_seconds and a.start_seconds < b.end_seconds
    )
    if overlaps < 3:
        problems.append(f"only {overlaps} overlapping pairs")
    if not any(b - a >= 30 for a, b in schedule.quiet):
        problems.append("no quiet stretch of 30 minutes")
    if not any(f.duration_seconds >= 1200 for f in schedule.faults):
        problems.append("no long fault")
    if max(f.end_seconds for f in schedule.faults) > (schedule.minutes - TAIL_MINUTES) * 60:
        problems.append("a fault ends inside the tail")
    return problems


# ---- the run ------------------------------------------------------------------------------------

SAMPLE_EVERY = 300.0
SAMPLE_QUERIES = {
    "incidents": "select count(*) from incidents",
    "open": "select count(*) from incidents where status='OPEN'",
    "diagnoses": "select count(*) from diagnoses",
    "gaps": "select count(*) from change_stream_gaps",
    "segments": "select count(*) from stream_follow_segments",
    "events": "select count(*) from event_versions",
    "objects": "select count(*) from object_versions",
}


def _log(message: str) -> None:
    print(f"{datetime.now(UTC).strftime('%H:%M:%S')} {message}", flush=True)


def _sql(database: str, query: str) -> str:
    result = subprocess.run(
        [
            "docker",
            "exec",
            "agentic-sre-cp-pg",
            "psql",
            "-U",
            "postgres",
            "-d",
            database,
            "-Atc",
            query,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else f"ERR:{result.stderr.strip()[:80]}"


def _cp_rss_mb() -> str:
    try:
        pid = (REPO / ".local" / "lab" / "cp.pid").read_text().strip()
        out = subprocess.run(["ps", "-o", "rss=", "-p", pid], capture_output=True, text=True)
        return f"{int(out.stdout.strip()) / 1024:.0f}"
    except (OSError, ValueError):
        return ""


def run(tag: str, load_rps: float = 10.0) -> int:
    from packages.evals.live.journal import InjectorJournal
    from packages.evals.live.testbed_lab import LabWorld, RealClock
    from packages.evals.live.testbed_runner import Injection, RunParameters

    out = ROOT / tag
    schedule = Schedule.load(json.loads((out / "schedule.json").read_text()))
    os.environ["SRE_LOG_LEVEL"] = "INFO"  # make cp-up passes the environment to the control plane
    os.chdir(REPO)
    world = LabWorld(clock=RealClock(), payment_probe=True)
    journal = InjectorJournal(out / "injector.jsonl")
    _log(f"long run {tag}: schedule {schedule.sha256[:12]}, {len(schedule.faults)} faults")

    def keep_cp_log(index: int) -> None:
        source = REPO / ".local" / "lab" / "cp.log"
        if source.exists():
            shutil.copy(source, out / f"cp{index}.log")

    world.isolate(f"longrun-{tag}")
    database = world.database
    _log(f"database {database}")
    world._start_forwards()
    world.start_load(load_rps)
    started = time.monotonic()
    samples = (out / "samples.csv").open("w", newline="")
    writer = csv.writer(samples)
    writer.writerow(["utc", "elapsed_min", "cp_rss_mb", *SAMPLE_QUERIES])

    def sample(elapsed: float) -> None:
        row = [datetime.now(UTC).isoformat(timespec="seconds"), f"{elapsed / 60:.1f}", _cp_rss_mb()]
        row += [_sql(database, q) for q in SAMPLE_QUERIES.values()]
        writer.writerow(row)
        samples.flush()
        _log(
            "sample " + " ".join(f"{k}={v}" for k, v in zip(SAMPLE_QUERIES, row[3:], strict=False))
        )

    pending = list(schedule.faults)
    active: list[tuple[Injection, float]] = []
    restart_due: float | None = schedule.restart_minutes * 60
    cp_index, next_sample = 1, 0.0
    try:
        while (elapsed := time.monotonic() - started) < schedule.minutes * 60:
            for item in [a for a in active if elapsed >= a[1]]:
                world.remove(item[0], journal)
                _log(f"fault removed: {item[0].kind} {item[0].name}")
                active.remove(item)
            while pending and elapsed >= pending[0].start_seconds:
                fault = pending.pop(0)
                world.target_app = target_of(fault.kind)
                params = RunParameters(
                    seed=schedule.seed,
                    baseline_seconds=0,
                    offset_seconds=0,
                    duration_seconds=fault.duration_seconds,
                    latency_ms=fault.latency_ms,
                    load_rps=load_rps,
                    fault=fault.kind,
                    cpu_workers=fault.cpu_workers or 16,
                )
                injection = world.settle(world.inject(params, journal, fault.name))
                _log(
                    f"fault injected: {fault.name} {fault.kind} -> {injection.kind} {injection.name}"
                )
                active.append((injection, fault.end_seconds))
            if restart_due is not None and elapsed >= restart_due and not active:
                restart_due = None
                keep_cp_log(cp_index)
                cp_index += 1
                _log("control plane restart (same database, connector untouched)")
                world._run(["make", "cp-stop"])
                world._run(["make", "cp-up", f"CP_DB_NAME={database}"], timeout=180)
                time.sleep(20)
            if elapsed >= next_sample:
                sample(elapsed)
                next_sample += SAMPLE_EVERY
            time.sleep(2)
    except Exception:  # noqa: BLE001
        _log("ABORTED:\n" + traceback.format_exc())
        return 1
    finally:
        for injection, _ in active:
            world.remove(injection, journal)
        world.stop_load()
        world.cleanup()
        sample(time.monotonic() - started)
        keep_cp_log(cp_index)
        samples.close()
        _log("LONGRUN_DONE")
    return 0


# ---- the pre-registered measurement (m21 §11, independent check) ---------------------------------


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def fault_intervals(
    journal_lines: Iterable[Mapping[str, Any]],
) -> list[tuple[datetime, datetime, tuple[str, str]]]:
    """``(created, removed, candidate)`` per injected fault, from the injector journal."""
    opened: dict[str, datetime] = {}
    out: list[tuple[datetime, datetime, tuple[str, str]]] = []
    for entry in journal_lines:
        # a rollout's created and removed records differ after the workload's name (env set, env unset)
        key = " ".join(str(entry["object"]).split()[:2])
        if entry.get("role") == "cause_created":
            opened[key] = _instant(entry["at"])
        elif entry.get("role") == "cause_removed" and key in opened:
            kind, name = key.split(" ", 1)
            candidate = {
                "deployment": ("Deployment", name),
                "networkchaos": ("NetworkChaos", name),
                "stresschaos": ("StressChaos", name),
            }[kind]
            out.append((opened.pop(key), _instant(entry["at"]), candidate))
    return out


def truth_at(
    onset: datetime, intervals: Sequence[tuple[datetime, datetime, tuple[str, str]]]
) -> frozenset[tuple[str, str]]:
    """Every fault in effect at the onset (created 1 minute before to 3 minutes after removal)."""
    return frozenset(
        c
        for created, removed, c in intervals
        if created - timedelta(minutes=1) <= onset <= removed + timedelta(minutes=3)
    )


def measure_rows(
    rows: Sequence[tuple[Mapping[str, Any], datetime | None]],
    events: Sequence[Mapping[str, Any]],
    intervals: Sequence[tuple[datetime, datetime, tuple[str, str]]],
    window: timedelta,
) -> Counter[str]:
    """The pre-registered metrics for one window: hard criteria and descriptive counts."""
    from packages.evals.temporal_relevance import shadow_leadership

    m: Counter[str] = Counter()
    for document, onset in rows:
        truth = truth_at(onset, intervals) if onset is not None else frozenset()
        group = "fault" if truth else "no-fault"
        before = document.get("leading_actor_display")
        shadow = shadow_leadership(document, events, window)
        candidates = {
            (c["kind"], c["name"]) for c in document.get("leading_actor_candidates") or []
        }
        m[f"{group}: diagnoses"] += 1
        m[f"{group}: competing"] += before == "COMPETING"
        m[f"{group}: acted"] += shadow.acted
        if shadow.display == "NOT_ESTABLISHED" and before != "NOT_ESTABLISHED":
            m["HARD not_established_added"] += 1
        if before == "COMPETING" and shadow.display == "SINGLE":
            m[f"{group}: competing_to_single"] += 1
            if truth and shadow.eligible[0] not in truth:
                m["HARD single_leader_wrong"] += 1
        in_pool = truth & candidates
        if in_pool & set(shadow.demoted):
            m["HARD true_cause_demoted"] += 1
            if len(truth) > 1:
                m["HARD overlap_cause_demoted"] += 1
        if len(truth) > 1:
            m["overlap: diagnoses"] += 1
            m["overlap: competing"] += before == "COMPETING"
        m["strong_tier"] += document.get("leading_actor_tier") == "STRONG"
    return m


def measure(tag: str, pg: str = "postgresql+psycopg://postgres:postgres@127.0.0.1:55433/{}") -> int:
    from sqlalchemy import create_engine, text

    out = ROOT / tag
    from packages.evals.live.testbed_lab import database_name

    database = database_name(f"longrun-{tag}")
    intervals = fault_intervals(
        json.loads(line)
        for line in (out / "injector.jsonl").read_text().splitlines()
        if line.strip()
    )
    engine = create_engine(pg.format(database))
    try:
        with engine.connect() as connection:
            rows = [
                (dict(r[0]), r[1] if r[1] is None or r[1].tzinfo else r[1].replace(tzinfo=UTC))
                for r in connection.execute(
                    text(
                        "select d.document, (select min(a.starts_at) from alerts a"
                        " where a.incident_id = d.incident_id) from diagnoses d"
                    )
                )
            ]
            events = [
                dict(r[0]) for r in connection.execute(text("select body from event_versions"))
            ]
    finally:
        engine.dispose()
    print(f"{tag}: {len(rows)} diagnoses, {len(intervals)} faults, {len(events)} event versions")
    print("display:", dict(Counter(d.get("leading_actor_display") for d, _ in rows)))
    for minutes in WINDOWS_MINUTES:
        m = measure_rows(rows, events, intervals, timedelta(minutes=minutes))
        hard = {k: v for k, v in m.items() if k.startswith("HARD")}
        print(f"W={minutes:2} min: HARD {hard or 'none'}")
        print(
            "   " + ", ".join(f"{k}={v}" for k, v in sorted(m.items()) if not k.startswith("HARD"))
        )
    return 0


def _write_once(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() == content:
            return
        raise FileExistsError(f"{path} exists and differs; a frozen schedule is never replaced")
    path.write_bytes(content)
    path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="packages.evals.live.longrun")
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("schedule", help="generate and freeze a schedule")
    plan.add_argument("--tag", required=True)
    plan.add_argument("--seed", type=int, required=True)
    plan.add_argument("--minutes", type=float, default=180.0)
    for name in ("run", "measure"):
        commands.add_parser(name).add_argument("--tag", required=True)
    args = parser.parse_args(argv)
    if args.command == "schedule":
        schedule = rhythm_schedule(args.seed, args.minutes)
        problems = schedule_problems(schedule)
        if problems:
            print("refused:", problems)
            return 1
        _write_once(ROOT / args.tag / "schedule.json", schedule.canonical())
        print(f"schedule {schedule.sha256}")
        for fault in schedule.faults:
            print(
                f"  {fault.name:6} {fault.block:13} {fault.kind:13} start {fault.start_seconds / 60:6.1f} min"
                f"  {fault.duration_seconds:6.0f} s"
            )
        for start, end in schedule.quiet:
            print(f"  quiet {start:.1f}-{end:.1f} min")
        return 0
    if args.command == "run":
        return run(args.tag)
    return measure(args.tag)


if __name__ == "__main__":
    sys.exit(main())

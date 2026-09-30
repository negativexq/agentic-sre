"""The real lab behind the runner's ``World`` (docs/architecture/testbed-scenarios-design.md §3, §10).

Isolation per run: a fresh control-plane database (the control plane is restarted on it), a restart of the
connector (new epoch, empty alert buffer) and a quiet lab (no chaos object, no firing alert). Nothing is
truncated. Also holds the command line: ``phase0`` (one unscored validation run kept apart from the suite),
``freeze`` (write the frozen manifest) and ``run`` (the repeats of a frozen suite).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.request
import uuid
from collections.abc import Collection, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import bindparam, create_engine, text

from packages.evals.live.actions import Context, Forward, PortForwarder
from packages.evals.live.ground_truth import (
    ParameterRange,
    ScenarioSpec,
    SuiteManifest,
    TestbedStore,
)
from packages.evals.live.journal import (
    ROLE_CAUSE_CREATED,
    ROLE_CAUSE_REMOVED,
    InjectorJournal,
    parse_instant,
)
from packages.evals.live.oracle import Measurement, http_measurement
from packages.evals.live.runner import WorkloadDriver
from packages.evals.live.scenarios import Target, Workload
from packages.evals.live.testbed_grader import aggregate
from packages.evals.live.testbed_runner import (
    Injection,
    RunOutcome,
    RunParameters,
    StoredDiagnosis,
    run_once,
)

REPO = Path(__file__).resolve().parents[3]
NAMESPACE = "sre-demo"
SCENARIO_ID = "dependency-delay-payment"


class RealClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def database_name(run_id: str) -> str:
    """A safe PostgreSQL database name for one run."""
    return re.sub(r"[^a-z0-9_]", "_", f"testbed_{run_id}".lower())[:60]


_TARGET_SCRIPT = """
import json, time, urllib.request
started = time.monotonic()
try:
    response = urllib.request.urlopen("http://payment-service:8000/health", timeout=4)
    ok, detail = 200 <= response.status < 300, "HTTP %d" % response.status
except Exception as error:
    ok, detail = False, type(error).__name__
print(json.dumps({"ok": ok, "latency": time.monotonic() - started, "detail": detail}))
"""

_DIAGNOSES = text(
    """
    select i.incident_id::text, i.title, i.created_at, d.created_at, d.document
    from incidents i
    join lateral (
        select created_at, document from diagnoses
        where incident_id = i.incident_id order by created_at desc limit 1
    ) d on true
    where i.title = any(:alerts) and i.created_at >= :since
    order by i.created_at
    """
).bindparams(bindparam("alerts", expanding=False))


class LabWorld:
    def __init__(
        self,
        *,
        clock: RealClock | None = None,
        pg_container: str = "agentic-sre-cp-pg",
        pg_port: int = 55433,
        control_plane: str = "http://127.0.0.1:8080",
        order_port: int = 18000,
        alertmanager_port: int = 19093,
    ) -> None:
        self.clock = clock or RealClock()
        self.pg_container, self.pg_port = pg_container, pg_port
        self.control_plane = control_plane
        self.order_port, self.alertmanager_port = order_port, alertmanager_port
        self.database = ""
        self._forwarder: PortForwarder | None = None
        self._driver: WorkloadDriver | None = None

    # ---- plumbing ------------------------------------------------------------------------------

    def _run(
        self,
        args: Sequence[str],
        *,
        stdin: str | None = None,
        timeout: float = 120,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            list(args),
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=REPO,
            check=False,
        )
        if check and result.returncode != 0:
            raise RuntimeError(f"{' '.join(args)[:120]} failed: {result.stderr.strip()[:300]}")
        return result

    def _get_json(self, url: str, timeout: float = 5) -> Any:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read())

    def _wait(self, what: str, condition: Any, seconds: float, step: float = 2.0) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                if condition():
                    return
            except (OSError, ValueError, KeyError, RuntimeError):
                pass
            time.sleep(step)
        raise RuntimeError(f"timed out waiting for {what}")

    def _alerts(self) -> list[dict[str, Any]]:
        data = self._get_json(f"http://localhost:{self.alertmanager_port}/api/v2/alerts")
        return [a for a in data if a.get("status", {}).get("state") == "active"]

    # ---- World ---------------------------------------------------------------------------------

    def isolate(self, run_id: str) -> None:
        self.database = database_name(run_id)
        self._run(["make", "cp-stop"])
        self._run(["make", "cp-up", f"CP_DB_NAME={self.database}"], timeout=180)
        self._wait(
            "the control plane",
            lambda: self._get_json(f"{self.control_plane}/health")["status"] == "ok",
            60,
        )
        self._run(["kubectl", "-n", "connector", "rollout", "restart", "deployment/connector"])
        self._run(
            [
                "kubectl",
                "-n",
                "connector",
                "rollout",
                "status",
                "deployment/connector",
                "--timeout=180s",
            ],
            timeout=200,
        )

        def connected() -> bool:
            rows = self._get_json(f"{self.control_plane}/api/v1/console/system")["connectors"]
            return any(c["name"] == "Connector" and c["status"] == "connected" for c in rows)

        self._wait("the connector to attach", connected, 90)
        self._forwarder = PortForwarder(
            (
                Forward("order-service", self.order_port, 8000, NAMESPACE),
                Forward("alertmanager", self.alertmanager_port, 9093, "observability"),
            ),
            timeout_seconds=30,
        )
        unreachable = self._forwarder.start_all()
        if unreachable:
            raise RuntimeError(f"port-forward failed for {', '.join(unreachable)}")
        self._run(
            ["kubectl", "-n", NAMESPACE, "delete", "networkchaos", "--all", "--ignore-not-found"]
        )
        self._wait("the alerts to clear", lambda: not self._alerts(), 300, step=5.0)

    def start_load(self, rps: float) -> None:
        workload = Workload(
            target=Target.ORDERS,
            count=1,
            concurrency=2,
            interval_seconds=max(0.05, 2.0 / rps - 0.05),
        )
        self._driver = WorkloadDriver(
            workload, Context(order_url=f"http://localhost:{self.order_port}")
        )
        self._driver.start()

    def stop_load(self) -> None:
        if self._driver is not None:
            self._driver.stop()
            self._driver = None

    def target_measure(self) -> Measurement:
        at = self.clock.now()
        try:
            result = self._run(
                [
                    "kubectl",
                    "-n",
                    NAMESPACE,
                    "exec",
                    "deploy/order-service",
                    "--",
                    "python",
                    "-c",
                    _TARGET_SCRIPT,
                ],
                timeout=10,
                check=False,
            )
            parsed = json.loads(result.stdout.strip().splitlines()[-1])
            return Measurement(
                at, bool(parsed["ok"]), float(parsed["latency"]), str(parsed["detail"])
            )
        except (subprocess.TimeoutExpired, ValueError, IndexError, KeyError):
            return Measurement(at, False, None, "exec failed")

    def client_measure(self) -> Measurement:
        body = json.dumps(
            {
                "customer_id": f"oracle-{uuid.uuid4().hex[:8]}",
                "amount_cents": 4200,
                "currency": "USD",
            }
        ).encode()
        return http_measurement(
            f"http://localhost:{self.order_port}/orders",
            method="POST",
            body=body,
            timeout_seconds=3.0,
            clock=self.clock.now,
        )

    def inject(self, params: RunParameters, journal: InjectorJournal, name: str) -> Injection:
        manifest = f"""apiVersion: chaos-mesh.org/v1alpha1
kind: NetworkChaos
metadata: {{name: {name}, namespace: {NAMESPACE}}}
spec:
  action: delay
  mode: all
  selector: {{namespaces: [{NAMESPACE}], labelSelectors: {{app: payment-service}}}}
  delay: {{latency: {params.latency_ms}ms}}
  duration: {int(params.duration_seconds) + 60}s
"""
        result = self._run(["kubectl", "apply", "-f", "-"], stdin=manifest, check=False)
        ok = result.returncode == 0
        uid = ""
        if ok:
            uid = self._run(
                [
                    "kubectl",
                    "-n",
                    NAMESPACE,
                    "get",
                    "networkchaos",
                    name,
                    "-o",
                    "jsonpath={.metadata.uid}",
                ]
            ).stdout.strip()
        entry = journal.record(
            verb="apply",
            object=f"networkchaos {name}",
            ok=ok,
            response=result.stdout if ok else result.stderr,
            uid=uid or None,
            role=ROLE_CAUSE_CREATED,
        )
        if not ok:
            raise RuntimeError(f"the experiment was not created: {result.stderr.strip()[:200]}")
        pods = json.loads(
            self._run(
                [
                    "kubectl",
                    "-n",
                    NAMESPACE,
                    "get",
                    "pod",
                    "-l",
                    "app=payment-service",
                    "-o",
                    "json",
                ]
            ).stdout
        )["items"]
        pod = pods[0]["metadata"]
        return Injection(name, uid, pod["name"], pod["uid"], entry.at)

    def applied_at(self, injection: Injection) -> datetime | None:
        selector = f"involvedObject.name={injection.name},reason=Applied"
        items = json.loads(
            self._run(
                [
                    "kubectl",
                    "-n",
                    NAMESPACE,
                    "get",
                    "events",
                    "--field-selector",
                    selector,
                    "-o",
                    "json",
                ]
            ).stdout
        )["items"]
        times = [
            t
            for item in items
            if (
                t := parse_instant(
                    item.get("eventTime") or item.get("firstTimestamp") or item.get("lastTimestamp")
                )
            )
        ]
        return min(times) if times else None

    def remove(self, injection: Injection, journal: InjectorJournal) -> None:
        result = self._run(
            [
                "kubectl",
                "-n",
                NAMESPACE,
                "delete",
                "networkchaos",
                injection.name,
                "--ignore-not-found",
            ],
            check=False,
        )
        journal.record(
            verb="delete",
            object=f"networkchaos {injection.name}",
            ok=result.returncode == 0,
            response=result.stdout or result.stderr,
            role=ROLE_CAUSE_REMOVED,
        )

    def alert_started_at(self, alerts: Collection[str], since: datetime) -> datetime | None:
        starts = [
            t
            for alert in self._alerts()
            if alert["labels"].get("alertname") in alerts
            and (t := parse_instant(alert.get("startsAt"))) is not None
            and t >= since - timedelta(seconds=1)
        ]
        return min(starts) if starts else None

    def diagnoses(self, alerts: Collection[str], since: datetime) -> list[StoredDiagnosis]:
        engine = create_engine(
            f"postgresql+psycopg://postgres:postgres@127.0.0.1:{self.pg_port}/{self.database}"
        )
        try:
            with engine.connect() as connection:
                rows = connection.execute(
                    _DIAGNOSES, {"alerts": sorted(alerts), "since": since}
                ).all()
        finally:
            engine.dispose()
        return [
            StoredDiagnosis(str(r[0]), str(r[1]), _aware(r[2]), _aware(r[3]), dict(r[4]))
            for r in rows
        ]

    def cleanup(self) -> None:
        self._run(
            ["kubectl", "-n", NAMESPACE, "delete", "networkchaos", "--all", "--ignore-not-found"],
            check=False,
        )
        if self._forwarder is not None:
            self._forwarder.stop_all()
            self._forwarder = None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# ---- suites and command line -------------------------------------------------------------------


def dependency_spec(repeats: int, seeds: tuple[int, ...], tier: str = "DEV") -> ScenarioSpec:
    """Slice 1 (design §10): a delay on ``payment-service`` under load, parameters seeded per repeat."""
    return ScenarioSpec(
        scenario_id=SCENARIO_ID,
        family="dependency-fault",
        tier=tier,  # type: ignore[arg-type]
        repeats=repeats,
        seeds=seeds,
        parameters={
            "baseline_seconds": ParameterRange(low=45, high=45),
            "offset_seconds": ParameterRange(low=0, high=20),
            "duration_seconds": ParameterRange(low=90, high=110),
            "latency_ms": ParameterRange(low=300, high=600),
            "load_rps": ParameterRange(low=8, high=12),
        },
    )


def _summarize(outcome: RunOutcome) -> str:
    record, timeline = outcome.record, outcome.record.timeline
    lines = [
        f"run {record.scenario_id}#{record.repeat} seed={record.seed}: {'VALID' if record.valid else 'INVALID'}"
    ]
    for reason in record.invalid_reasons:
        lines.append(f"  invalid: {reason}")
    base = timeline.cause_created_at
    for name in (
        "cause_created_at",
        "execution_started_at",
        "target_effect_at",
        "propagation_started_at",
        "symptom_started_at",
        "alert_fired_at",
        "recovery_at",
    ):
        stamp = timeline.stamp(name)
        offset = f"+{(stamp.at - base.at).total_seconds():6.1f}s" if stamp and base else "  -    "
        lines.append(f"  {name:24s} {offset}  {stamp.source.value if stamp else 'MISSING'}")
    if outcome.score is not None:
        lines.append("  score: " + outcome.score.model_dump_json())
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="packages.evals.live.testbed_lab")
    parser.add_argument("--root", type=Path, default=REPO / ".local/testbed")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("phase0", help="one unscored validation run, stored apart from the suite")
    freeze = commands.add_parser("freeze", help="write the frozen manifest of slice 1")
    freeze.add_argument("--suite", required=True)
    freeze.add_argument("--salt", required=True)
    freeze.add_argument("--seeds", required=True, help="comma separated, one per repeat")
    freeze.add_argument("--engine-version", required=True)
    run = commands.add_parser("run", help="run the missing repeats of a frozen suite")
    run.add_argument("--suite", required=True)
    args = parser.parse_args(argv)
    clock = RealClock()

    if args.command == "freeze":
        seeds = tuple(int(s) for s in args.seeds.split(","))
        manifest = SuiteManifest(
            suite_id=args.suite,
            engine_version=args.engine_version,
            created_at=clock.now(),
            salt=args.salt,
            scenarios=(dependency_spec(len(seeds), seeds),),
            acceptance={"false_resolved": 0, "false_strong_authority": 0},
        ).frozen()
        print(TestbedStore(args.root).write_manifest(manifest), manifest.sha256)
        return 0

    world = LabWorld(clock=clock)
    if args.command == "phase0":
        # Phase 0 is not part of any suite: its own store, its own manifest, never scored into a result.
        from packages.rca.engine import RCA_ENGINE_VERSION

        suite = "phase0-" + clock.now().strftime("%Y%m%dT%H%M%S")
        store = TestbedStore(args.root / "phase0")
        manifest = SuiteManifest(
            suite_id=suite,
            engine_version=RCA_ENGINE_VERSION,
            created_at=clock.now(),
            salt="phase0",
            scenarios=(dependency_spec(1, (1,)),),
            acceptance={},
        ).frozen()
        store.write_manifest(manifest)
        outcome = run_once(
            manifest, SCENARIO_ID, 0, world, store, work_root=args.root / "work", clock=clock
        )
        print(_summarize(outcome))
        return 0 if outcome.record.valid else 1

    store = TestbedStore(args.root)
    manifest = store.load_manifest(args.suite)
    outcomes: list[RunOutcome] = []
    for spec in manifest.scenarios:
        for repeat in range(spec.repeats):
            if store.run_dir(manifest.suite_id, spec.scenario_id, repeat).exists():
                continue
            outcome = run_once(
                manifest,
                spec.scenario_id,
                repeat,
                world,
                store,
                work_root=args.root / "work",
                clock=clock,
            )
            print(_summarize(outcome), flush=True)
            outcomes.append(outcome)
    scores = [o.score for o in outcomes if o.score is not None]
    print(json.dumps(aggregate(scores), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""The real lab behind the runner's ``World`` (docs/architecture/testbed-scenarios-design.md §3, §10).

Isolation per run: a fresh control-plane database (the control plane is restarted on it), a restart of the
connector (new epoch, empty alert buffer) and a quiet lab (no chaos object, no firing alert). Nothing is
truncated. Also holds the command line: ``phase0`` (one unscored validation run kept apart from the suite),
``freeze`` (write the frozen manifest) and ``run`` (the repeats of a frozen suite).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import subprocess
import sys
import time
import urllib.request
import uuid
from collections.abc import Callable, Collection, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import bindparam, create_engine, text

from packages.evals.live.actions import Context, Forward, PortForwarder
from packages.evals.live.ground_truth import (
    ORDER,
    ParameterRange,
    RunRecord,
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
from packages.evals.live.oracle import Measurement, SeriesWriter, http_measurement
from packages.evals.live.runner import WorkloadDriver
from packages.evals.live.scenarios import Target, Workload
from packages.evals.live.testbed_grader import RunScore, aggregate, score_run
from packages.evals.live.testbed_runner import (
    BaselineNotQuiet,
    Injection,
    RunOutcome,
    RunParameters,
    StoredDiagnosis,
    rederive,
    run_once,
)
from packages.rca.model import Diagnosis

REPO = Path(__file__).resolve().parents[3]
NAMESPACE = "sre-demo"
SCENARIO_ID = "dependency-delay-payment"
CONFIG_SCENARIO_ID = "config-delay-payment"
DIRECT_SCENARIO_ID = "direct-stress-order"
SCHEDULED_SCENARIO_ID = "scheduled-delay-payment"
COMPETING_SCENARIO_ID = "competing-delay-podkill"
NEGATIVE_SCENARIO_ID = "negative-config-decoy"
MAX_WARMUPS = 3
RUN_LABEL = (
    "testbed.agentic-sre.io/run"  # every experiment the harness creates carries its run's id
)
CHAOS_RESOURCES = ("networkchaos", "stresschaos", "podchaos", "iochaos", "httpchaos", "schedules")
WARMUP_SECONDS = 90.0  # a restarted pod fails probes for its first minutes under load
WATCHED_NAMESPACES = ("sre-demo", "lab-control", "chaos-mesh")
DECOY_NAMESPACE = "lab-control"  # the isolated workload of the negative control (contract §16)
ROLE_DECOY_CREATED, ROLE_DECOY_REMOVED = (
    "decoy_created",
    "decoy_removed",
)  # never the cause's instants
# Chaos Mesh's per-pod records of how its experiments are applied (owned by the pod, named after it): never an
# experiment of their own, so a foreign fault shows through its experiment's object and events instead.
CHAOS_POD_RECORDS = frozenset({"PodNetworkChaos", "PodIOChaos", "PodHttpChaos"})


def foreign_fault_event(kind: str, name: str, experiments: Collection[str]) -> bool:
    """An event about a fault object that this run did not create.

    An experiment spawned by one of the run's Schedules is the run's own: Chaos Mesh names it after the
    Schedule (``<schedule>-<suffix>``), and the Schedule's name is unique to the run.
    """
    if kind in CHAOS_POD_RECORDS:
        return False
    if not (kind.endswith("Chaos") or kind == "Schedule"):
        return False
    return name not in experiments and not any(name.startswith(f"{e}-") for e in experiments)


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

# The target probe of a fault inside the payment handler (the delay of `config-or-rollout` is applied in
# `POST /payments` only, so `/health` would never see it).
_PAYMENT_SCRIPT = """
import json, time, urllib.request, uuid
started = time.monotonic()
body = json.dumps({"order_id": str(uuid.uuid4()), "amount_cents": 4200, "currency": "USD"}).encode()
request = urllib.request.Request(
    "http://payment-service:8000/payments", data=body, headers={"Content-Type": "application/json"}
)
try:
    response = urllib.request.urlopen(request, timeout=6)
    ok, detail = 200 <= response.status < 300, "HTTP %d" % response.status
except Exception as error:
    ok, detail = False, type(error).__name__
print(json.dumps({"ok": ok, "latency": time.monotonic() - started, "detail": detail}))
"""
DELAY_VARIABLE = "FAULT_PAYMENT_DELAY_MS"

_DIAGNOSES = text(
    """
    select i.incident_id::text, i.title, i.created_at, d.created_at, d.document,
        (select min(created_at) from diagnoses where incident_id = i.incident_id)
    from incidents i
    join lateral (
        select created_at, document from diagnoses
        where incident_id = i.incident_id order by created_at desc limit 1
    ) d on true
    where i.title = any(:alerts) and i.created_at >= :since
      -- an alert that began before the run (Alertmanager resends a group's recently resolved alerts
      -- with the next notification) is not the run's, however late it reaches a fresh database
      and not exists (select 1 from alerts a where a.incident_id = i.incident_id and a.starts_at < :since)
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
        target_app: str = "payment-service",
        payment_probe: bool = False,
    ) -> None:
        self.target_app = (
            target_app  # the workload the fault lands on; its pod is replaced for every run
        )
        self.target_script = _PAYMENT_SCRIPT if payment_probe else _TARGET_SCRIPT
        self._rollout: tuple[datetime, str, str, str, str] | None = None
        self._last_delay = ""
        self._original_image = ""
        self._original_strategy: dict[str, Any] = {}
        self._broken_image = ""
        self._experiments: set[str] = set()  # the experiments this run created
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
        """Quiet the lab, then give the run a fresh control plane and connector (design §3).

        Order matters: Kubernetes keeps events for about an hour, so an earlier run's chaos events would
        reach the new run through the connector's first listing and be blamed for it. They are removed
        *before* the connector restarts, and the connector restarts before the control plane starts on its empty database.
        """
        self.database = database_name(run_id)
        self._experiments.clear()
        self._delete_experiments()
        self._unset_delay()  # a run that died mid-way may have left the change in place
        self._restore_image()
        self._quiet_target_pod()
        for namespace in WATCHED_NAMESPACES:
            self._run(
                ["kubectl", "-n", namespace, "delete", "events", "--all", "--ignore-not-found"]
            )
        self._wait("the alerts to clear", lambda: not self._alerts(), 300, step=5.0)

        # The connector goes first, while no control plane is listening: a connector that outlives the previous
        # run still holds its buffers (events, changes) and would hand them to the new database on attaching.
        self._run(["make", "cp-stop"])
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
        self._run(["make", "cp-up", f"CP_DB_NAME={self.database}"], timeout=180)
        self._wait(
            "the control plane",
            lambda: self._get_json(f"{self.control_plane}/health")["status"] == "ok",
            60,
        )

        def connected() -> bool:
            rows = self._get_json(f"{self.control_plane}/api/v1/console/system")["connectors"]
            return any(c["name"] == "Connector" and c["status"] == "connected" for c in rows)

        self._wait("the connector to attach", connected, 90)

    def _unset_delay(self) -> subprocess.CompletedProcess[str]:
        return self._run(
            [
                "kubectl",
                "-n",
                NAMESPACE,
                "set",
                "env",
                "deployment/payment-service",
                f"{DELAY_VARIABLE}-",
            ],
            check=False,
        )

    def _delete_experiments(self) -> None:
        for kind in (
            "schedules",
            "networkchaos",
            "stresschaos",
            "podchaos",
        ):  # a Schedule first, so it spawns no more
            for namespace in (NAMESPACE, DECOY_NAMESPACE):
                self._run(
                    ["kubectl", "-n", namespace, "delete", kind, "--all", "--ignore-not-found"],
                    check=False,
                )

    def construction_problems(self) -> list[str]:
        """Contract §16.3: the decoy's workload is isolated and nothing in sre-demo is configured to reach it."""
        problems: list[str] = []
        policies = json.loads(
            self._run(
                ["kubectl", "-n", DECOY_NAMESPACE, "get", "networkpolicy", "-o", "json"]
            ).stdout
        )["items"]
        if not any(p["metadata"]["name"] == "default-deny" for p in policies):
            problems.append(f"no default-deny NetworkPolicy in {DECOY_NAMESPACE}")
        reached = self._run(
            [
                "kubectl",
                "-n",
                NAMESPACE,
                "exec",
                "deployment/order-service",
                "--",
                "python",
                "-c",
                "import urllib.request; urllib.request.urlopen("
                f"'http://isolated-echo.{DECOY_NAMESPACE}:8080', timeout=3)",
            ],
            check=False,
        )
        if reached.returncode == 0:
            problems.append(f"the isolated workload is reachable from {NAMESPACE}")
        for resource in ("deployments", "configmaps"):
            items = json.loads(
                self._run(["kubectl", "-n", NAMESPACE, "get", resource, "-o", "json"]).stdout
            )["items"]
            for item in items:
                text = json.dumps(item.get("spec", {}).get("template", {}) or item.get("data", {}))
                if DECOY_NAMESPACE in text or "isolated-echo" in text:
                    problems.append(
                        f"{resource}/{item['metadata']['name']} refers to {DECOY_NAMESPACE}"
                    )
        return problems

    def _fresh_target_pod(self) -> None:
        """A new pod of the target workload per run, so it carries no event history of an earlier run.

        The kubelet re-creates a deleted event with its cached first timestamp and count, so an old
        pod's recurring events would look as if they began before this run's injection.
        """
        deployment = f"deployment/{self.target_app}"
        self._run(["kubectl", "-n", NAMESPACE, "rollout", "restart", deployment])
        self._run(
            ["kubectl", "-n", NAMESPACE, "rollout", "status", deployment, "--timeout=180s"],
            timeout=200,
        )

        def single_pod() -> bool:
            items = json.loads(
                self._run(
                    [
                        "kubectl",
                        "-n",
                        NAMESPACE,
                        "get",
                        "pod",
                        "-l",
                        f"app={self.target_app}",
                        "-o",
                        "json",
                    ]
                ).stdout
            )["items"]
            return len(items) == 1 and all("deletionTimestamp" not in i["metadata"] for i in items)

        self._wait(f"the old {self.target_app} pod to terminate", single_pod, 120)

    def _start_forwards(self) -> None:
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

    def target_warnings(self) -> list[str]:
        """Warning events of the current target pod (a terminated pod of the same workload is another pod)."""
        pods = json.loads(
            self._run(
                [
                    "kubectl",
                    "-n",
                    NAMESPACE,
                    "get",
                    "pod",
                    "-l",
                    f"app={self.target_app}",
                    "-o",
                    "json",
                ]
            ).stdout
        )["items"]
        names = {p["metadata"]["name"] for p in pods}
        events = json.loads(
            self._run(["kubectl", "-n", NAMESPACE, "get", "events", "-o", "json"]).stdout
        )["items"]
        return [
            f"{e['involvedObject']['name']}: {e.get('reason')}"
            for e in events
            if e.get("type") == "Warning" and e["involvedObject"].get("name") in names
        ]

    def foreign_faults(self) -> list[str]:
        found: list[str] = []
        for namespace in WATCHED_NAMESPACES:
            for resource in CHAOS_RESOURCES:
                result = self._run(
                    ["kubectl", "-n", namespace, "get", resource, "-o", "json"], check=False
                )
                if result.returncode != 0:
                    continue  # a kind the lab does not serve
                for item in json.loads(result.stdout)["items"]:
                    labels = item["metadata"].get("labels") or {}
                    owners = item["metadata"].get("ownerReferences") or []
                    spawned = any(
                        o.get("kind") == "Schedule" and o.get("name") in self._experiments
                        for o in owners
                    )
                    if labels.get(RUN_LABEL) != self.database and not spawned:
                        found.append(f"{namespace}/{item['kind']}/{item['metadata']['name']}")
            events = json.loads(
                self._run(["kubectl", "-n", namespace, "get", "events", "-o", "json"]).stdout
            )["items"]
            for event in events:
                involved = event.get("involvedObject", {})
                kind, name = involved.get("kind", ""), involved.get("name", "")
                if foreign_fault_event(kind, name, self._experiments):
                    found.append(f"{namespace}/{kind}/{name} (event {event.get('reason')})")
        return sorted(set(found))

    def _quiet_target_pod(self) -> None:
        """A fresh target pod that raised no warning while it warmed up, or the run does not start.

        The kubelet re-creates a deleted event with its cached first timestamp and count, so any failure
        of the pod before the injection would come back as an observation that may precede it. A pod
        that failed while warming up is replaced and warmed up again.
        """
        for _ in range(MAX_WARMUPS):
            self._fresh_target_pod()  # before the port-forwards: replacing order-service would cut its own
            self._start_forwards()
            self._warm_up()
            if not self.target_warnings():
                return
            self._forwarder.stop_all()  # type: ignore[union-attr]
        raise RuntimeError(
            f"the {self.target_app} pod kept raising warnings while warming up ({MAX_WARMUPS} attempts)"
        )

    def _warm_up(self) -> None:
        """Run the workload against the fresh pod, then forget it happened.

        The pod's startup failures (probe timeouts under first load) belong to no run: the events they
        produce are deleted straight after, before the connector or the control plane sees them.
        """
        self.start_load(10.0)
        try:
            time.sleep(WARMUP_SECONDS)
        finally:
            self.stop_load()

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
                    self.target_script,
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
        if params.fault == "env-delay":
            return self._inject_env_delay(params, journal)
        if params.fault == "image-break":
            return self._inject_image_break(params, journal)
        duration = f"  duration: {int(params.duration_seconds) + 60}s\n"
        spawn_kind = "NetworkChaos"
        if params.fault == "scheduled-delay":
            # design §4 `scheduled-recurring`, variant A: a recurring delay on payment-service
            kind, duration, body = (
                "Schedule",
                "",
                (
                    f'  schedule: "@every {params.spawn_every_seconds}s"\n'
                    "  type: NetworkChaos\n  historyLimit: 10\n"
                    "  concurrencyPolicy: Forbid\n  networkChaos:\n"
                    "    action: delay\n    mode: all\n"
                    f"    selector: {{namespaces: [{NAMESPACE}], labelSelectors: {{app: payment-service}}}}\n"
                    f"    delay: {{latency: {params.latency_ms}ms}}\n"
                    f"    duration: {params.spawn_seconds}s\n"
                ),
            )
        elif params.fault == "scheduled-stress":
            # design §12.2.2, `scheduled-recurring` variant B: recurring CPU stress on order-service
            spawn_kind = "StressChaos"
            kind, duration, body = (
                "Schedule",
                "",
                (
                    f'  schedule: "@every {params.spawn_every_seconds}s"\n'
                    "  type: StressChaos\n  historyLimit: 10\n"
                    "  concurrencyPolicy: Forbid\n  stressChaos:\n    mode: all\n"
                    f"    selector: {{namespaces: [{NAMESPACE}], labelSelectors: {{app: order-service}}}}\n"
                    f"    stressors: {{cpu: {{workers: {params.cpu_workers}, load: 100}}}}\n"
                    f"    duration: {params.spawn_seconds}s\n"
                ),
            )
        elif params.fault in ("network-loss", "competing-loss"):
            # design §12.2.2: packet loss instead of delay on payment-service
            kind, body = (
                "NetworkChaos",
                (
                    "  action: loss\n  mode: all\n"
                    f"  selector: {{namespaces: [{NAMESPACE}], labelSelectors: {{app: payment-service}}}}\n"
                    f'  loss: {{loss: "{params.loss_percent}"}}\n'
                ),
            )
        elif params.fault == "decoy-stress":
            # design §12.2.2, `negative-control` variant B: a CPU-stress decoy on the isolated workload
            kind, body = (
                "StressChaos",
                (
                    "  mode: all\n"
                    f"  selector: {{namespaces: [{DECOY_NAMESPACE}], labelSelectors: {{app: isolated-echo}}}}\n"
                    f"  stressors: {{cpu: {{workers: {params.cpu_workers}, load: 100}}}}\n"
                ),
            )
        elif params.fault == "decoy-delay":
            # contract §16: a decoy on the isolated workload, at about the same time as the real cause
            kind, body = (
                "NetworkChaos",
                (
                    "  action: delay\n  mode: all\n"
                    f"  selector: {{namespaces: [{DECOY_NAMESPACE}], labelSelectors: {{app: isolated-echo}}}}\n"
                    f"  delay: {{latency: {params.latency_ms}ms}}\n"
                ),
            )
        elif params.fault == "pod-kill":
            # contract §15: the second competing cause, one pod of order-worker killed once
            kind, duration, body = (
                "PodChaos",
                "",
                (
                    "  action: pod-kill\n  mode: one\n"
                    f"  selector: {{namespaces: [{NAMESPACE}], labelSelectors: {{app: order-worker}}}}\n"
                ),
            )
        elif params.fault == "cpu-stress":
            kind, body = (
                "StressChaos",
                (
                    "  mode: all\n"
                    f"  selector: {{namespaces: [{NAMESPACE}], labelSelectors: {{app: {self.target_app}}}}}\n"
                    f"  stressors: {{cpu: {{workers: {params.cpu_workers}, load: 100}}}}\n"
                ),
            )
        else:
            kind, body = (
                "NetworkChaos",
                (
                    "  action: delay\n  mode: all\n"
                    f"  selector: {{namespaces: [{NAMESPACE}], labelSelectors: {{app: payment-service}}}}\n"
                    f"  delay: {{latency: {params.latency_ms}ms}}\n"
                ),
            )
        decoy = params.fault in ("decoy-delay", "decoy-stress")
        namespace = DECOY_NAMESPACE if decoy else NAMESPACE
        manifest = (
            f"apiVersion: chaos-mesh.org/v1alpha1\nkind: {kind}\n"
            f"metadata: {{name: {name}, namespace: {namespace}, "
            f"labels: {{{RUN_LABEL}: {self.database}}}}}\nspec:\n{body}{duration}"
        )
        self._experiments.add(name)
        # the pod a pod-kill will remove is known only before it is killed
        app = {
            "pod-kill": "order-worker",
            "decoy-delay": "isolated-echo",
            "decoy-stress": "isolated-echo",
        }.get(params.fault, self.target_app)
        before = json.loads(
            self._run(
                ["kubectl", "-n", namespace, "get", "pod", "-l", f"app={app}", "-o", "json"]
            ).stdout
        )["items"]
        result = self._run(["kubectl", "apply", "-f", "-"], stdin=manifest, check=False)
        ok = result.returncode == 0
        uid = ""
        if ok:
            uid = self._run(
                [
                    "kubectl",
                    "-n",
                    namespace,
                    "get",
                    kind.lower(),
                    name,
                    "-o",
                    "jsonpath={.metadata.uid}",
                ]
            ).stdout.strip()
        entry = journal.record(
            verb="apply",
            object=f"{kind.lower()} {name}",
            ok=ok,
            response=result.stdout if ok else result.stderr,
            uid=uid or None,
            role=ROLE_DECOY_CREATED if decoy else ROLE_CAUSE_CREATED,
        )
        if not ok:
            raise RuntimeError(f"the experiment was not created: {result.stderr.strip()[:200]}")
        pods = (
            before
            if params.fault == "pod-kill"
            else json.loads(
                self._run(
                    ["kubectl", "-n", namespace, "get", "pod", "-l", f"app={app}", "-o", "json"]
                ).stdout
            )["items"]
        )
        pod = pods[0]["metadata"]
        return Injection(
            name,
            uid,
            pod["name"],
            pod["uid"],
            entry.at,
            kind,
            namespace=namespace,
            spawn_kind=spawn_kind,
        )

    def _inject_env_delay(self, params: RunParameters, journal: InjectorJournal) -> Injection:
        """Change the Deployment's environment; the rollout it starts is the execution."""
        before = json.loads(
            self._run(
                ["kubectl", "-n", NAMESPACE, "get", "deployment", "payment-service", "-o", "json"]
            ).stdout
        )["metadata"]
        result = self._run(
            [
                "kubectl",
                "-n",
                NAMESPACE,
                "set",
                "env",
                "deployment/payment-service",
                f"{DELAY_VARIABLE}={params.latency_ms}",
            ],
            check=False,
        )
        ok = result.returncode == 0
        entry = journal.record(
            verb="patch",
            object=f"deployment payment-service env {DELAY_VARIABLE}={params.latency_ms}",
            ok=ok,
            response=result.stdout if ok else result.stderr,
            uid=before["uid"],
            role=ROLE_CAUSE_CREATED,
        )
        if not ok:
            raise RuntimeError(f"the change was not applied: {result.stderr.strip()[:200]}")
        self._rollout, self._last_delay = None, str(params.latency_ms)
        return Injection("payment-service", before["uid"], "", "", entry.at, "Deployment")

    def _inject_image_break(self, params: RunParameters, journal: InjectorJournal) -> Injection:
        """Design §12.2.2, `config-or-rollout` variant B: an image that does not exist, in one patch with a
        rollout strategy that takes the old pod away first (design §12.6: with a pod delete alone the old
        ReplicaSet brought its pod straight back, and the outage lasted about 20 s)."""
        before = json.loads(
            self._run(
                ["kubectl", "-n", NAMESPACE, "get", "deployment", "payment-service", "-o", "json"]
            ).stdout
        )
        self._original_image = before["spec"]["template"]["spec"]["containers"][0]["image"]
        self._original_strategy = before["spec"]["strategy"]
        broken = f"{self._original_image.rsplit(':', 1)[0]}:missing-{params.seed}"
        result = self._patch_payment(
            broken, {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1}}
        )
        ok = result.returncode == 0
        entry = journal.record(
            verb="patch",
            object=f"deployment payment-service image {broken}",
            ok=ok,
            response=result.stdout if ok else result.stderr,
            uid=before["metadata"]["uid"],
            role=ROLE_CAUSE_CREATED,
        )
        if not ok:
            raise RuntimeError(f"the image was not changed: {result.stderr.strip()[:200]}")
        self._rollout, self._broken_image = None, broken
        return Injection(
            "payment-service", before["metadata"]["uid"], "", "", entry.at, "Deployment"
        )

    def _patch_payment(
        self, image: str, strategy: dict[str, Any]
    ) -> subprocess.CompletedProcess[str]:
        body = {
            "spec": {
                "strategy": strategy,
                "template": {"spec": {"containers": [{"name": "payment-service", "image": image}]}},
            }
        }
        return self._run(
            [
                "kubectl",
                "-n",
                NAMESPACE,
                "patch",
                "deployment/payment-service",
                "--type=strategic",
                "-p",
                json.dumps(body),
            ],
            check=False,
        )

    def _restore_image(self) -> subprocess.CompletedProcess[str] | None:
        if not self._broken_image or not self._original_image:
            return None
        result = self._patch_payment(self._original_image, self._original_strategy)
        self._broken_image = ""
        return result

    def _new_rollout(
        self, value: int | str, image: str = ""
    ) -> tuple[datetime, str, str, str, str] | None:
        """(first pod created, ReplicaSet name, ReplicaSet uid, pod name, pod uid) of the rollout whose
        template carries the delay (or the broken ``image``), once its first pod exists."""
        sets = json.loads(
            self._run(
                ["kubectl", "-n", NAMESPACE, "get", "rs", "-l", "app=payment-service", "-o", "json"]
            ).stdout
        )["items"]
        wanted = {"name": DELAY_VARIABLE, "value": str(value)}
        matches = [
            rs
            for rs in sets
            if (
                rs["spec"]["template"]["spec"]["containers"][0].get("image") == image
                if image
                else wanted in rs["spec"]["template"]["spec"]["containers"][0].get("env", [])
            )
        ]
        if not matches:
            return None
        rs = max(matches, key=lambda r: r["metadata"]["creationTimestamp"])["metadata"]
        pods = [
            p["metadata"]
            for p in json.loads(
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
            if any(o.get("uid") == rs["uid"] for o in p["metadata"].get("ownerReferences", []))
        ]
        if not pods:
            return None
        first = min(pods, key=lambda p: p["creationTimestamp"])
        created = parse_instant(first["creationTimestamp"])
        if created is None:
            return None
        return created, rs["name"], rs["uid"], first["name"], first["uid"]

    def _spawned(self, injection: Injection) -> list[tuple[str, str]]:
        """(name, uid) of the experiments a Schedule of this run has spawned so far, oldest first."""
        items = json.loads(
            self._run(
                ["kubectl", "-n", NAMESPACE, "get", injection.spawn_kind.lower(), "-o", "json"]
            ).stdout
        )["items"]
        owned = [
            i["metadata"]
            for i in items
            if any(
                o.get("uid") == injection.uid for o in i["metadata"].get("ownerReferences") or []
            )
        ]
        owned.sort(key=lambda m: m["creationTimestamp"])
        return [(m["name"], m["uid"]) for m in owned]

    def settle(self, injection: Injection) -> Injection:
        if injection.kind == "Schedule":
            return dataclasses.replace(injection, spawned=tuple(self._spawned(injection)))
        if injection.kind != "Deployment" or self._rollout is None:
            return injection
        _, rs_name, rs_uid, pod_name, pod_uid = self._rollout
        return dataclasses.replace(
            injection,
            execution_name=rs_name,
            execution_uid=rs_uid,
            target_pod=pod_name,
            target_pod_uid=pod_uid,
        )

    def applied_at(self, injection: Injection) -> datetime | None:
        if injection.kind == "Deployment":
            # contract §4: for a rollout the execution starts with the first new pod created
            if self._rollout is None:
                self._rollout = self._new_rollout(self._delay_value(injection), self._broken_image)
            return self._rollout[0] if self._rollout is not None else None
        # a Schedule executes through the experiments it spawns: its first child's Applied
        names = (
            [name for name, _ in self._spawned(injection)]
            if injection.kind == "Schedule"
            else [injection.name]
        )
        items = []
        for name in names:
            items += json.loads(
                self._run(
                    [
                        "kubectl",
                        "-n",
                        NAMESPACE,
                        "get",
                        "events",
                        "--field-selector",
                        f"involvedObject.name={name},reason=Applied",
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

    def _delay_value(self, injection: Injection) -> str:
        return self._last_delay

    def remove(self, injection: Injection, journal: InjectorJournal) -> None:
        if injection.kind == "Deployment" and self._broken_image:
            restored = self._original_image
            result = self._restore_image()
            journal.record(
                verb="patch",
                object=f"deployment payment-service image {restored}",
                ok=result is not None and result.returncode == 0,
                response=(result.stdout or result.stderr) if result is not None else "",
                role=ROLE_CAUSE_REMOVED,
            )
            return
        if injection.kind == "Deployment":
            result = self._unset_delay()
            journal.record(
                verb="patch",
                object=f"deployment payment-service env {DELAY_VARIABLE}-",
                ok=result.returncode == 0,
                response=result.stdout or result.stderr,
                role=ROLE_CAUSE_REMOVED,
            )
            return
        result = self._run(
            [
                "kubectl",
                "-n",
                injection.namespace,
                "delete",
                injection.kind.lower(),
                injection.name,
                "--ignore-not-found",
            ],
            check=False,
        )
        journal.record(
            verb="delete",
            object=f"{injection.kind.lower()} {injection.name}",
            ok=result.returncode == 0,
            response=result.stdout or result.stderr,
            role=ROLE_DECOY_REMOVED
            if injection.namespace == DECOY_NAMESPACE
            else ROLE_CAUSE_REMOVED,
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
            StoredDiagnosis(
                str(r[0]), str(r[1]), _aware(r[2]), _aware(r[3]), dict(r[4]), _aware(r[5])
            )
            for r in rows
        ]

    def rediagnose(self, alerts: Collection[str], since: datetime) -> int:
        """Ask the control plane, through its own API, to diagnose each incident of the run once more."""
        asked = 0
        for stored in self.diagnoses(alerts, since):
            request = urllib.request.Request(
                f"{self.control_plane}/api/v1/incidents/{stored.incident_id}/diagnosis",
                data=b"",
                method="POST",
            )
            try:
                urllib.request.urlopen(request, timeout=120).read()
                asked += 1
            except OSError:
                pass  # the earlier diagnosis stays the latest; the run records how many were asked
        return asked

    def cleanup(self) -> None:
        if self._last_delay:
            self._unset_delay()  # already undone by remove() on a normal run; this covers an aborted one
        self._restore_image()
        self._delete_experiments()
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


def direct_pod_spec(repeats: int, seeds: tuple[int, ...], tier: str = "DEV") -> ScenarioSpec:
    """Slice 2 (design §10): a CPU stress on ``order-service`` under load."""
    return ScenarioSpec(
        scenario_id=DIRECT_SCENARIO_ID,
        family="direct-pod-fault",
        tier=tier,  # type: ignore[arg-type]
        repeats=repeats,
        seeds=seeds,
        parameters={
            "baseline_seconds": ParameterRange(low=45, high=45),
            "offset_seconds": ParameterRange(low=0, high=20),
            "duration_seconds": ParameterRange(low=90, high=110),
            "cpu_workers": ParameterRange(low=20, high=28),
            "load_rps": ParameterRange(low=8, high=12),
        },
    )


def config_spec(repeats: int, seeds: tuple[int, ...], tier: str = "DEV") -> ScenarioSpec:
    """Slice 3 (design §4, variant A): ``FAULT_PAYMENT_DELAY_MS`` set on ``payment-service`` by a rollout."""
    return ScenarioSpec(
        scenario_id=CONFIG_SCENARIO_ID,
        family="config-or-rollout",
        tier=tier,  # type: ignore[arg-type]
        repeats=repeats,
        seeds=seeds,
        parameters={
            "baseline_seconds": ParameterRange(low=45, high=45),
            "offset_seconds": ParameterRange(low=0, high=20),
            "duration_seconds": ParameterRange(low=90, high=110),
            "latency_ms": ParameterRange(low=600, high=1500),
            "load_rps": ParameterRange(low=8, high=12),
        },
    )


def scheduled_spec(repeats: int, seeds: tuple[int, ...], tier: str = "DEV") -> ScenarioSpec:
    """Slice 4b (design §4 `scheduled-recurring`, variant A as amended 2026-10-03): a Schedule spawning a 60 s
    delay on ``payment-service`` every 90 s, in place long enough for three spawns. The 20 s-every-minute
    form of slice 4 never raised a latency alert (its manifest has no spawn parameters, so it keeps 60 s / 20 s)."""
    return ScenarioSpec(
        scenario_id=SCHEDULED_SCENARIO_ID,
        family="scheduled-recurring",
        tier=tier,  # type: ignore[arg-type]
        repeats=repeats,
        seeds=seeds,
        parameters={
            "baseline_seconds": ParameterRange(low=45, high=45),
            "offset_seconds": ParameterRange(low=0, high=20),
            "duration_seconds": ParameterRange(low=300, high=360),
            "latency_ms": ParameterRange(low=300, high=600),
            "load_rps": ParameterRange(low=8, high=12),
            "spawn_every_seconds": ParameterRange(low=90, high=90),
            "spawn_seconds": ParameterRange(low=60, high=60),
        },
    )


def competing_spec(repeats: int, seeds: tuple[int, ...], tier: str = "DEV") -> ScenarioSpec:
    """Slice 5 (design §4 `competing-causes`, contract §15): a delay on ``payment-service`` and, after a seeded
    offset, a pod-kill of ``order-worker``; scored per incident against its symptom group."""
    return ScenarioSpec(
        scenario_id=COMPETING_SCENARIO_ID,
        family="competing-causes",
        tier=tier,  # type: ignore[arg-type]
        repeats=repeats,
        seeds=seeds,
        parameters={
            "baseline_seconds": ParameterRange(low=45, high=45),
            "offset_seconds": ParameterRange(low=0, high=20),
            "duration_seconds": ParameterRange(low=120, high=160),
            "latency_ms": ParameterRange(low=300, high=600),
            "load_rps": ParameterRange(low=8, high=12),
            "second_offset_seconds": ParameterRange(low=0, high=60),
        },
    )


def negative_spec(repeats: int, seeds: tuple[int, ...], tier: str = "DEV") -> ScenarioSpec:
    """Slice 6 (design §4 `negative-control`, contract §16): slice 3's real configuration change plus a decoy
    delay on the isolated workload, started 30 s before to 30 s after it."""
    return ScenarioSpec(
        scenario_id=NEGATIVE_SCENARIO_ID,
        family="negative-control",
        tier=tier,  # type: ignore[arg-type]
        repeats=repeats,
        seeds=seeds,
        parameters={
            "baseline_seconds": ParameterRange(low=45, high=45),
            "offset_seconds": ParameterRange(low=0, high=20),
            "duration_seconds": ParameterRange(low=90, high=110),
            "latency_ms": ParameterRange(low=600, high=1500),
            "load_rps": ParameterRange(low=8, high=12),
            "decoy_offset_seconds": ParameterRange(low=-30, high=30),
        },
    )


def _variant_b(
    base: Callable[..., ScenarioSpec], scenario_id: str, **extra: ParameterRange
) -> Callable[..., ScenarioSpec]:
    """A variant B of design §12.2.2: the family's variant A parameters, a new scenario and its own extras.
    Declared ``HOLDOUT`` by default (split by construction, §12.2.1)."""

    def spec(repeats: int, seeds: tuple[int, ...], tier: str = "HOLDOUT") -> ScenarioSpec:
        a = base(repeats, seeds, tier=tier)
        return a.model_copy(
            update={"scenario_id": scenario_id, "parameters": {**a.parameters, **extra}}
        )

    return spec


# blind phase 0 (design §12.4): at 20 to 40% the target saw the loss only now and then
LOSS = ParameterRange(low=50, high=70)
WORKERS = ParameterRange(low=20, high=28)

SPECS: dict[str, Callable[..., ScenarioSpec]] = {
    "dependency-b": _variant_b(dependency_spec, "dependency-loss-payment", loss_percent=LOSS),
    # blind phase 0 (design §12.4): at 20 to 28 workers payment-service stayed at 0.24 to 0.31 s, under every alert
    "direct-b": _variant_b(
        direct_pod_spec, "direct-stress-payment", cpu_workers=ParameterRange(low=56, high=72)
    ),
    "scheduled-b": _variant_b(scheduled_spec, "scheduled-stress-order", cpu_workers=WORKERS),
    "config-b": _variant_b(config_spec, "config-image-payment"),
    "negative-b": _variant_b(negative_spec, "negative-image-decoy", cpu_workers=WORKERS),
    "competing-b": _variant_b(competing_spec, "competing-loss-podkill", loss_percent=LOSS),
    "negative": negative_spec,
    "competing": competing_spec,
    "dependency": dependency_spec,
    "direct": direct_pod_spec,
    "config": config_spec,
    "scheduled": scheduled_spec,
}


def world_for(
    families: Collection[str], clock: RealClock, scenario_ids: Collection[str] = ()
) -> LabWorld:
    """The lab set up for a family: which workload the fault lands on and how its target is probed."""
    if "direct-stress-payment" in scenario_ids:
        return LabWorld(clock=clock, target_app="payment-service", payment_probe=True)
    if "scheduled-stress-order" in scenario_ids:
        return LabWorld(clock=clock, target_app="order-service")
    if {"config-image-payment", "negative-image-decoy"} & set(scenario_ids):
        return LabWorld(clock=clock, payment_probe=True)
    if {"dependency-loss-payment", "competing-loss-podkill"} & set(scenario_ids):
        # blind phase 0 (design §12.4): a single short /health exchange sees packet loss only now and then
        return LabWorld(clock=clock, payment_probe=True)
    if "direct-pod-fault" in families:
        return LabWorld(clock=clock, target_app="order-service")
    return LabWorld(
        clock=clock, payment_probe=bool({"config-or-rollout", "negative-control"} & set(families))
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


def _rescore(store: TestbedStore, suite: str) -> int:
    """Score the stored runs with the current scorer into ``score.v2.json``; nothing is re-run."""
    manifest = store.load_manifest(suite)
    scores: list[RunScore] = []
    for spec in manifest.scenarios:
        for repeat in range(spec.repeats):
            directory = store.run_dir(suite, spec.scenario_id, repeat)
            record = RunRecord.model_validate_json((directory / "run.json").read_bytes())
            stored = json.loads((directory / "diagnoses.json").read_bytes())
            if not record.valid or not stored:
                continue
            documents = [Diagnosis.model_validate(d["document"]) for d in stored]
            score = score_run(
                record,
                documents[0],
                tier=spec.tier,
                also=documents[1:],
                incidents=[
                    (str(d["alert"]), doc) for d, doc in zip(stored, documents, strict=True)
                ],
            )
            store.write_artifact(record, "score.v2.json", score.model_dump_json().encode())
            scores.append(score)
    print(json.dumps(aggregate(scores), indent=2, sort_keys=True))
    return 0


def _rederive(store: TestbedStore, suite: str) -> int:
    """Derive every stored run's timeline again by contract §14 (``timeline.v2.json``, ``run.v2.json``) and
    score the valid ones with the current scorer (``score.v3.json``); nothing is re-run or overwritten."""
    manifest = store.load_manifest(suite)
    scores: list[RunScore] = []
    for spec in manifest.scenarios:
        for repeat in range(spec.repeats):
            directory = store.run_dir(suite, spec.scenario_id, repeat)
            if not (directory / "run.json").exists():
                continue
            record = RunRecord.model_validate_json((directory / "run.json").read_bytes())
            series = SeriesWriter(directory / "series.jsonl").read()
            entries = InjectorJournal(directory / "journal.jsonl").entries()
            again = rederive(record, series, entries)
            body = again.model_dump(mode="json")
            store.write_artifact(record, "timeline.v2.json", _json(body["timeline"]))
            store.write_artifact(record, "run.v2.json", _json(body))
            moved = [
                f"{name} {(new.at - old.at).total_seconds():+.2f}s"
                for name in ORACLE_FIELDS
                if (old := record.timeline.stamp(name)) is not None
                and (new := again.timeline.stamp(name)) is not None
                and new.at != old.at
            ]
            print(
                f"{suite} {spec.scenario_id}#{repeat}: "
                f"{'VALID' if record.valid else 'INVALID'} -> {'VALID' if again.valid else 'INVALID'}"
                + (f"; moved: {', '.join(moved)}" if moved else "")
            )
            for reason in again.invalid_reasons:
                print(f"  invalid: {reason}")
            stored = json.loads((directory / "diagnoses.json").read_bytes())
            if not again.valid or not stored:
                continue
            documents = [Diagnosis.model_validate(d["document"]) for d in stored]
            score = score_run(
                again,
                documents[0],
                tier=spec.tier,
                also=documents[1:],
                incidents=[
                    (str(d["alert"]), doc) for d, doc in zip(stored, documents, strict=True)
                ],
            )
            store.write_artifact(record, "score.v3.json", score.model_dump_json().encode())
            scores.append(score)
    print(json.dumps(aggregate(scores), indent=2, sort_keys=True))
    return 0


def _json(document: object) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()


ORACLE_FIELDS = (
    "target_effect_at",
    "propagation_started_at",
    "symptom_started_at",
    "recovery_at",
)


ENGINE_PATHS = ("packages/rca", "apps/control_plane")


def engine_drift(commit: str) -> list[str]:
    """Files of the engine that differ from ``commit`` in the working tree (design §12.2.3); empty when frozen
    there or when the manifest predates the freeze."""
    if not commit:
        return []
    changed = subprocess.run(
        ["git", "diff", "--name-only", commit, "--", *ENGINE_PATHS],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "--", *ENGINE_PATHS],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return sorted(set(changed) | set(untracked))


def _blind_summary(outcome: RunOutcome, store: TestbedStore) -> str:
    """Validity, the timeline and the incidents' alert names; nothing the engine concluded."""
    record = outcome.record
    lines = [f"run {record.scenario_id}#{record.repeat}: {'VALID' if record.valid else 'INVALID'}"]
    lines += [f"  invalid: {reason}" for reason in record.invalid_reasons]
    base = record.timeline.cause_created_at
    for name in ORDER:
        stamp = record.timeline.stamp(name)
        offset = f"+{(stamp.at - base.at).total_seconds():6.1f}s" if stamp and base else "  -    "
        lines.append(f"  {name:24s} {offset}")
    directory = store.run_dir(record.suite_id, record.scenario_id, record.repeat)
    stored = json.loads((directory / "diagnoses.json").read_bytes())
    lines.append("  incident alerts: " + ", ".join(sorted({str(d["alert"]) for d in stored})))
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="packages.evals.live.testbed_lab")
    parser.add_argument("--root", type=Path, default=REPO / ".local/testbed")
    commands = parser.add_subparsers(dest="command", required=True)
    phase0 = commands.add_parser(
        "phase0", help="one unscored validation run, stored apart from the suite"
    )
    phase0.add_argument("--scenario", choices=sorted(SPECS), default="dependency")
    phase0.add_argument(
        "--blind",
        action="store_true",
        help="report validity and alert names only, never the engine's diagnosis (design §12.2.4)",
    )
    freeze = commands.add_parser("freeze", help="write the frozen manifest of slice 1")
    freeze.add_argument("--suite", required=True)
    freeze.add_argument("--salt", required=True)
    freeze.add_argument("--seeds", required=True, help="comma separated, one per repeat")
    freeze.add_argument("--engine-version", required=True)
    freeze.add_argument("--scenario", choices=sorted(SPECS), default="dependency")
    freeze.add_argument(
        "--tier", choices=("DEV", "HOLDOUT"), default=None, help="default: the scenario's own tier"
    )
    freeze.add_argument(
        "--engine-commit", default="", help="freeze the engine at this git commit (design §12.2.3)"
    )
    run = commands.add_parser("run", help="run the missing repeats of a frozen suite")
    run.add_argument("--suite", required=True)
    rescore = commands.add_parser(
        "rescore", help="score a suite's stored runs again, keeping the old scores"
    )
    rescore.add_argument("--suite", required=True)
    again = commands.add_parser(
        "rederive",
        help="derive the stored runs' timelines again by contract §14, keeping the originals",
    )
    again.add_argument("--suite", required=True)
    args = parser.parse_args(argv)
    clock = RealClock()

    if args.command == "rescore":
        return _rescore(TestbedStore(args.root), args.suite)
    if args.command == "rederive":
        return _rederive(TestbedStore(args.root), args.suite)

    if args.command == "freeze":
        seeds = tuple(int(s) for s in args.seeds.split(","))
        acceptance: dict[str, float] = {"false_resolved": 0, "false_strong_authority": 0}
        if SPECS[args.scenario](1, (1,)).family == "negative-control":
            acceptance["decoy_named"] = 0  # contract §16.5
        manifest = SuiteManifest(
            suite_id=args.suite,
            engine_version=args.engine_version,
            created_at=clock.now(),
            salt=args.salt,
            scenarios=(
                SPECS[args.scenario](
                    len(seeds), seeds, **({"tier": args.tier} if args.tier else {})
                ),
            ),
            acceptance=acceptance,
            engine_commit=args.engine_commit,
        ).frozen()
        print(TestbedStore(args.root).write_manifest(manifest), manifest.sha256)
        return 0

    if args.command == "phase0":
        probe = SPECS[args.scenario](1, (1,))
        world = world_for({probe.family}, clock, {probe.scenario_id})
        # Phase 0 is not part of any suite: its own store, its own manifest, never scored into a result.
        from packages.rca.engine import RCA_ENGINE_VERSION

        suite = "phase0-" + clock.now().strftime("%Y%m%dT%H%M%S")
        store = TestbedStore(args.root / "phase0")
        manifest = SuiteManifest(
            suite_id=suite,
            engine_version=RCA_ENGINE_VERSION,
            created_at=clock.now(),
            salt="phase0",
            scenarios=(spec := SPECS[args.scenario](1, (1,)),),
            acceptance={},
        ).frozen()
        store.write_manifest(manifest)
        try:
            outcome = run_once(
                manifest,
                spec.scenario_id,
                0,
                world,
                store,
                work_root=args.root / "work",
                clock=clock,
            )
        except BaselineNotQuiet as refused:
            print(f"run refused before the injection: {refused}")
            return 2
        print(_blind_summary(outcome, store) if args.blind else _summarize(outcome))
        return 0 if outcome.record.valid else 1

    store = TestbedStore(args.root)
    manifest = store.load_manifest(args.suite)
    drift = engine_drift(manifest.engine_commit)
    if drift:
        print(
            f"run refused: the engine differs from the frozen commit {manifest.engine_commit}: {drift}"
        )
        return 2
    world = world_for(
        {sp.family for sp in manifest.scenarios},
        clock,
        {sp.scenario_id for sp in manifest.scenarios},
    )
    outcomes: list[RunOutcome] = []
    for spec in manifest.scenarios:
        for repeat in range(spec.repeats):
            if store.run_dir(manifest.suite_id, spec.scenario_id, repeat).exists():
                continue
            try:
                outcome = run_once(
                    manifest,
                    spec.scenario_id,
                    repeat,
                    world,
                    store,
                    work_root=args.root / "work",
                    clock=clock,
                )
            except BaselineNotQuiet as refused:
                # the suite stops: a world that is not quiet is fixed first, never retried until lucky
                print(f"run {spec.scenario_id}#{repeat} refused before the injection: {refused}")
                return 2
            print(_summarize(outcome), flush=True)
            outcomes.append(outcome)
    scores = [o.score for o in outcomes if o.score is not None]
    print(json.dumps(aggregate(scores), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

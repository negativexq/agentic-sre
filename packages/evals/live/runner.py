"""Execute live scenarios end to end against a running demo cluster.

One run is: restore a clean baseline, stage the fault, drive bounded traffic,
wait for the real alerting path to open an incident, read the diagnosis the
control plane stored, grade it, and undo the fault.  Nothing here reads
diagnosis state before grading, and nothing tells the engine what was staged.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.request import Request, urlopen

from packages.evals.live.actions import (
    Action,
    ActionError,
    Context,
    EnvUnset,
    HttpFault,
    PortForwarder,
    WaitRollout,
    kubectl,
    truncate_change_journal,
)
from packages.evals.live.grader import Outcome, ScenarioResult, SuiteReport, grade
from packages.evals.live.scenarios import LiveScenario, Target, Workload

# A rollout replaces the pod behind a port-forward mid-request, so the workload
# driver and the control-plane pollers must treat a dropped connection as a
# failed request, not a crash.  OSError covers URLError, HTTPError, TimeoutError
# and every ConnectionError; HTTPException covers RemoteDisconnected and the
# other partial-response errors that are not OSErrors.
TRANSPORT_ERRORS = (OSError, http.client.HTTPException)

REQUEST_TIMEOUT_SECONDS = 20
INCIDENT_TIMEOUT_SECONDS = 300
DIAGNOSIS_TIMEOUT_SECONDS = 180
POLL_INTERVAL_SECONDS = 5.0
SETTLE_SECONDS = 20.0


@dataclass(frozen=True, slots=True)
class RunOptions:
    """Knobs a caller may vary without touching scenario definitions."""

    incident_timeout_seconds: float = INCIDENT_TIMEOUT_SECONDS
    diagnosis_timeout_seconds: float = DIAGNOSIS_TIMEOUT_SECONDS
    settle_seconds: float = SETTLE_SECONDS
    api_token: str = ""
    """When set, the runner triggers diagnosis instead of waiting for the watcher."""

    keep_fault: bool = False
    """Skip teardown so the staged fault stays visible in the UI."""


DEFAULT_OPTIONS = RunOptions()


def _http(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    token: str = "",
    timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> Any:
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    if token:
        # The control plane's shared guard expects a bearer token, not a
        # custom header (see apps/control_plane/auth.py).
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, data=data, headers=headers, method=method)  # noqa: S310
    with urlopen(request, timeout=timeout) as response:  # noqa: S310
        body = response.read().decode()
    return json.loads(body) if body else None


def _http_or_none(url: str, *, token: str = "") -> Any:
    try:
        return _http(url, token=token)
    except TRANSPORT_ERRORS:
        return None


def _order_request() -> dict[str, Any]:
    return {"customer_id": f"bench-{uuid.uuid4().hex[:8]}", "amount_cents": 4200, "currency": "USD"}


def _payment_request() -> dict[str, Any]:
    return {
        "order_id": str(uuid.uuid4()),
        "amount_cents": 4200,
        "currency": "USD",
    }


def _workload_endpoint(
    workload: Workload, context: Context
) -> tuple[str, Callable[[], dict[str, Any]]]:
    if workload.target is Target.ORDERS:
        return f"{context.order_url}/orders", _order_request
    return f"{context.payment_url}/payments", _payment_request


class WorkloadDriver:
    """Drive a scenario's traffic in the background until told to stop.

    A single burst of requests leaves the metric's rate window populated for
    only a few seconds, which is too narrow for a scrape, an alert evaluation
    and the control-plane watch to all land inside it.  Running traffic
    continuously keeps the symptom elevated across the whole detection window,
    so the driver runs in a thread and is stopped once the incident opens (or
    the wait times out).  A failed request is counted, not raised: for most
    scenarios a failing request is the symptom being staged.
    """

    def __init__(self, workload: Workload, context: Context) -> None:
        self._workload = workload
        self._context = context
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._attempted = 0
        self._succeeded = 0
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        return self._workload.target is not Target.NONE and self._workload.count > 0

    def _once(self, url: str, body: Callable[[], dict[str, Any]]) -> None:
        ok = True
        try:
            _http(url, method="POST", payload=body())
        except TRANSPORT_ERRORS:
            ok = False
        with self._lock:
            self._attempted += 1
            self._succeeded += int(ok)

    def _run(self) -> None:
        url, body = _workload_endpoint(self._workload, self._context)
        concurrency = max(1, self._workload.concurrency)
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            while not self._stop.is_set():
                futures = [pool.submit(self._once, url, body) for _ in range(concurrency)]
                for future in futures:
                    future.result()
                if self._workload.interval_seconds:
                    self._stop.wait(self._workload.interval_seconds)

    def start(self) -> None:
        if not self.active:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> int:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=30)
        with self._lock:
            return self._succeeded


def drive_workload(workload: Workload, context: Context) -> int:
    """Send one bounded burst of a scenario's traffic and return successes.

    Kept for callers and tests that want a single deterministic burst; the
    scenario runner uses :class:`WorkloadDriver` for sustained load instead.
    """
    if workload.target is Target.NONE or workload.count == 0:
        return 0
    url, body = _workload_endpoint(workload, context)

    def once() -> bool:
        try:
            _http(url, method="POST", payload=body())
        except TRANSPORT_ERRORS:
            return False
        return True

    succeeded = 0
    for wave in range(workload.waves):
        if workload.concurrency > 1:
            with ThreadPoolExecutor(max_workers=workload.concurrency) as pool:
                succeeded += sum(pool.map(lambda _: once(), range(workload.count)))
        else:
            for _ in range(workload.count):
                succeeded += once()
                if workload.interval_seconds:
                    time.sleep(workload.interval_seconds)
        if wave + 1 < workload.waves:
            time.sleep(workload.wave_interval_seconds)
    return succeeded


def reset_baseline(context: Context) -> None:
    """Return the namespace to its manifest state before staging a scenario."""
    for action in (
        HttpFault("payment-service"),
        HttpFault("order-service"),
        EnvUnset(
            "payment-service",
            ("FAULT_PAYMENT_DELAY_MS", "FAULT_PAYMENT_ERROR", "FAULT_PAYMENT_DB_HOLD_MS"),
        ),
        EnvUnset(
            "order-service", ("FAULT_ORDER_DELAY_MS", "FAULT_ORDER_ERROR", "PAYMENT_SERVICE_URL")
        ),
        EnvUnset("order-worker", ("FAULT_WORKER_DELAY_MS", "FAULT_WORKER_FAILURE")),
    ):
        try:
            action.apply(context)
        except ActionError:
            # A baseline that is already clean reports an error for the unset of
            # a variable that was never set.  That is the desired state, not a
            # failure, so it must not abort the run.
            continue


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _matching_incident(context: Context, alert_name: str, since: datetime) -> str | None:
    """Return the incident this scenario's alert produced, if any.

    The control plane correlates a re-firing alert into the incident it already
    opened rather than minting a new id, and Alertmanager will not redeliver a
    still-firing alert for an hour, so a scenario's incident is recognised by
    its alert title and an ``updated_at`` at or after the moment we staged the
    fault — not by the appearance of a new id.
    """
    incidents = _http_or_none(f"{context.control_plane_url}/api/v1/incidents") or []
    best: tuple[datetime, str] | None = None
    for item in incidents:
        if not isinstance(item, dict) or item.get("title") != alert_name:
            continue
        updated = _parse_time(item.get("updated_at"))
        incident_id = item.get("incident_id")
        if updated is None or incident_id is None or updated < since:
            continue
        if best is None or updated > best[0]:
            best = (updated, str(incident_id))
    return best[1] if best else None


def _wait_for_incident(
    context: Context, alert_name: str, since: datetime, *, timeout_seconds: float
) -> tuple[str, float] | None:
    started = time.monotonic()
    while time.monotonic() - started < timeout_seconds:
        incident_id = _matching_incident(context, alert_name, since)
        if incident_id is not None:
            return incident_id, time.monotonic() - started
        time.sleep(POLL_INTERVAL_SECONDS)
    return None


def _firing_alert_names(context: Context) -> set[str]:
    payload = _http_or_none(f"{context.prometheus_url}/api/v1/alerts")
    if not isinstance(payload, dict):
        return set()
    alerts = payload.get("data", {}).get("alerts", [])
    return {
        str(alert["labels"]["alertname"])
        for alert in alerts
        if isinstance(alert, dict)
        and alert.get("state") == "firing"
        and "alertname" in alert.get("labels", {})
    }


def _wait_for_alert_clear(context: Context, alert_name: str, *, timeout_seconds: float) -> bool:
    """Wait until ``alert_name`` is not firing, so the next firing is a real transition.

    Alertmanager only delivers an alert on a not-firing → firing edge (its
    repeat interval is an hour), so a scenario staged while its alert is still
    firing from the previous run would produce no delivery and no fresh
    incident.  Returns True once clear, False if it never cleared in time.
    """
    started = time.monotonic()
    while time.monotonic() - started < timeout_seconds:
        if alert_name not in _firing_alert_names(context):
            return True
        time.sleep(POLL_INTERVAL_SECONDS)
    return False


def _wait_for_diagnosis(
    context: Context, incident_id: str, *, timeout_seconds: float, token: str
) -> dict[str, Any] | None:
    url = f"{context.control_plane_url}/api/v1/incidents/{incident_id}/diagnosis"
    if token:
        try:
            result = _http(url, method="POST", token=token, timeout=timeout_seconds)
        except TRANSPORT_ERRORS:
            result = None
        if isinstance(result, dict):
            return result
    started = time.monotonic()
    while time.monotonic() - started < timeout_seconds:
        document = _http_or_none(url)
        if isinstance(document, dict) and document.get("error") is None:
            return document
        time.sleep(POLL_INTERVAL_SECONDS)
    return None


def _apply(actions: tuple[Action, ...], context: Context) -> None:
    for action in actions:
        action.apply(context)


def reset_change_journal(context: Context, *, token: str = "", settle_seconds: float = 3.0) -> None:
    """Clear the change journal and re-establish a baseline snapshot.

    Runs after the baseline is restored and before the fault is staged, so the
    only change in the engine's two-hour window is the one the scenario stages.
    """
    truncate_change_journal(context)
    try:
        _http(f"{context.control_plane_url}/api/v1/cluster/snapshot", method="POST", token=token)
    except TRANSPORT_ERRORS:
        pass
    time.sleep(settle_seconds)


def run_scenario(
    scenario: LiveScenario,
    context: Context,
    options: RunOptions = DEFAULT_OPTIONS,
    *,
    forwarder: PortForwarder | None = None,
    on_event: Callable[[str], None] = lambda message: None,
) -> ScenarioResult:
    """Stage, observe, and grade one scenario."""
    # The previous scenario's teardown rolled deployments, so re-point the
    # workload forwards before baseline clears the runtime faults through them.
    if forwarder is not None:
        forwarder.refresh(("order-service", "payment-service"))
    on_event(f"[{scenario.id}] baseline")
    reset_baseline(context)
    # Baseline should have restored every service, so a forward that still will
    # not open now points at a genuinely unhealthy dependency, not scenario state.
    if forwarder is not None:
        down = forwarder.refresh(("order-service", "payment-service"))
        if down:
            on_event(f"[{scenario.id}] warning: {', '.join(down)} not reachable after baseline")
    # The alert must be clear before staging, or Alertmanager will not deliver a
    # fresh firing and no incident will be raised for this run.
    if not _wait_for_alert_clear(context, scenario.alert, timeout_seconds=120):
        on_event(f"[{scenario.id}] warning: {scenario.alert} still firing from a prior run")

    # Wipe the two-hour change journal and re-baseline, so the staged fault is
    # the only change the engine can find — otherwise baseline-reset rollouts
    # and adjacent scenarios leak in as false root causes.
    on_event(f"[{scenario.id}] reset change journal")
    reset_change_journal(context, token=options.api_token)

    try:
        on_event(f"[{scenario.id}] stage: {'; '.join(a.describe() for a in scenario.setup) or '-'}")
        stage_started = utc_now()
        _apply(scenario.setup, context)
    except ActionError as error:
        _teardown(scenario, context, options, on_event)
        return ScenarioResult(
            scenario_id=scenario.id,
            outcome=Outcome.ERROR,
            expected=scenario.expectation.label,
            actual="",
            detail=str(error),
        )

    # Setup may roll or break a service, and kubectl port-forward does not follow
    # a rollout, so re-point the workload forwards before traffic.  A forward the
    # scenario intentionally broke stays down; its traffic then fails, as intended.
    if forwarder is not None:
        forwarder.refresh(("order-service", "payment-service"))

    # Keep traffic flowing while we wait, so the symptom stays elevated across
    # the whole scrape/alert/watch window instead of decaying after one burst.
    driver = WorkloadDriver(scenario.workload, context)
    on_event(f"[{scenario.id}] traffic ({'sustained' if driver.active else 'none'})")
    driver.start()
    try:
        arrival = _wait_for_incident(
            context, scenario.alert, stage_started, timeout_seconds=options.incident_timeout_seconds
        )
    finally:
        succeeded = driver.stop()
    if arrival is None:
        on_event(f"[{scenario.id}] no incident ({succeeded} ok requests)")
        _teardown(scenario, context, options, on_event)
        return ScenarioResult(
            scenario_id=scenario.id,
            outcome=Outcome.NO_INCIDENT,
            expected=scenario.expectation.label,
            actual="",
            detail=f"no incident within {options.incident_timeout_seconds:.0f}s",
        )
    incident_id, alert_seconds = arrival
    on_event(f"[{scenario.id}] incident {incident_id} after {alert_seconds:.0f}s")

    # Let the change journal catch up with the staged mutation before reading the
    # diagnosis, so a correct engine is not graded against a half-built history.
    time.sleep(options.settle_seconds)
    document = _wait_for_diagnosis(
        context,
        incident_id,
        timeout_seconds=options.diagnosis_timeout_seconds,
        token=options.api_token,
    )
    result = grade(scenario, document)
    _teardown(scenario, context, options, on_event)
    return ScenarioResult(
        scenario_id=result.scenario_id,
        outcome=result.outcome,
        expected=result.expected,
        actual=result.actual,
        resolution=result.resolution,
        confidence=result.confidence,
        finding_kinds=result.finding_kinds,
        supported_by_expected_kind=result.supported_by_expected_kind,
        incident_id=incident_id,
        alert_seconds=alert_seconds,
        detail=result.detail,
    )


def _teardown(
    scenario: LiveScenario,
    context: Context,
    options: RunOptions,
    on_event: Callable[[str], None],
) -> None:
    if options.keep_fault:
        on_event(f"[{scenario.id}] fault left in place")
        return
    on_event(f"[{scenario.id}] teardown")
    for action in scenario.teardown:
        try:
            action.apply(context)
        except ActionError as error:
            on_event(f"[{scenario.id}] teardown failed: {error}")
    reset_baseline(context)


def run_suite(
    scenarios: tuple[LiveScenario, ...],
    context: Context,
    options: RunOptions = DEFAULT_OPTIONS,
    *,
    forwarder: PortForwarder | None = None,
    on_event: Callable[[str], None] = lambda message: None,
) -> SuiteReport:
    """Run every scenario in order and aggregate the outcomes."""
    results: list[ScenarioResult] = []
    for scenario in scenarios:
        try:
            results.append(
                run_scenario(scenario, context, options, forwarder=forwarder, on_event=on_event)
            )
        except Exception as error:  # noqa: BLE001 - one scenario must not sink the suite
            on_event(f"[{scenario.id}] unexpected error: {error!r}")
            results.append(
                ScenarioResult(
                    scenario_id=scenario.id,
                    outcome=Outcome.ERROR,
                    expected=scenario.expectation.label,
                    actual="",
                    detail=repr(error),
                )
            )
    return SuiteReport(results=tuple(results))


WORKLOAD_MANIFEST = "infra/kubernetes/workload.yaml"


def restore_namespace(context: Context) -> None:
    """Undo anything a partial run may have left behind.

    Re-applying the workload manifest resets every deployment's env, image,
    resources, replicas and Service selector to their shipped state in one step,
    which is more robust after a crashed run than undoing each field piecemeal.
    """
    reset_baseline(context)
    for kind, name in (
        ("networkpolicy", "deny-order-egress"),
        ("resourcequota", "sre-demo-pods"),
        ("horizontalpodautoscaler", "order-service"),
    ):
        kubectl("delete", kind, name, "--ignore-not-found", context=context, check=False)
    kubectl("apply", "-f", WORKLOAD_MANIFEST, context=context, check=False)
    for deployment in ("order-service", "payment-service", "order-worker"):
        WaitRollout(deployment, timeout_seconds=120).apply(context)


def utc_now() -> datetime:
    return datetime.now(UTC)

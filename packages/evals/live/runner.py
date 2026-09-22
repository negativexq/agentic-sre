"""Execute live scenarios end to end against a running demo cluster.

One run is: restore a clean baseline, stage the fault, drive bounded traffic,
wait for the real alerting path to open an incident, read the diagnosis the
control plane stored, grade it, and undo the fault.  Nothing here reads
diagnosis state before grading, and nothing tells the engine what was staged.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from packages.evals.live.actions import (
    Action,
    ActionError,
    Context,
    EnvUnset,
    HttpFault,
    WaitRollout,
    kubectl,
)
from packages.evals.live.grader import Outcome, ScenarioResult, SuiteReport, grade
from packages.evals.live.scenarios import LiveScenario, Target, Workload

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
        headers["X-API-Token"] = token
    request = Request(url, data=data, headers=headers, method=method)  # noqa: S310
    with urlopen(request, timeout=timeout) as response:  # noqa: S310
        body = response.read().decode()
    return json.loads(body) if body else None


def _http_or_none(url: str, *, token: str = "") -> Any:
    try:
        return _http(url, token=token)
    except (HTTPError, URLError, TimeoutError):
        return None


def _order_request() -> dict[str, Any]:
    return {"customer_id": f"bench-{uuid.uuid4().hex[:8]}", "amount_cents": 4200, "currency": "USD"}


def _payment_request() -> dict[str, Any]:
    return {
        "order_id": str(uuid.uuid4()),
        "amount_cents": 4200,
        "currency": "USD",
    }


def drive_workload(workload: Workload, context: Context) -> int:
    """Send the scenario's traffic and return how many requests succeeded.

    Failures are counted, not raised: for most scenarios a failing request is
    the symptom being staged.
    """
    if workload.target is Target.NONE or workload.count == 0:
        return 0
    if workload.target is Target.ORDERS:
        url, body = f"{context.order_url}/orders", _order_request
    else:
        url, body = f"{context.payment_url}/payments", _payment_request

    def once() -> bool:
        try:
            _http(url, method="POST", payload=body())
        except (HTTPError, URLError, TimeoutError):
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


def _incident_ids(context: Context) -> set[str]:
    incidents = _http_or_none(f"{context.control_plane_url}/api/v1/incidents") or []
    return {str(item["id"]) for item in incidents if isinstance(item, dict) and "id" in item}


def _wait_for_new_incident(
    context: Context, known: set[str], *, timeout_seconds: float
) -> tuple[str, float] | None:
    started = time.monotonic()
    while time.monotonic() - started < timeout_seconds:
        for incident_id in _incident_ids(context) - known:
            return incident_id, time.monotonic() - started
        time.sleep(POLL_INTERVAL_SECONDS)
    return None


def _wait_for_diagnosis(
    context: Context, incident_id: str, *, timeout_seconds: float, token: str
) -> dict[str, Any] | None:
    url = f"{context.control_plane_url}/api/v1/incidents/{incident_id}/diagnosis"
    if token:
        try:
            result = _http(url, method="POST", token=token, timeout=timeout_seconds)
        except (HTTPError, URLError, TimeoutError):
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


def run_scenario(
    scenario: LiveScenario,
    context: Context,
    options: RunOptions = DEFAULT_OPTIONS,
    *,
    on_event: Callable[[str], None] = lambda message: None,
) -> ScenarioResult:
    """Stage, observe, and grade one scenario."""
    on_event(f"[{scenario.id}] baseline")
    reset_baseline(context)
    known = _incident_ids(context)

    try:
        on_event(f"[{scenario.id}] stage: {'; '.join(a.describe() for a in scenario.setup) or '-'}")
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

    on_event(f"[{scenario.id}] traffic")
    succeeded = drive_workload(scenario.workload, context)
    on_event(f"[{scenario.id}] traffic done ({succeeded} ok); waiting for the alert to fire")

    arrival = _wait_for_new_incident(
        context, known, timeout_seconds=options.incident_timeout_seconds
    )
    if arrival is None:
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
    on_event: Callable[[str], None] = lambda message: None,
) -> SuiteReport:
    """Run every scenario in order and aggregate the outcomes."""
    results = tuple(
        run_scenario(scenario, context, options, on_event=on_event) for scenario in scenarios
    )
    return SuiteReport(results=results)


def restore_namespace(context: Context) -> None:
    """Undo anything a partial run may have left behind."""
    reset_baseline(context)
    for kind, name in (
        ("networkpolicy", "deny-order-egress"),
        ("resourcequota", "sre-demo-pods"),
        ("horizontalpodautoscaler", "order-service"),
    ):
        kubectl("delete", kind, name, "--ignore-not-found", context=context, check=False)
    kubectl("scale", "deployment/order-service", "--replicas=2", context=context, check=False)
    kubectl(
        "set",
        "image",
        "deployment/payment-service",
        "payment-service=agentic-sre/payment-service:dev",
        context=context,
        check=False,
    )
    kubectl(
        "patch",
        "service",
        "payment-service",
        "--type=merge",
        "-p",
        json.dumps({"spec": {"selector": {"app": "payment-service"}}}),
        context=context,
        check=False,
    )
    for deployment in ("order-service", "payment-service", "order-worker"):
        WaitRollout(deployment, timeout_seconds=120).apply(context)


def utc_now() -> datetime:
    return datetime.now(UTC)

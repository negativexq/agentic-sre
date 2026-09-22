"""The internal live scenario suite.

Each scenario stages a real fault in the demo namespace, lets the real alerting
path fire, and is graded against what the change-first engine should conclude.

Two expectation classes carry equal weight:

``RootCause``
    The fault leaves a durable Kubernetes trace, so the engine must name the
    actor that caused it.

``Abstain``
    The fault lives only in a process's memory and leaves no cluster change.
    The engine must decline to name a root cause.  Scoring an abstention as a
    success is deliberate: a change-first engine that invents a root cause when
    no change exists is worse than one that says it does not know.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from packages.evals.live.actions import (
    Action,
    ApplyFile,
    ApplyManifest,
    DeleteObject,
    DeletePod,
    EnvPatch,
    EnvUnset,
    HttpFault,
    PatchConfigMap,
    PatchService,
    RestartContainer,
    RolloutRestart,
    Scale,
    SetImage,
    SetResources,
    WaitRollout,
)

NAMESPACE = "sre-demo"


class Tier(StrEnum):
    """How a scenario may be used."""

    DEV = "DEV"
    """Freely used for development and tuning."""

    HOLDOUT = "HOLDOUT"
    """Frozen; only run for reported measurements, never for tuning."""


class Target(StrEnum):
    """Which workload endpoint drives traffic for a scenario."""

    ORDERS = "orders"
    PAYMENTS = "payments"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class Workload:
    """Bounded, deterministic traffic used to make a fault observable."""

    target: Target = Target.ORDERS
    count: int = 60
    concurrency: int = 1
    interval_seconds: float = 0.5
    waves: int = 1
    wave_interval_seconds: float = 5.0


NO_WORKLOAD = Workload(target=Target.NONE, count=0)


@dataclass(frozen=True, slots=True)
class RootCause:
    """The engine must name this actor as the cause."""

    kind: str
    name: str
    finding_kinds: tuple[str, ...] = ()
    """At least one of these findings must back the answer, when given."""

    @property
    def label(self) -> str:
        return f"{NAMESPACE}/{self.kind}/{self.name}"


@dataclass(frozen=True, slots=True)
class Abstain:
    """The engine must decline to name a root cause."""

    because: str

    @property
    def label(self) -> str:
        return "ABSTAIN"


Expectation = RootCause | Abstain


@dataclass(frozen=True, slots=True)
class LiveScenario:
    """One staged fault, its traffic, and how its diagnosis is graded."""

    id: str
    title: str
    alert: str
    service: str
    expectation: Expectation
    setup: tuple[Action, ...] = ()
    workload: Workload = field(default_factory=Workload)
    teardown: tuple[Action, ...] = ()
    tier: Tier = Tier.DEV
    demo: bool = False
    """Whether this scenario is worth showing in the live UI demo."""

    @property
    def expects_root_cause(self) -> bool:
        return isinstance(self.expectation, RootCause)


def _clear_faults() -> tuple[Action, ...]:
    return (HttpFault("payment-service"), HttpFault("order-service"))


# --------------------------------------------------------------------------
# Runtime-only faults.  These never touch the Kubernetes API, so no change
# exists for the engine to find and abstention is the correct answer.
# --------------------------------------------------------------------------

_RUNTIME_FAULTS: tuple[LiveScenario, ...] = (
    LiveScenario(
        id="payment_error_spike",
        title="Payment service returns errors",
        alert="PaymentErrorRateHigh",
        service="payment-service",
        expectation=Abstain(because="in-process error injection leaves no cluster change"),
        setup=(HttpFault("payment-service", {"error": True}),),
        workload=Workload(target=Target.PAYMENTS, count=60),
        teardown=_clear_faults(),
    ),
    LiveScenario(
        id="order_error_spike",
        title="Order service returns errors",
        alert="OrderErrorRateHigh",
        service="order-service",
        expectation=Abstain(because="in-process error injection leaves no cluster change"),
        setup=(HttpFault("order-service", {"error": True}),),
        workload=Workload(target=Target.ORDERS, count=60),
        teardown=_clear_faults(),
    ),
    LiveScenario(
        id="payment_dependency_latency",
        title="Payment dependency slows the order path",
        alert="OrderDependencyLatencyHigh",
        service="order-service",
        expectation=Abstain(because="in-process delay leaves no cluster change"),
        setup=(HttpFault("payment-service", {"delay_ms": 700}),),
        workload=Workload(target=Target.ORDERS, count=60),
        teardown=_clear_faults(),
    ),
    LiveScenario(
        id="order_latency_spike",
        title="Order service latency rises",
        alert="OrderRequestLatencyHigh",
        service="order-service",
        expectation=Abstain(because="in-process delay leaves no cluster change"),
        setup=(HttpFault("order-service", {"delay_ms": 700}),),
        workload=Workload(target=Target.ORDERS, count=60),
        teardown=_clear_faults(),
    ),
    LiveScenario(
        id="payment_db_pool_pressure",
        title="Payment connection pool saturates",
        alert="PaymentDbAcquisitionSlow",
        service="payment-service",
        expectation=Abstain(because="in-process connection hold leaves no cluster change"),
        setup=(HttpFault("payment-service", {"db_hold_ms": 3500}),),
        workload=Workload(target=Target.PAYMENTS, count=18, concurrency=18, waves=3),
        teardown=_clear_faults(),
    ),
    LiveScenario(
        id="order_db_query_latency",
        title="Order database queries slow down",
        alert="OrderDbQueryLatencyHigh",
        service="order-service",
        expectation=Abstain(because="in-process query delay leaves no cluster change"),
        setup=(HttpFault("order-service", {"db_query_delay_ms": 700}),),
        workload=Workload(target=Target.ORDERS, count=60),
        teardown=_clear_faults(),
    ),
    LiveScenario(
        id="cross_service_payment_failure",
        title="Order failures caused by a failing payment dependency",
        alert="OrderErrorRateHigh",
        service="order-service",
        expectation=Abstain(because="in-process error injection leaves no cluster change"),
        setup=(HttpFault("payment-service", {"error": True}),),
        workload=Workload(target=Target.ORDERS, count=60),
        teardown=_clear_faults(),
        tier=Tier.HOLDOUT,
    ),
    LiveScenario(
        id="cross_service_db_pressure",
        title="Order latency caused by payment pool pressure",
        alert="OrderRequestLatencyHigh",
        service="order-service",
        expectation=Abstain(because="in-process connection hold leaves no cluster change"),
        setup=(HttpFault("payment-service", {"db_hold_ms": 3500}),),
        workload=Workload(target=Target.ORDERS, count=30, concurrency=30, waves=4),
        teardown=_clear_faults(),
        tier=Tier.HOLDOUT,
    ),
    LiveScenario(
        id="payment_db_query_latency",
        title="Payment database queries slow down",
        alert="PaymentRequestLatencyHigh",
        service="payment-service",
        expectation=Abstain(because="in-process query delay leaves no cluster change"),
        setup=(HttpFault("payment-service", {"db_query_delay_ms": 700}),),
        workload=Workload(target=Target.PAYMENTS, count=60),
        teardown=_clear_faults(),
        tier=Tier.HOLDOUT,
    ),
    LiveScenario(
        id="traffic_surge_no_change",
        title="Load surge with no underlying change",
        alert="OrderRequestLatencyHigh",
        service="order-service",
        expectation=Abstain(because="nothing changed; the system is merely busy"),
        setup=(),
        workload=Workload(target=Target.ORDERS, count=40, concurrency=40, waves=5),
        teardown=_clear_faults(),
        tier=Tier.HOLDOUT,
    ),
)


# --------------------------------------------------------------------------
# Kubernetes-visible changes.  Each leaves a durable trace the engine reads.
# --------------------------------------------------------------------------

_SPEC_CHANGES: tuple[LiveScenario, ...] = (
    LiveScenario(
        id="order_worker_lag",
        title="Worker slowdown builds Kafka consumer lag",
        alert="OrderWorkerLagHigh",
        service="order-worker",
        expectation=RootCause("Deployment", "order-worker", ("SPEC_CHANGE",)),
        setup=(
            EnvPatch(
                "order-worker", {"FAULT_WORKER_DELAY_MS": "2000", "FAULT_WORKER_FAILURE": "false"}
            ),
        ),
        workload=Workload(target=Target.ORDERS, count=30, waves=2),
        teardown=(EnvUnset("order-worker", ("FAULT_WORKER_DELAY_MS", "FAULT_WORKER_FAILURE")),),
        demo=True,
    ),
    LiveScenario(
        id="order_worker_failure",
        title="Worker consumer starts failing",
        alert="OrderWorkerConsumerErrorsHigh",
        service="order-worker",
        expectation=RootCause("Deployment", "order-worker", ("SPEC_CHANGE",)),
        setup=(
            EnvPatch(
                "order-worker", {"FAULT_WORKER_DELAY_MS": "0", "FAULT_WORKER_FAILURE": "true"}
            ),
        ),
        workload=Workload(target=Target.ORDERS, count=40),
        teardown=(EnvUnset("order-worker", ("FAULT_WORKER_DELAY_MS", "FAULT_WORKER_FAILURE")),),
        demo=True,
    ),
    LiveScenario(
        id="payment_config_change",
        title="A bad rollout makes payments take seconds",
        alert="PaymentServiceLatencyCritical",
        service="payment-service",
        expectation=RootCause("Deployment", "payment-service", ("SPEC_CHANGE",)),
        setup=(EnvPatch("payment-service", {"FAULT_PAYMENT_DELAY_MS": "10000"}),),
        workload=Workload(target=Target.PAYMENTS, count=20, concurrency=20),
        teardown=(EnvUnset("payment-service", ("FAULT_PAYMENT_DELAY_MS",)),),
        demo=True,
    ),
    LiveScenario(
        id="order_config_latency",
        title="An order-service rollout adds latency",
        alert="OrderRequestLatencyHigh",
        service="order-service",
        expectation=RootCause("Deployment", "order-service", ("SPEC_CHANGE",)),
        setup=(EnvPatch("order-service", {"FAULT_ORDER_DELAY_MS": "700"}),),
        workload=Workload(target=Target.ORDERS, count=60),
        teardown=(EnvUnset("order-service", ("FAULT_ORDER_DELAY_MS",)),),
        demo=True,
    ),
    LiveScenario(
        id="payment_config_cross_service_impact",
        title="A payment rollout degrades the order service",
        alert="OrderRequestLatencyHigh",
        service="order-service",
        expectation=RootCause("Deployment", "payment-service", ("SPEC_CHANGE",)),
        setup=(EnvPatch("payment-service", {"FAULT_PAYMENT_DELAY_MS": "3000"}),),
        workload=Workload(target=Target.ORDERS, count=30, concurrency=30, waves=4),
        teardown=(EnvUnset("payment-service", ("FAULT_PAYMENT_DELAY_MS",)),),
        tier=Tier.HOLDOUT,
        demo=True,
    ),
    LiveScenario(
        id="dependency_endpoint_misconfig",
        title="Order service points at the wrong payment endpoint",
        alert="OrderErrorRateHigh",
        service="order-service",
        expectation=RootCause("Deployment", "order-service", ("SPEC_CHANGE",)),
        setup=(EnvPatch("order-service", {"PAYMENT_SERVICE_URL": "http://payment-service:9999"}),),
        workload=Workload(target=Target.ORDERS, count=40),
        teardown=(EnvUnset("order-service", ("PAYMENT_SERVICE_URL",)),),
        demo=True,
    ),
)


_CLUSTER_CHANGES: tuple[LiveScenario, ...] = (
    LiveScenario(
        id="configmap_dependency_change",
        title="A ConfigMap edit repoints the payment dependency",
        alert="OrderErrorRateHigh",
        service="order-service",
        expectation=RootCause("ConfigMap", "workload-config", ("CONFIG_CHANGE",)),
        setup=(
            PatchConfigMap(
                "workload-config", {"PAYMENT_SERVICE_URL": "http://payment-service:9999"}
            ),
            RolloutRestart("order-service"),
            WaitRollout("order-service"),
        ),
        workload=Workload(target=Target.ORDERS, count=40),
        teardown=(
            PatchConfigMap(
                "workload-config", {"PAYMENT_SERVICE_URL": "http://payment-service:8000"}
            ),
            RolloutRestart("order-service"),
            WaitRollout("order-service"),
        ),
        demo=True,
    ),
    LiveScenario(
        id="image_regression",
        title="A deploy pins an image tag that does not exist",
        alert="OrderErrorRateHigh",
        service="order-service",
        # A rolling update keeps the healthy pod while the bad-image pod fails to
        # pull, so the pod is deleted to force the broken spec live.  Payment then
        # has no serving pod and the order service records real dependency errors;
        # the engine must trace them to the payment image change.
        expectation=RootCause("Deployment", "payment-service", ("IMAGE_CHANGE",)),
        setup=(
            SetImage("payment-service", "payment-service", "agentic-sre/payment-service:v0"),
            DeletePod("payment-service"),
        ),
        workload=Workload(target=Target.ORDERS, count=40),
        teardown=(
            SetImage("payment-service", "payment-service", "agentic-sre/payment-service:dev"),
            WaitRollout("payment-service"),
        ),
        demo=True,
    ),
    LiveScenario(
        id="dependency_scaled_to_zero",
        title="The payment dependency is scaled to zero",
        alert="OrderErrorRateHigh",
        service="order-service",
        # The demo runs one replica per service, so a scale-down that still
        # leaves a serving pod is not a fault.  Scaling the payment dependency
        # to zero is an unambiguous scale change: the order service records real
        # 5xx as its dependency calls fail, and the engine must name the payment
        # deployment that was scaled away.
        expectation=RootCause("Deployment", "payment-service", ("SCALE_CHANGE",)),
        setup=(Scale("payment-service", 0),),
        workload=Workload(target=Target.ORDERS, count=40),
        teardown=(Scale("payment-service", 1), WaitRollout("payment-service")),
        demo=True,
    ),
    LiveScenario(
        id="rollout_restart_disruption",
        title="A rollout restart disrupts the payment dependency",
        alert="OrderErrorRateHigh",
        service="order-service",
        # A graceful rolling restart at one replica keeps a pod serving, so the
        # restart annotation is recorded but the old pod is deleted to force a
        # real serving gap.  The order service records the dependency errors and
        # the engine must attribute them to the payment restart.
        expectation=RootCause("Deployment", "payment-service", ("ROLLOUT_RESTART",)),
        setup=(RolloutRestart("payment-service"), DeletePod("payment-service")),
        workload=Workload(target=Target.ORDERS, count=40),
        teardown=(WaitRollout("payment-service"),),
    ),
    LiveScenario(
        id="network_policy_isolation",
        title="A NetworkPolicy cuts order from payment",
        alert="OrderErrorRateHigh",
        service="order-service",
        expectation=RootCause(
            "NetworkPolicy", "deny-order-egress", ("POLICY_CREATED", "NETWORK_RESTRICTION")
        ),
        setup=(
            ApplyManifest(
                {
                    "apiVersion": "networking.k8s.io/v1",
                    "kind": "NetworkPolicy",
                    "metadata": {"name": "deny-order-egress", "namespace": NAMESPACE},
                    "spec": {
                        "podSelector": {"matchLabels": {"app": "order-service"}},
                        "policyTypes": ["Egress"],
                        "egress": [],
                    },
                },
                label="NetworkPolicy/deny-order-egress",
            ),
        ),
        workload=Workload(target=Target.ORDERS, count=40),
        teardown=(DeleteObject("networkpolicy", "deny-order-egress"),),
        demo=True,
    ),
    LiveScenario(
        id="quota_blocks_scaling",
        title="A ResourceQuota blocks the pods a scale-up needs",
        alert="OrderRequestLatencyHigh",
        service="order-service",
        expectation=RootCause(
            "ResourceQuota", "sre-demo-pods", ("QUOTA_EXCEEDED", "QUOTA_EXHAUSTED")
        ),
        setup=(
            ApplyManifest(
                {
                    "apiVersion": "v1",
                    "kind": "ResourceQuota",
                    "metadata": {"name": "sre-demo-pods", "namespace": NAMESPACE},
                    "spec": {"hard": {"pods": "8"}},
                },
                label="ResourceQuota/sre-demo-pods",
            ),
            Scale("order-service", 6),
        ),
        workload=Workload(target=Target.ORDERS, count=40, concurrency=20, waves=3),
        teardown=(
            Scale("order-service", 2),
            DeleteObject("resourcequota", "sre-demo-pods"),
        ),
        tier=Tier.HOLDOUT,
    ),
    LiveScenario(
        id="memory_limit_oom",
        title="A tightened memory limit kills the payment container",
        alert="OrderErrorRateHigh",
        service="order-service",
        # A container that OOMs at startup never serves /metrics, so the
        # metric-based runtime-instability alert cannot see it.  Its failure only
        # becomes observable through the dependent order service, which records
        # real errors when payment is down; the engine must trace those to the
        # payment memory-limit change.  The limit must sit at or above the 128Mi
        # request, so both drop together, and the pod is deleted to force the
        # starved spec live past the rolling update.
        expectation=RootCause("Deployment", "payment-service", ("SPEC_CHANGE",)),
        setup=(
            SetResources(
                "payment-service",
                "payment-service",
                limits={"memory": "32Mi"},
                requests={"memory": "32Mi"},
            ),
            DeletePod("payment-service"),
        ),
        workload=Workload(target=Target.ORDERS, count=40),
        teardown=(
            SetResources(
                "payment-service",
                "payment-service",
                limits={"memory": "512Mi"},
                requests={"memory": "128Mi"},
            ),
            WaitRollout("payment-service"),
        ),
        demo=True,
    ),
    LiveScenario(
        id="cpu_limit_throttle",
        title="A tightened CPU limit throttles the service",
        alert="PaymentRequestLatencyHigh",
        service="payment-service",
        # cpu limit must be at or above the 100m request, so both drop together.
        expectation=RootCause(
            "Deployment", "payment-service", ("SPEC_CHANGE", "RESOURCE_PRESSURE")
        ),
        setup=(
            SetResources(
                "payment-service",
                "payment-service",
                limits={"cpu": "50m"},
                requests={"cpu": "50m"},
            ),
        ),
        workload=Workload(target=Target.PAYMENTS, count=30, concurrency=30, waves=3),
        teardown=(
            SetResources(
                "payment-service",
                "payment-service",
                limits={"cpu": "500m"},
                requests={"cpu": "100m"},
            ),
            WaitRollout("payment-service"),
        ),
        tier=Tier.HOLDOUT,
    ),
    LiveScenario(
        id="hpa_replica_cap",
        title="An autoscaler cap prevents scaling under load",
        alert="OrderRequestLatencyHigh",
        service="order-service",
        expectation=RootCause(
            "HorizontalPodAutoscaler", "order-service", ("AUTOSCALING_FAILURE", "SPEC_CHANGE")
        ),
        setup=(
            ApplyManifest(
                {
                    "apiVersion": "autoscaling/v2",
                    "kind": "HorizontalPodAutoscaler",
                    "metadata": {"name": "order-service", "namespace": NAMESPACE},
                    "spec": {
                        "scaleTargetRef": {
                            "apiVersion": "apps/v1",
                            "kind": "Deployment",
                            "name": "order-service",
                        },
                        "minReplicas": 1,
                        "maxReplicas": 1,
                        "metrics": [
                            {
                                "type": "Resource",
                                "resource": {
                                    "name": "cpu",
                                    "target": {"type": "Utilization", "averageUtilization": 50},
                                },
                            }
                        ],
                    },
                },
                label="HorizontalPodAutoscaler/order-service",
            ),
        ),
        workload=Workload(target=Target.ORDERS, count=40, concurrency=40, waves=4),
        teardown=(
            DeleteObject("horizontalpodautoscaler", "order-service"),
            Scale("order-service", 2),
        ),
        tier=Tier.HOLDOUT,
    ),
    LiveScenario(
        id="service_selector_drift",
        title="A Service selector edit drops every endpoint",
        alert="OrderErrorRateHigh",
        service="order-service",
        expectation=RootCause("Service", "payment-service", ("SPEC_CHANGE",)),
        setup=(PatchService("payment-service", {"selector": {"app": "payment-service-retired"}}),),
        workload=Workload(target=Target.ORDERS, count=40),
        teardown=(PatchService("payment-service", {"selector": {"app": "payment-service"}}),),
        demo=True,
    ),
    LiveScenario(
        id="worker_deployment_deleted",
        title="The order worker deployment is deleted",
        alert="OrderWorkerLagHigh",
        service="order-worker",
        expectation=RootCause("Deployment", "order-worker", ("OBJECT_DELETED",)),
        setup=(DeleteObject("deployment", "order-worker"),),
        workload=Workload(target=Target.ORDERS, count=40),
        teardown=(
            ApplyFile("infra/kubernetes/workload.yaml"),
            WaitRollout("order-worker"),
        ),
        tier=Tier.HOLDOUT,
    ),
    LiveScenario(
        id="payment_pod_crash",
        title="The payment process restarts repeatedly",
        alert="PaymentRuntimeInstability",
        service="payment-service",
        # The restart signal the engine attaches varies (container failure,
        # rollout restart, or a lifecycle event), so this scenario asserts the
        # actor and leaves the finding kind unconstrained.
        expectation=RootCause("Deployment", "payment-service"),
        setup=(RestartContainer("payment-service", times=3),),
        workload=NO_WORKLOAD,
        teardown=(WaitRollout("payment-service"),),
        demo=True,
    ),
)


SCENARIOS: tuple[LiveScenario, ...] = _RUNTIME_FAULTS + _SPEC_CHANGES + _CLUSTER_CHANGES

SCENARIO_BY_ID = {scenario.id: scenario for scenario in SCENARIOS}

DEMO_SCENARIOS = tuple(scenario for scenario in SCENARIOS if scenario.demo)


def scenarios_for(
    *, tier: Tier | None = None, demo_only: bool = False, ids: tuple[str, ...] = ()
) -> tuple[LiveScenario, ...]:
    """Select scenarios by tier, demo flag, or explicit id."""
    if ids:
        missing = tuple(item for item in ids if item not in SCENARIO_BY_ID)
        if missing:
            raise KeyError(f"unknown scenarios: {', '.join(missing)}")
        return tuple(SCENARIO_BY_ID[item] for item in ids)
    selected = SCENARIOS
    if tier is not None:
        selected = tuple(item for item in selected if item.tier is tier)
    if demo_only:
        selected = tuple(item for item in selected if item.demo)
    return selected

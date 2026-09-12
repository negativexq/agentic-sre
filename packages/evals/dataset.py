"""Versioned frozen incident ground truth kept outside model context."""

import json
from hashlib import sha256

from pydantic import BaseModel, ConfigDict, Field

from packages.investigation.contracts import HypothesisMechanism


class FrozenIncident(BaseModel):
    """One reproducible benchmark case and its private expected outcome."""

    model_config = ConfigDict(extra="forbid", strict=True)

    scenario_id: str = Field(min_length=1)
    category: str = Field(min_length=1)
    affected_component: str = Field(min_length=1)
    mechanism: HypothesisMechanism
    suspected_trigger: str = Field(min_length=1)
    fixture: str = Field(min_length=1)

    def public_context(self) -> dict[str, str]:
        """Return only scenario facts that may be shown to an investigator."""
        return {"scenario_id": self.scenario_id, "category": self.category, "fixture": self.fixture}


FROZEN_DATASET: tuple[FrozenIncident, ...] = (
    FrozenIncident(
        scenario_id="V020-001",
        category="service_error",
        affected_component="payment-service",
        mechanism=HypothesisMechanism.SERVICE_ERROR_REGRESSION,
        suspected_trigger="payment service returned elevated 5xx responses",
        fixture="payment_error_spike",
    ),
    FrozenIncident(
        scenario_id="V020-002",
        category="service_error",
        affected_component="order-service",
        mechanism=HypothesisMechanism.SERVICE_ERROR_REGRESSION,
        suspected_trigger="order service returned elevated 5xx responses",
        fixture="order_error_spike",
    ),
    FrozenIncident(
        scenario_id="V020-003",
        category="dependency_latency",
        affected_component="payment-service",
        mechanism=HypothesisMechanism.DEPENDENCY_LATENCY,
        suspected_trigger="payment dependency response latency increased",
        fixture="payment_dependency_latency",
    ),
    FrozenIncident(
        scenario_id="V020-004",
        category="service_latency",
        affected_component="order-service",
        mechanism=HypothesisMechanism.SERVICE_LATENCY_REGRESSION,
        suspected_trigger="order request latency increased",
        fixture="order_latency_spike",
    ),
    FrozenIncident(
        scenario_id="V020-005",
        category="database",
        affected_component="payment-service",
        mechanism=HypothesisMechanism.DATABASE_CONNECTION_PRESSURE,
        suspected_trigger="payment database connection pool reached pressure",
        fixture="payment_db_pool_pressure",
    ),
    FrozenIncident(
        scenario_id="V020-006",
        category="database",
        affected_component="order-service",
        mechanism=HypothesisMechanism.DATABASE_QUERY_LATENCY,
        suspected_trigger="order database query latency increased",
        fixture="order_db_query_latency",
    ),
    FrozenIncident(
        scenario_id="V020-007",
        category="kafka",
        affected_component="order-worker",
        mechanism=HypothesisMechanism.KAFKA_CONSUMER_LAG,
        suspected_trigger="order worker consumer lag increased",
        fixture="order_worker_lag",
    ),
    FrozenIncident(
        scenario_id="V020-008",
        category="kafka",
        affected_component="order-worker",
        mechanism=HypothesisMechanism.CONSUMER_FAILURE,
        suspected_trigger="order worker stopped consuming messages",
        fixture="order_worker_failure",
    ),
    FrozenIncident(
        scenario_id="V020-009",
        category="kubernetes",
        affected_component="payment-service",
        mechanism=HypothesisMechanism.POD_CRASH,
        suspected_trigger="payment service pod restarted repeatedly",
        fixture="payment_pod_crash",
    ),
    FrozenIncident(
        scenario_id="V020-010",
        category="deployment",
        affected_component="payment-service",
        mechanism=HypothesisMechanism.CONFIGURATION_REGRESSION,
        suspected_trigger="payment deployment configuration changed",
        fixture="payment_config_change",
    ),
)


def frozen_dataset_hash() -> str:
    """Return the stable content hash used to identify the dataset version."""
    payload = [item.model_dump(mode="json") for item in FROZEN_DATASET]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(encoded).hexdigest()

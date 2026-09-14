"""Integration-only smoke fixture metadata.

This module is intentionally outside the frozen dataset and target mapping.
The smoke fixture exercises the real fault-to-telemetry-to-incident path but
can never become an A1 evaluation scenario.
"""

from pydantic import BaseModel, ConfigDict, Field

from packages.evals.live_fixtures import FixtureDefinition

SMOKE_SCENARIO_ID = "A1_SMOKE_001"
SMOKE_FIXTURE = "a1_smoke_payment_error"
SMOKE_ALERT_NAME = "A1SmokePaymentErrorRateHigh"


class SmokeScenario(BaseModel):
    """Minimal lifecycle input with no evaluator truth fields."""

    model_config = ConfigDict(extra="forbid", strict=True)

    scenario_id: str = Field(min_length=1, max_length=64)
    category: str = Field(min_length=1, max_length=64)
    fixture: str = Field(min_length=1, max_length=100)


SMOKE_SCENARIO = SmokeScenario(
    scenario_id=SMOKE_SCENARIO_ID,
    category="integration_smoke",
    fixture=SMOKE_FIXTURE,
)
SMOKE_FIXTURE_DEFINITION = FixtureDefinition(
    fixture=SMOKE_FIXTURE,
    alert_name=SMOKE_ALERT_NAME,
    service="payment-service",
    primary_tools=("service_error_rate",),
)

__all__ = [
    "SMOKE_ALERT_NAME",
    "SMOKE_FIXTURE",
    "SMOKE_FIXTURE_DEFINITION",
    "SMOKE_SCENARIO",
    "SMOKE_SCENARIO_ID",
    "SmokeScenario",
]

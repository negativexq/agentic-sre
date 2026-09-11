"""Foundation-release action policy evaluator."""

from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from packages.contracts import ActionRequest, PolicyDecision, PolicyEvaluation


class PolicyConfig:
    """Explicit release policy configuration."""

    def __init__(self, *, writes_enabled: bool = False, version: str = "v0.1.0-foundation") -> None:
        self.writes_enabled = writes_enabled
        self.version = version


class PolicyEvaluator:
    """Deny unknown, malformed, unavailable, and foundation write actions."""

    def __init__(self, config: PolicyConfig | None) -> None:
        self._config = config

    def evaluate(self, action: ActionRequest) -> PolicyEvaluation:
        """Evaluate a typed action; foundation defaults to DENY."""
        if self._config is None or not self._config.writes_enabled:
            reason = (
                "policy configuration missing"
                if self._config is None
                else "writes disabled by release policy"
            )
            version = "unavailable" if self._config is None else self._config.version
            return PolicyEvaluation(
                decision=PolicyDecision.DENY,
                reason=reason,
                evaluated_at=datetime.now(UTC),
                policy_version=version,
            )
        return PolicyEvaluation(
            decision=PolicyDecision.ALLOW,
            reason="action is allowed by configured policy",
            evaluated_at=datetime.now(UTC),
            policy_version=self._config.version,
        )

    def evaluate_raw(self, raw: Any) -> PolicyEvaluation:
        """Validate untrusted input and fail closed on malformed actions."""
        try:
            action = ActionRequest.model_validate(raw)
        except (ValidationError, TypeError, ValueError):
            return PolicyEvaluation(
                decision=PolicyDecision.DENY,
                reason="malformed or unknown action",
                evaluated_at=datetime.now(UTC),
                policy_version=self._config.version if self._config else "unavailable",
            )
        return self.evaluate(action)

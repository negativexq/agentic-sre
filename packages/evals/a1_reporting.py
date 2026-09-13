"""Pure reporting helpers for comparing A1-native output with A0 facts."""

from typing import Any

from packages.evals.a1_graders import A1Rate


def a0_compatibility_baseline() -> dict[str, Any]:
    """Return only v0.2 metrics whose denominator and meaning are documented.

    Legacy service and trigger values are retained as non-comparable facts. They
    are not presented as canonical A1 causal-component or structured-trigger
    baselines.
    """
    return {
        "baseline": {
            "release": "v0.2.0",
            "evaluated_runtime_sha": "4603fc6380d672b0a3e9d885885b879f9b32c407",
        },
        "comparable": {
            "completion_rate": A1Rate(numerator=9, denominator=10, rate=0.9),
            "mechanism_accuracy": A1Rate(numerator=8, denominator=10, rate=0.8),
            "cross_component_exploration": A1Rate(numerator=0, denominator=1, rate=0.0),
            "submitted_hypothesis_reference_integrity": A1Rate(
                numerator=9, denominator=9, rate=1.0
            ),
        },
        "legacy_non_comparable": {
            "service_exact_match": {
                "numerator": 4,
                "denominator": 10,
                "rate": 0.4,
                "reason": "free-text affected_component equality",
            },
            "trigger_exact_match": {
                "numerator": 0,
                "denominator": 10,
                "rate": 0.0,
                "reason": "free-text suspected_trigger equality",
            },
        },
    }


__all__ = ["a0_compatibility_baseline"]

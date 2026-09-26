"""M19-6.4: workload readiness probes /ready, liveness probes /health."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

MANIFEST = Path(__file__).resolve().parents[2] / "infra" / "kubernetes" / "workload.yaml"
# Chosen from measured start-to-listen time (<= 3.3 s) plus a 2 s margin.
READINESS_INITIAL_DELAY_SECONDS = 6


def _container(deployment: str) -> dict[str, Any]:
    for document in yaml.safe_load_all(MANIFEST.read_text(encoding="utf-8")):
        if (
            document
            and document.get("kind") == "Deployment"
            and document["metadata"]["name"] == deployment
        ):
            (container,) = document["spec"]["template"]["spec"]["containers"]
            return dict(container)
    raise AssertionError(f"no Deployment {deployment}")


@pytest.mark.parametrize("deployment", ["order-service", "payment-service"])
def test_readiness_and_liveness_use_separate_endpoints(deployment: str) -> None:
    container = _container(deployment)
    assert container["readinessProbe"] == {
        "httpGet": {"path": "/ready", "port": 8000},
        "initialDelaySeconds": READINESS_INITIAL_DELAY_SECONDS,
        "periodSeconds": 5,
        "failureThreshold": 2,
        "successThreshold": 1,
    }
    assert container["livenessProbe"] == {
        "httpGet": {"path": "/health", "port": 8000},
        "periodSeconds": 10,
        "failureThreshold": 6,
    }
    assert {"containerPort": 8000} in container["ports"]

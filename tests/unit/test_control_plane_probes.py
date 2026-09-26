"""M19-6.12b: no control-plane probe runs before the server listens."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

MANIFEST = Path(__file__).resolve().parents[2] / "infra" / "kubernetes" / "control-plane.yaml"
# Measured start-to-listen ~3.8 s (two Kind clusters sharing the CPU) plus margin.
INITIAL_DELAY_SECONDS = 8


def _container() -> dict[str, Any]:
    for document in yaml.safe_load_all(MANIFEST.read_text(encoding="utf-8")):
        if document and document.get("kind") == "Deployment":
            (container,) = document["spec"]["template"]["spec"]["containers"]
            return dict(container)
    raise AssertionError("no control-plane Deployment")


def test_probes_wait_until_the_server_listens() -> None:
    container = _container()
    assert container["readinessProbe"] == {
        "httpGet": {"path": "/ready", "port": 8000},
        "initialDelaySeconds": INITIAL_DELAY_SECONDS,
    }
    assert container["livenessProbe"] == {
        "httpGet": {"path": "/health", "port": 8000},
        "initialDelaySeconds": INITIAL_DELAY_SECONDS,
    }

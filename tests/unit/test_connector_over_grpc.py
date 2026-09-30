"""The transport-independent suites of §7 and §10, run again over gRPC with mTLS (contract §12.8).

The assertions are the ones of the in-process tests, unchanged: only the transport underneath
``ConnectorClient`` differs. Any divergence between the two is a defect.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import test_connector_boundary as boundary
import test_connector_streams as streams
from connector_harness import GrpcHarness


@pytest.fixture(autouse=True)
def over_grpc(monkeypatch: pytest.MonkeyPatch) -> Iterator[GrpcHarness]:
    harness = GrpcHarness()
    monkeypatch.setattr(boundary, "in_process_transport", harness.attach)
    monkeypatch.setattr(streams, "in_process_transport", harness.attach)
    yield harness
    harness.close()


globals().update(
    {
        name: value
        for module in (boundary, streams)
        for name, value in vars(module).items()
        if name.startswith("test_")
    }
)

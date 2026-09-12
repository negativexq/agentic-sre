"""Offline tests for Kubernetes event scoping."""

from types import SimpleNamespace

from packages.tools.live_backends import KubernetesBackend


def _event(uid: str, object_name: str, reason: str) -> SimpleNamespace:
    return SimpleNamespace(
        metadata=SimpleNamespace(uid=uid),
        involved_object=SimpleNamespace(name=object_name),
        reason=reason,
        message=f"{reason} event",
        type="Normal",
        last_timestamp="2026-09-12T12:00:00Z",
    )


def test_kubernetes_events_include_pod_scoped_events() -> None:
    selectors: list[str] = []
    pod = SimpleNamespace(metadata=SimpleNamespace(name="payment-service-abc"))
    events = {
        "payment-service-abc": [_event("pod-1", "payment-service-abc", "Started")],
        "payment-service": [_event("deployment-1", "payment-service", "ScalingReplicaSet")],
    }

    class FakeCore:
        def list_namespaced_pod(self, _namespace: str, *, label_selector: str) -> SimpleNamespace:
            assert label_selector == "app=payment-service"
            return SimpleNamespace(items=[pod])

        def list_namespaced_event(self, _namespace: str, *, field_selector: str) -> SimpleNamespace:
            selectors.append(field_selector)
            name = field_selector.split("=", 1)[1]
            return SimpleNamespace(items=events[name])

    class FakeApps:
        def read_namespaced_deployment(self, _name: str, _namespace: str) -> SimpleNamespace:
            return SimpleNamespace()

    backend = KubernetesBackend()
    backend._core = FakeCore()
    backend._apps = FakeApps()

    result = backend.query("get_events", {"deployment": "payment-service"})

    assert set(selectors) == {
        "involvedObject.name=payment-service-abc",
        "involvedObject.name=payment-service",
    }
    assert {item["involved_object"] for item in result["records"]} == {
        "payment-service-abc",
        "payment-service",
    }
    assert result["__temporal_mode"] == "HISTORICAL_EVENT"

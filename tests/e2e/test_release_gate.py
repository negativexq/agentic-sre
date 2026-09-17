"""Static assertions for the deterministic v0.1.0 release boundary."""

import re
from pathlib import Path


def test_kubernetes_investigation_role_has_zero_write_verbs() -> None:
    manifests = (
        "infra/kubernetes/tools-rbac.yaml",
        "infra/kubernetes/chaos-mesh-rbac.yaml",
    )
    checked = 0
    for manifest in manifests:
        content = Path(manifest).read_text(encoding="utf-8")
        verb_sets = re.findall(r"verbs:\s*\[([^]]+)\]", content)
        assert verb_sets, manifest
        checked += len(verb_sets)
        for verbs in verb_sets:
            assert {verb.strip().strip('"') for verb in verbs.split(",")} <= {
                "get",
                "list",
                "watch",
            }, manifest
    assert checked >= 6


def test_release_contains_required_observability_and_runtime_boundaries() -> None:
    required_files = (
        "infra/observability/otel-collector.yaml",
        "infra/observability/prometheus-rules.yml",
        "infra/observability/loki.yml",
        "infra/observability/tempo.yml",
        "infra/observability/grafana-dashboard.json",
        "infra/kubernetes/control-plane.yaml",
    )
    assert all(Path(path).is_file() for path in required_files)
    rules = Path("infra/observability/prometheus-rules.yml").read_text(encoding="utf-8")
    for alert_name in (
        "HighErrorRate",
        "HighRequestLatency",
        "KafkaConsumerLag",
        "PodRestartSpike",
        "DBConnectionPressure",
    ):
        assert f"alert: {alert_name}" in rules


def test_python_source_contains_no_agent_runtime_imports() -> None:
    forbidden = ("langgraph", "langchain", "anthropic", "crewai", "autogen", "mcp")
    source_files = (
        list(Path("apps").rglob("*.py"))
        + list(Path("packages").rglob("*.py"))
        + list(Path("workload").rglob("*.py"))
    )
    source = "\n".join(path.read_text(encoding="utf-8").lower() for path in source_files)
    assert all(token not in source for token in forbidden)

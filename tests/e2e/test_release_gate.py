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


def test_secure_api_overlay_wires_the_token_without_committing_a_credential() -> None:
    overlay = Path("infra/kubernetes/secure-api-auth")
    assert (overlay / "kustomization.yaml").is_file()
    secrets = (overlay / "api-secrets.yaml").read_text(encoding="utf-8")
    control_plane = (overlay / "control-plane-env-patch.yaml").read_text(encoding="utf-8")
    alertmanager = (overlay / "alertmanager-config-patch.yaml").read_text(encoding="utf-8")
    volume = (overlay / "alertmanager-volume-patch.yaml").read_text(encoding="utf-8")
    assert secrets.count("name: agentic-sre-api") == 2
    assert secrets.count("REPLACE_BEFORE_APPLY") == 2
    assert "SRE_API_TOKEN" in control_plane and "secretKeyRef" in control_plane
    assert "credentials_file: /etc/alertmanager/api-token/token" in alertmanager
    assert "secretName: agentic-sre-api" in volume


def test_python_source_contains_no_agent_runtime_imports() -> None:
    forbidden = ("langgraph", "langchain", "anthropic", "crewai", "autogen", "mcp")
    source_files = (
        list(Path("apps").rglob("*.py"))
        + list(Path("packages").rglob("*.py"))
        + list(Path("workload").rglob("*.py"))
    )
    source = "\n".join(path.read_text(encoding="utf-8").lower() for path in source_files)
    assert all(token not in source for token in forbidden)

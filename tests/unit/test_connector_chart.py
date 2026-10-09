"""The Connector's Helm chart renders read-only RBAC and refuses bad values (connector-install-design §A8.4)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

CHART = Path(__file__).resolve().parents[2] / "charts" / "agentic-sre-connector"
pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")

VALUES = [
    "controlPlane.endpoint=cp.example.com:8443",
    "controlPlane.enrollEndpoint=cp.example.com:8444",
    "watch.namespaces={shop,payments}",
    "watch.evidenceNamespaces={chaos-mesh}",
]


def render(*extra: str) -> list[dict[str, Any]]:
    args = ["helm", "template", "conn", str(CHART), "--namespace", "connector"]
    for value in (*VALUES, *extra):
        args += ["--set", value]
    out = subprocess.run(args, capture_output=True, text=True, check=True).stdout
    return [doc for doc in yaml.safe_load_all(out) if doc]


def refused(*values: str) -> str:
    args = ["helm", "template", "conn", str(CHART)]
    for value in values:
        args += ["--set", value]
    result = subprocess.run(args, capture_output=True, text=True)
    assert result.returncode != 0
    return result.stderr


def test_every_watched_namespace_gets_read_only_access_and_never_secrets() -> None:
    roles = [d for d in render() if d["kind"] == "Role" and d["metadata"]["name"] == "conn-reader"]
    assert {r["metadata"]["namespace"] for r in roles} == {"shop", "payments", "chaos-mesh"}
    for role in roles:
        for rule in role["rules"]:
            assert set(rule["verbs"]) == {"get", "list", "watch"}
            assert "secrets" not in rule["resources"]
    chaos = next(r for r in roles if r["metadata"]["namespace"] == "chaos-mesh")
    assert [rule["apiGroups"] for rule in chaos["rules"]] == [
        [""],
        ["chaos-mesh.org"],
    ]  # events + chaos only


def test_its_own_identity_is_one_secret_by_name_get_and_update_only() -> None:
    docs = render()
    (role,) = [
        d for d in docs if d["kind"] == "Role" and d["metadata"]["name"] == "conn-credentials"
    ]
    assert role["metadata"]["namespace"] == "connector"
    assert role["rules"] == [
        {
            "apiGroups": [""],
            "resources": ["secrets"],
            "resourceNames": ["conn-credentials"],
            "verbs": ["get", "update"],
        }
    ]
    assert not [d for d in docs if d["kind"] in ("ClusterRole", "ClusterRoleBinding")]


def test_chaos_kinds_beside_workloads_can_be_turned_off() -> None:
    roles = render("watch.chaosBesideWorkloads=false")
    shop = next(d for d in roles if d["kind"] == "Role" and d["metadata"]["namespace"] == "shop")
    assert all("chaos-mesh.org" not in rule["apiGroups"] for rule in shop["rules"])


def test_the_deployment_runs_unprivileged_and_restores_its_identity_from_the_secret() -> None:
    docs = render("enrollment.token=prod-eu.secret.AAAA")
    (deployment,) = [d for d in docs if d["kind"] == "Deployment"]
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    (config,) = [d for d in docs if d["kind"] == "ConfigMap"]
    assert config["data"]["SRE_CONNECTOR_CREDENTIAL_SECRET"] == "conn-credentials"
    assert config["data"]["SRE_WATCH_NAMESPACES"] == "shop,payments"
    (enrollment,) = [
        d for d in docs if d["kind"] == "Secret" and d["metadata"]["name"] == "conn-enrollment"
    ]
    assert enrollment["stringData"]["token"] == "prod-eu.secret.AAAA"


def test_bad_values_are_refused_by_the_schema() -> None:
    assert "endpoint" in refused("watch.namespaces={shop}")  # no control plane
    assert refused(*VALUES[:2], "watch.namespaces={Shop_1}")  # not a namespace name
    assert refused(*VALUES, "enrollment.token=not-a-token")
    assert refused(*VALUES[:2])  # at least one watched namespace
    assert render(*VALUES[:3])  # the evidence list may be empty

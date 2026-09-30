"""The lab's recorded inputs stay what the design approved (testbed lab design §4, §5, B0)."""

from __future__ import annotations

import hashlib
import re
import tarfile
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
CHAOS = ROOT / "infra/kubernetes/chaos-mesh"
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def load(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_the_vendored_chart_is_the_pinned_one() -> None:
    pins = load(CHAOS / "pins.yaml")
    archive = CHAOS / pins["chart"]["file"]
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == pins["chart"]["sha256"]
    with tarfile.open(archive) as tar:
        member = tar.extractfile("chaos-mesh/Chart.yaml")
        assert member is not None
        chart = yaml.safe_load(member.read())
    assert chart["name"] == pins["chart"]["name"] == "chaos-mesh"
    assert chart["version"] == pins["chart"]["version"] == "2.8.4"
    assert chart["appVersion"] == pins["chart"]["version"]


def test_every_pinned_image_carries_a_digest_and_the_chart_version_tag() -> None:
    images = load(CHAOS / "pins.yaml")["images"]
    assert {i["name"] for i in images} == {
        "ghcr.io/chaos-mesh/chaos-mesh",
        "ghcr.io/chaos-mesh/chaos-daemon",
    }
    for image in images:
        assert image["tag"] == "v2.8.4" and DIGEST.fullmatch(image["digest"])


def test_the_values_match_a_containerd_node_with_the_smallest_footprint() -> None:
    values = load(CHAOS / "values.yaml")
    assert values["chaosDaemon"] == {
        "runtime": "containerd",
        "socketPath": "/run/containerd/containerd.sock",
    }
    assert values["controllerManager"]["replicaCount"] == 1
    assert values["dashboard"]["create"] is False
    assert values["dnsServer"]["create"] is False
    assert values["enableProfiling"] is False


def test_the_kind_cluster_is_one_pinned_node_with_no_published_ports() -> None:
    config = load(ROOT / "infra/kubernetes/kind-config.yaml")
    (node,) = config["nodes"]
    assert node["role"] == "control-plane"
    assert re.fullmatch(r"kindest/node:v1\.37\.0@sha256:[0-9a-f]{64}", node["image"])
    assert "extraPortMappings" not in node and "extraPortMappings" not in config


def test_the_makefile_installs_the_vendored_chart_with_the_recorded_values() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "chaos-mesh-$(CHAOS_TAG:v%=%).tgz" in makefile
    assert "-f $(CHAOS_DIR)/values.yaml" in makefile
    assert "docker pull $$image@$$digest" in makefile  # images are pulled by digest


def test_kafka_declares_its_topic_and_keeps_its_data_across_a_container_restart() -> None:
    documents = list(yaml.safe_load_all((ROOT / "infra/kubernetes/dependencies.yaml").read_text()))
    kafka = next(
        d for d in documents if d and d["kind"] == "Deployment" and d["metadata"]["name"] == "kafka"
    )
    pod = kafka["spec"]["template"]["spec"]
    containers = {c["name"]: c for c in pod["containers"]}
    assert set(containers) == {"kafka", "topics"}
    env = {e["name"]: e["value"] for e in containers["kafka"]["env"]}
    assert env["KAFKA_LOG_DIRS"] == "/var/lib/kafka/data"
    assert {"name": "kafka-data", "emptyDir": {}} in pod["volumes"]
    topics_env = {e["name"]: e["value"] for e in containers["topics"]["env"]}
    assert topics_env["TOPICS"] == "orders.created"
    assert "--if-not-exists" in containers["topics"]["args"][0]

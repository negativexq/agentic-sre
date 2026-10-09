"""connectorctl preflight: every backend reachable, every permission exactly right (connector-install-design §A8.1)."""

from __future__ import annotations

import socket
import ssl
import threading
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.error import URLError

import pytest

from packages.connector import ctl
from packages.connector.client import ConnectorClient, in_process_transport
from packages.connector.pki import issue_connector, issue_server, new_ca, write_identity
from packages.connector.preflight import (
    CHAOS_RESOURCES,
    READ_VERBS,
    WORKLOAD_RESOURCES,
    Check,
    backend_checks,
    control_plane_check,
    failed,
    kubernetes_checks,
    run_preflight,
)
from packages.connector.service import Connector
from packages.rca.provider_adapter import ProviderReaders

# ---- Kubernetes permissions ---------------------------------------------------------------------------------


class Review:
    """Allows exactly the (namespace, group, resource, verb) tuples it is given."""

    def __init__(self, allowed: set[tuple[str, str, str, str]], *, down: bool = False) -> None:
        self.granted, self.down = allowed, down

    def version(self) -> str:
        if self.down:
            raise ConnectionError("connection refused")
        return "v1.31.0"

    def allowed(self, namespace: str, group: str, resource: str, verb: str) -> bool:
        return (namespace, group, resource, verb) in self.granted


def reads(namespace: str, resources: tuple[tuple[str, str], ...]) -> set[tuple[str, str, str, str]]:
    return {(namespace, g, r, v) for g, r in resources for v in READ_VERBS}


EXACT = reads("shop", WORKLOAD_RESOURCES) | reads("chaos-mesh", CHAOS_RESOURCES)


def statuses(checks: list[Check]) -> dict[str, str]:
    return {check.name: check.status for check in checks}


def test_exactly_the_reads_needed_pass_in_every_namespace() -> None:
    checks = kubernetes_checks(Review(EXACT), ["shop"], ["chaos-mesh"])
    assert statuses(checks) == {
        "kubernetes": "ok",
        "read:shop": "ok",
        "read-only:shop": "ok",
        "read:chaos-mesh": "ok",
        "read-only:chaos-mesh": "ok",
    }


def test_a_missing_read_is_named() -> None:
    granted = EXACT - {("shop", "apps", "deployments", "watch")}
    (read,) = [c for c in kubernetes_checks(Review(granted), ["shop"], []) if c.name == "read:shop"]
    assert read.status == "failed" and "watch apps/deployments" in read.detail


def test_a_write_or_a_secret_makes_the_connector_broader_than_read_only() -> None:
    for extra in (("shop", "", "pods", "delete"), ("shop", "", "secrets", "get")):
        checks = kubernetes_checks(Review(EXACT | {extra}), ["shop"], [])
        (only,) = [c for c in checks if c.name == "read-only:shop"]
        assert only.status == "failed" and "broader than read-only" in only.detail
        assert failed(checks)


def test_chaos_kinds_are_required_in_evidence_namespaces_and_optional_beside_workloads() -> None:
    no_chaos = reads("shop", WORKLOAD_RESOURCES)
    checks = statuses(kubernetes_checks(Review(no_chaos), ["shop"], ["chaos-mesh"]))
    assert checks["read:shop"] == "ok" and checks["read:chaos-mesh"] == "failed"
    some = no_chaos | reads("shop", CHAOS_RESOURCES[:2])
    assert statuses(kubernetes_checks(Review(some), ["shop"], []))["read:shop"] == "warning"


def test_preflight_checks_the_namespaces_the_connector_reads() -> None:
    # the chart sets SRE_EVIDENCE_NAMESPACES="" when none are listed; the Connector then reads none,
    # so preflight must not check chaos-mesh (found in the D4 lab run)
    env = {"SRE_WATCH_NAMESPACES": "shop", "SRE_EVIDENCE_NAMESPACES": ""}
    checks = statuses(
        run_preflight(env, review=Review(reads("shop", WORKLOAD_RESOURCES)), get=fake_get({}))
    )
    assert "read:chaos-mesh" not in checks and checks["read:shop"] == "ok"
    unset = statuses(
        run_preflight({"SRE_WATCH_NAMESPACES": "shop"}, review=Review(set()), get=fake_get({}))
    )
    assert "read:chaos-mesh" in unset


def test_an_unreachable_api_or_no_cluster_access_is_reported_as_such() -> None:
    (down,) = kubernetes_checks(Review(set(), down=True), ["shop"], [])
    assert down.status == "failed" and "unreachable" in down.detail
    assert kubernetes_checks(None, ["shop"], []) == [Check("kubernetes", "not_configured")]


# ---- backends ------------------------------------------------------------------------------------------------


def fake_get(answers: Mapping[str, tuple[int, bytes] | Exception]):  # type: ignore[no-untyped-def]
    seen: list[tuple[str, dict[str, str]]] = []

    def get(url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
        seen.append((url, dict(headers)))
        for prefix, answer in answers.items():
            if url.startswith(prefix):
                if isinstance(answer, Exception):
                    raise answer
                return answer
        return 200, b'{"status":"success"}'

    get.seen = seen  # type: ignore[attr-defined]
    return get


ENV = {
    "SRE_ALERTMANAGER_URL": "http://am:9093",
    "SRE_ALERTMANAGER_TOKEN": "am-token",
    "PROMETHEUS_URL": "http://prom:9090",
    "SRE_LOKI_URL": "http://loki:3100",
    "TEMPO_URL": "http://tempo:3200",
}


def test_every_configured_backend_is_probed_with_its_own_credentials() -> None:
    get = fake_get({})
    assert statuses(backend_checks(ENV, get)) == {
        "alertmanager": "ok",
        "prometheus": "ok",
        "loki": "ok",
        "tempo": "ok",
    }
    am = [h for u, h in get.seen if u.startswith("http://am:9093")]
    assert am and am[0]["Authorization"] == "Bearer am-token"


def test_refused_credentials_unreachable_hosts_and_wrong_answers_fail_by_name() -> None:
    get = fake_get(
        {
            "http://am:9093": (401, b""),
            "http://prom:9090/api/v1/query": (200, b'{"status":"error"}'),
            "http://loki:3100": URLError("no route to host"),
        }
    )
    checks = {c.name: c for c in backend_checks(ENV, get)}
    assert "credentials are refused" in checks["alertmanager"].detail
    assert "unexpected answer" in checks["prometheus"].detail
    assert "unreachable" in checks["loki"].detail
    assert checks["tempo"].status == "ok"


def test_an_unset_backend_is_not_configured_and_does_not_fail() -> None:
    checks = backend_checks({}, fake_get({}))
    assert {c.status for c in checks} == {"not_configured"} and not failed(checks)


# ---- the control plane: a real mutual TLS handshake ------------------------------------------------------------


@pytest.fixture
def control_plane(tmp_path: Path) -> Iterator[dict[str, str]]:
    ca = new_ca()
    server = issue_server(ca, ["localhost", "127.0.0.1"])
    (tmp_path / "ca.crt").write_bytes(ca.certificate)
    server_crt, server_key = write_identity(server, tmp_path, "server")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(server_crt, server_key)
    context.load_verify_locations(tmp_path / "ca.crt")
    context.verify_mode = ssl.CERT_REQUIRED
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    stop = threading.Event()

    def serve() -> None:
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            try:
                with context.wrap_socket(conn, server_side=True) as accepted:
                    accepted.settimeout(3)
                    accepted.recv(1)  # like a gRPC server: keep the connection, wait for the client
            except (ssl.SSLError, OSError):
                pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    env = {
        "SRE_CONNECTOR_ENDPOINT": f"127.0.0.1:{port}",
        "SRE_CONNECTOR_TLS_CA": str(tmp_path / "ca.crt"),
        "SRE_CONNECTOR_SERVER_NAME": "localhost",
        "_dir": str(tmp_path),
    }
    env["_ca_pem"] = ca.certificate.decode()
    yield env | {"_ca_key": ca.private_key.decode()}
    stop.set()
    thread.join(timeout=2)
    listener.close()


def client_identity(env: dict[str, str], *, days: int = 90, trusted: bool = True) -> dict[str, str]:
    from packages.connector.pki import Identity

    ca = (
        Identity(env["_ca_pem"].encode(), env["_ca_key"].encode())
        if trusted
        else new_ca("rogue CA")
    )
    crt, key = write_identity(
        issue_connector(ca, "lab", days=days), Path(env["_dir"]), f"client-{days}-{trusted}"
    )
    return env | {"SRE_CONNECTOR_TLS_CERT": str(crt), "SRE_CONNECTOR_TLS_KEY": str(key)}


def test_the_handshake_succeeds_with_the_connectors_identity(control_plane: dict[str, str]) -> None:
    check = control_plane_check(client_identity(control_plane))
    assert check.status == "ok" and "valid for 89 days" in check.detail


def test_a_certificate_close_to_expiry_warns_and_an_expired_one_fails(
    control_plane: dict[str, str],
) -> None:
    env = client_identity(control_plane)
    soon = datetime.now(UTC) + timedelta(days=80)
    assert control_plane_check(env, now=soon).status == "warning"
    assert control_plane_check(env, now=soon + timedelta(days=30)).status == "failed"


def test_an_untrusted_client_certificate_is_refused(control_plane: dict[str, str]) -> None:
    check = control_plane_check(client_identity(control_plane, trusted=False))
    assert check.status == "failed"


def test_no_endpoint_is_not_configured_and_missing_files_fail() -> None:
    assert control_plane_check({}).status == "not_configured"
    assert control_plane_check({"SRE_CONNECTOR_ENDPOINT": "cp:8443"}).status == "failed"


# ---- the wire op and the command ------------------------------------------------------------------------------


def test_the_wire_op_carries_the_checks_and_fills_reachability() -> None:
    connector = Connector(
        providers=ProviderReaders.from_urls(prometheus_url="http://prom:9090"),
        preflight_runner=lambda: [Check("prometheus", "failed", "HTTP 503")],
    )
    answer = ConnectorClient(in_process_transport(connector)).call("preflight")
    backends = {b["name"]: b for b in answer["backends"]}
    assert backends["prometheus"] == {"name": "prometheus", "configured": True, "reachable": False}
    assert backends["loki"]["reachable"] is None  # not configured: nothing to say
    assert answer["checks"] == [{"name": "prometheus", "status": "failed", "detail": "HTTP 503"}]


def test_the_command_exits_non_zero_when_a_check_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(ctl, "run_preflight", lambda **_: [Check("loki", "failed", "HTTP 500")])
    assert ctl.main(["preflight"]) == 1
    assert "loki" in capsys.readouterr().out
    monkeypatch.setattr(ctl, "run_preflight", lambda **_: [Check("loki", "not_configured")])
    assert ctl.main(["preflight", "--json"]) == 0


def test_against_the_real_grpc_gateway_a_trusted_identity_passes_and_a_foreign_one_fails(
    tmp_path: Path,
) -> None:
    from connector_harness import GrpcHarness

    harness = GrpcHarness()
    try:
        (tmp_path / "ca.crt").write_bytes(harness.ca.certificate)
        base = {
            "SRE_CONNECTOR_ENDPOINT": f"127.0.0.1:{harness.port}",
            "SRE_CONNECTOR_TLS_CA": str(tmp_path / "ca.crt"),
            "SRE_CONNECTOR_SERVER_NAME": "localhost",
        }
        for name, ca, expected in (
            ("trusted", harness.ca, "ok"),
            ("foreign", new_ca("rogue"), "failed"),
        ):
            crt, key = write_identity(issue_connector(ca, "lab-0"), tmp_path, name)
            env = base | {"SRE_CONNECTOR_TLS_CERT": str(crt), "SRE_CONNECTOR_TLS_KEY": str(key)}
            assert control_plane_check(env).status == expected, name
    finally:
        harness.close()

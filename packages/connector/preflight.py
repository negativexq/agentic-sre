"""Connector preflight: is every backend reachable and every permission exactly right (roadmap A8.1).

docs/architecture/connector-install-design.md §A8.1. Each check is bounded by a timeout and reports ``ok``,
``failed`` with the reason, ``warning`` (usable, with a caveat), or ``not_configured``. Kubernetes permissions are
checked both ways: every read the Connector needs must be allowed, and writes and Secrets in the watched namespaces
must be *denied* (a Connector broader than read-only is a failure). The probes read status endpoints and bounded
queries only; no incident data is returned.
"""

from __future__ import annotations

import importlib
import json
import os
import socket
import ssl
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

Status = Literal["ok", "failed", "warning", "not_configured"]
EXPIRY_WARNING = timedelta(days=14)

# (API group, resource) the Connector reads in every watched namespace (packages/rca/live.py), events included.
WORKLOAD_RESOURCES: tuple[tuple[str, str], ...] = (
    ("", "configmaps"),
    ("", "services"),
    ("", "pods"),
    ("", "resourcequotas"),
    ("", "limitranges"),
    ("", "events"),
    ("apps", "deployments"),
    ("apps", "statefulsets"),
    ("apps", "daemonsets"),
    ("apps", "replicasets"),
    ("networking.k8s.io", "networkpolicies"),
    ("autoscaling", "horizontalpodautoscalers"),
)
CHAOS_RESOURCES: tuple[tuple[str, str], ...] = tuple(
    ("chaos-mesh.org", plural)
    for plural in ("networkchaos", "podchaos", "stresschaos", "iochaos", "httpchaos", "schedules")
)
READ_VERBS = ("get", "list", "watch")
WRITE_VERBS = ("create", "update", "patch", "delete")


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str = ""


class AccessReview(Protocol):
    """What preflight asks the Kubernetes API (a ``SelfSubjectAccessReview`` per question)."""

    def version(self) -> str: ...

    def allowed(self, namespace: str, group: str, resource: str, verb: str) -> bool: ...


HttpGet = Callable[[str, Mapping[str, str], float], tuple[int, bytes]]


def http_get(url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
    """A bounded GET; an HTTP error status is returned, a transport error raised."""
    request = Request(url, headers=dict(headers), method="GET")  # noqa: S310 (configured backends only)
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            return int(response.status), response.read(65536)
    except HTTPError as error:
        return int(error.code), b""


def _auth(token: str | None, tenant: str | None = None) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if tenant:
        headers["X-Scope-OrgID"] = tenant
    return headers


def _probe(
    name: str,
    base: str | None,
    steps: Sequence[tuple[str, Callable[[bytes], bool] | None]],
    headers: Mapping[str, str],
    timeout: float,
    get: HttpGet,
) -> Check:
    """Each step is a path and an optional check of its body; the first failure names the step."""
    if not base:
        return Check(name, "not_configured")
    for path, accept in steps:
        url = base.rstrip("/") + path
        try:
            status, body = get(url, headers, timeout)
        except (URLError, OSError, TimeoutError) as error:
            return Check(name, "failed", f"{path}: unreachable ({type(error).__name__}: {error})")
        if status in (401, 403):
            return Check(name, "failed", f"{path}: HTTP {status}, the credentials are refused")
        if status != 200:
            return Check(name, "failed", f"{path}: HTTP {status}")
        if accept is not None and not accept(body):
            return Check(name, "failed", f"{path}: unexpected answer")
    return Check(name, "ok", "reachable, credentials accepted")


def _json_success(body: bytes) -> bool:
    try:
        return bool(json.loads(body).get("status") == "success")
    except (ValueError, AttributeError):
        return False


def backend_checks(
    env: Mapping[str, str], get: HttpGet = http_get, now: datetime | None = None
) -> list[Check]:
    """Alertmanager, Prometheus, Loki and Tempo, with the Connector's own configuration and credentials."""
    now = now or datetime.now(UTC)
    end, start = int(now.timestamp()), int((now - timedelta(minutes=5)).timestamp())
    window = urlencode({"start": f"{start}000000000", "end": f"{end}000000000"})
    return [
        _probe(
            "alertmanager",
            env.get("SRE_ALERTMANAGER_URL"),
            [("/api/v2/status", None)],
            _auth(env.get("SRE_ALERTMANAGER_TOKEN")),
            float(env.get("SRE_ALERTMANAGER_TIMEOUT_SECONDS", "5")),
            get,
        ),
        _probe(
            "prometheus",
            env.get("PROMETHEUS_URL"),
            [("/-/ready", None), ("/api/v1/query?query=up", _json_success)],
            _auth(env.get("PROMETHEUS_BEARER_TOKEN"), env.get("PROMETHEUS_TENANT_ID")),
            float(env.get("PROMETHEUS_TIMEOUT_SECONDS", "5")),
            get,
        ),
        _probe(
            "loki",
            env.get("SRE_LOKI_URL"),
            [("/ready", None), (f"/loki/api/v1/labels?{window}", _json_success)],
            _auth(None),
            5.0,
            get,
        ),
        _probe(
            "tempo",
            env.get("TEMPO_URL"),
            [("/ready", None), (f"/api/search?limit=1&start={start}&end={end}", None)],
            _auth(env.get("TEMPO_BEARER_TOKEN"), env.get("TEMPO_TENANT_ID")),
            float(env.get("TEMPO_TIMEOUT_SECONDS", "5")),
            get,
        ),
    ]


def _listed(items: Sequence[str], limit: int = 8) -> str:
    more = len(items) - limit
    return ", ".join(items[:limit]) + (f" and {more} more" if more > 0 else "")


def _resource(group: str, resource: str) -> str:
    return f"{group}/{resource}" if group else resource


def kubernetes_checks(
    review: AccessReview | None,
    watched: Sequence[str],
    evidence: Sequence[str],
) -> list[Check]:
    """Reachability, then per namespace: every read allowed, nothing more allowed."""
    if review is None:
        return [Check("kubernetes", "not_configured")]
    try:
        version = review.version()
    except Exception as error:  # noqa: BLE001 (any client failure is the check's result)
        return [Check("kubernetes", "failed", f"API unreachable ({type(error).__name__}: {error})")]
    checks = [Check("kubernetes", "ok", f"API server {version}")]
    for namespace in dict.fromkeys((*watched, *evidence)):
        required = [*WORKLOAD_RESOURCES] if namespace in watched else []
        optional: list[tuple[str, str]] = []
        if namespace in evidence:
            required += [r for r in CHAOS_RESOURCES if r not in required]
        else:
            optional = list(CHAOS_RESOURCES)  # experiments beside their targets, when chaos is used
        try:
            missing = [
                f"{verb} {_resource(g, r)}"
                for g, r in required
                for verb in READ_VERBS
                if not review.allowed(namespace, g, r, verb)
            ]
            extra = [
                f"{verb} {_resource(g, r)}"
                for g, r in (*required, *optional)
                for verb in WRITE_VERBS
                if review.allowed(namespace, g, r, verb)
            ]
            if review.allowed(namespace, "", "secrets", "get"):
                extra.append("get secrets")
            unread = [
                _resource(g, r) for g, r in optional if not review.allowed(namespace, g, r, "list")
            ]
        except Exception as error:  # noqa: BLE001
            checks.append(
                Check(f"permissions:{namespace}", "failed", f"access review failed ({error})")
            )
            continue
        if missing:
            checks.append(Check(f"read:{namespace}", "failed", "missing: " + _listed(missing)))
        elif unread and len(unread) < len(optional):
            checks.append(
                Check(
                    f"read:{namespace}",
                    "warning",
                    "chaos kinds partly readable: " + ", ".join(unread),
                )
            )
        else:
            checks.append(Check(f"read:{namespace}", "ok", "every read the Connector needs"))
        checks.append(
            Check(
                f"read-only:{namespace}",
                "failed" if extra else "ok",
                ("broader than read-only: " + _listed(extra))
                if extra
                else "no write and no Secret access",
            )
        )
    return checks


def control_plane_check(
    env: Mapping[str, str],
    *,
    connect: Callable[[str, int, ssl.SSLContext, str | None], ssl.SSLSocket] | None = None,
    now: datetime | None = None,
) -> Check:
    """A mutual TLS handshake to the control plane with the Connector's identity, and its expiry."""
    endpoint = env.get("SRE_CONNECTOR_ENDPOINT")
    cert, key, ca = (
        env.get("SRE_CONNECTOR_TLS_CERT"),
        env.get("SRE_CONNECTOR_TLS_KEY"),
        env.get("SRE_CONNECTOR_TLS_CA"),
    )
    if not endpoint:
        return Check("control-plane", "not_configured")
    if not (cert and key and ca):
        return Check("control-plane", "failed", "the Connector's certificate, key or CA is not set")
    host, _, port = endpoint.removeprefix("https://").rpartition(":")
    try:
        context = ssl.create_default_context(cafile=ca)
        context.load_cert_chain(cert, key)
        context.set_alpn_protocols(["h2"])
        server_name = env.get("SRE_CONNECTOR_SERVER_NAME") or host
        opener = connect or _tls_connect
        with opener(host, int(port), context, server_name) as channel:
            # TLS 1.3 completes the client's handshake before the server has judged the client
            # certificate; a refusal arrives as an alert on the first read, so read once
            channel.settimeout(2.0)
            try:
                first = channel.recv(1)
            except TimeoutError:
                first = None  # the server waits for our first frame: the certificate was accepted
            if first == b"":
                # gRPC closes, without an alert, a connection whose client certificate it refused
                return Check(
                    "control-plane",
                    "failed",
                    "the control plane closed the connection: the certificate was not accepted",
                )
    except ssl.SSLError as error:
        return Check(
            "control-plane", "failed", f"refused by the control plane ({error.reason or error})"
        )
    except (OSError, ValueError) as error:
        return Check(
            "control-plane", "failed", f"handshake failed ({type(error).__name__}: {error})"
        )
    left = _expiry(Path(cert)) - (now or datetime.now(UTC))
    if left <= timedelta(0):
        return Check("control-plane", "failed", "the Connector's certificate has expired")
    if left < EXPIRY_WARNING:
        return Check(
            "control-plane", "warning", f"handshake ok; certificate expires in {left.days} days"
        )
    return Check("control-plane", "ok", f"handshake ok; certificate valid for {left.days} days")


def _tls_connect(
    host: str, port: int, context: ssl.SSLContext, server_name: str | None
) -> ssl.SSLSocket:
    raw = socket.create_connection((host or "localhost", port), timeout=5)
    return context.wrap_socket(raw, server_hostname=server_name)


def _expiry(cert: Path) -> datetime:
    from cryptography import x509

    return x509.load_pem_x509_certificate(cert.read_bytes()).not_valid_after_utc


class KubernetesAccessReview:
    """``AccessReview`` over the Kubernetes Python client, in-cluster or from the kubeconfig."""

    def __init__(self, impersonate: str | None = None) -> None:
        kubernetes: Any = importlib.import_module("kubernetes")  # untyped; imported as live.py does
        importlib.import_module("kubernetes.config")
        try:
            kubernetes.config.load_incluster_config()
        except Exception:  # noqa: BLE001 (not in a pod)
            kubernetes.config.load_kube_config()
        self._k = kubernetes
        # ``--as``: ask as the Connector's ServiceAccount from a workstation (Kubernetes impersonation)
        self._api = kubernetes.client.ApiClient()
        if impersonate:
            self._api.set_default_header("Impersonate-User", impersonate)

    def version(self) -> str:
        info = self._k.client.VersionApi(self._api).get_code()
        return str(info.git_version)

    def allowed(self, namespace: str, group: str, resource: str, verb: str) -> bool:
        client = self._k.client
        body = client.V1SelfSubjectAccessReview(
            spec=client.V1SelfSubjectAccessReviewSpec(
                resource_attributes=client.V1ResourceAttributes(
                    namespace=namespace, group=group, resource=resource, verb=verb
                )
            )
        )
        result = client.AuthorizationV1Api(self._api).create_self_subject_access_review(body)
        return bool(result.status.allowed)


def _csv(value: str | None, default: str) -> tuple[str, ...]:
    # as the Connector reads it (agent._csv): set but empty means none, not the default
    return tuple(
        item.strip() for item in (default if value is None else value).split(",") if item.strip()
    )


def run_preflight(
    env: Mapping[str, str] | None = None,
    *,
    review: AccessReview | None = None,
    get: HttpGet = http_get,
    impersonate: str | None = None,
) -> list[Check]:
    """Every check, with the Connector's environment (the same variables the agent reads)."""
    env = dict(os.environ if env is None else env)
    if review is None and env.get("SRE_CLUSTER_ACCESS") == "true":
        try:
            review = KubernetesAccessReview(impersonate)
        except Exception as error:  # noqa: BLE001
            return [
                Check("kubernetes", "failed", f"no usable kubeconfig ({error})"),
                *backend_checks(env, get),
                control_plane_check(env),
            ]
    watched = _csv(env.get("SRE_WATCH_NAMESPACES"), "sre-demo")
    evidence = _csv(env.get("SRE_EVIDENCE_NAMESPACES"), "chaos-mesh")
    return [
        *kubernetes_checks(review, watched, evidence),
        *backend_checks(env, get),
        control_plane_check(env),
    ]


def failed(checks: Sequence[Check]) -> bool:
    return any(check.status == "failed" for check in checks)


def as_dicts(checks: Sequence[Check]) -> list[dict[str, Any]]:
    return [asdict(check) for check in checks]


def render(checks: Sequence[Check]) -> str:
    width = max((len(c.name) for c in checks), default=10)
    lines = [f"{'CHECK'.ljust(width)}  STATUS          DETAIL"]
    lines += [f"{c.name.ljust(width)}  {c.status.ljust(14)}  {c.detail}" for c in checks]
    return "\n".join(lines)


__all__ = [
    "AccessReview",
    "Check",
    "KubernetesAccessReview",
    "as_dicts",
    "backend_checks",
    "control_plane_check",
    "failed",
    "kubernetes_checks",
    "render",
    "run_preflight",
]

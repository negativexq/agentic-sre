"""Bounded cluster and workload mutations used to stage live scenarios.

Every action is a small, reversible operation against the demo namespace.  They
are benchmark setup authority only: nothing here is reachable from the
investigation tool surface, and no action reads or writes diagnosis state.
"""

from __future__ import annotations

import json
import socket
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

NAMESPACE = "sre-demo"
REQUEST_TIMEOUT_SECONDS = 15


class ActionError(RuntimeError):
    """Raised when a staging action could not be applied."""

    def __init__(self, action: str, details: dict[str, Any]) -> None:
        self.action = action
        self.details = details
        super().__init__(f"{action}: {json.dumps(details, sort_keys=True)}")


@dataclass(frozen=True, slots=True)
class Context:
    """Endpoints and namespace a scenario run operates against."""

    namespace: str = NAMESPACE
    order_url: str = "http://localhost:18000"
    payment_url: str = "http://localhost:18001"
    control_plane_url: str = "http://localhost:18080"
    prometheus_url: str = "http://localhost:19090"
    dry_run: bool = False


class Action(Protocol):
    """One reversible staging step."""

    def describe(self) -> str: ...

    def apply(self, context: Context) -> None: ...


def kubectl(*args: str, context: Context, check: bool = True) -> str:
    """Run kubectl against the scenario namespace and return stdout."""
    command = ("kubectl", "-n", context.namespace, *args)
    if context.dry_run:
        return ""
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        raise ActionError(
            "kubectl",
            {
                "command": " ".join(command),
                "returncode": result.returncode,
                "stderr": result.stderr,
            },
        )
    return result.stdout


def post_json(url: str, payload: dict[str, Any], *, context: Context) -> Any:
    """POST a JSON body and return the decoded response."""
    if context.dry_run:
        return None
    request = Request(  # noqa: S310 - fixed http endpoints inside the demo namespace
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # noqa: S310
            body = response.read().decode()
    except HTTPError as error:
        raise ActionError("post_json", {"url": url, "status": error.code}) from error
    except URLError as error:
        raise ActionError("post_json", {"url": url, "reason": str(error.reason)}) from error
    return json.loads(body) if body else None


@dataclass(frozen=True, slots=True)
class HttpFault:
    """Set the runtime fault configuration of a workload service.

    This mutates process memory only.  It leaves no Kubernetes trace, which is
    exactly why scenarios built on it expect the engine to abstain.
    """

    service: str
    values: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        rendered = ", ".join(f"{key}={value}" for key, value in sorted(self.values.items()))
        return f"{self.service} runtime fault ({rendered or 'cleared'})"

    def _url(self, context: Context) -> str:
        if self.service == "payment-service":
            return f"{context.payment_url}/__faults"
        if self.service == "order-service":
            return f"{context.order_url}/__faults"
        raise ActionError("http_fault", {"service": self.service, "reason": "no fault endpoint"})

    def apply(self, context: Context) -> None:
        payload: dict[str, Any] = {
            "delay_ms": 0,
            "error": False,
            "db_query_delay_ms": 0,
        }
        if self.service == "payment-service":
            payload["db_hold_ms"] = 0
        payload.update(self.values)
        post_json(self._url(context), payload, context=context)


@dataclass(frozen=True, slots=True)
class EnvPatch:
    """Patch deployment environment variables, producing a real SPEC_CHANGE."""

    deployment: str
    values: dict[str, str]
    wait: bool = True

    def describe(self) -> str:
        rendered = ", ".join(f"{key}={value}" for key, value in sorted(self.values.items()))
        return f"{self.deployment} env {rendered}"

    def apply(self, context: Context) -> None:
        pairs = [f"{key}={value}" for key, value in sorted(self.values.items())]
        kubectl("set", "env", f"deployment/{self.deployment}", *pairs, context=context)
        if self.wait:
            WaitRollout(self.deployment).apply(context)


@dataclass(frozen=True, slots=True)
class EnvUnset:
    """Remove environment overrides so a deployment returns to its manifest."""

    deployment: str
    names: tuple[str, ...]

    def describe(self) -> str:
        return f"{self.deployment} env unset {', '.join(self.names)}"

    def apply(self, context: Context) -> None:
        pairs = [f"{name}-" for name in self.names]
        kubectl("set", "env", f"deployment/{self.deployment}", *pairs, context=context)
        WaitRollout(self.deployment).apply(context)


@dataclass(frozen=True, slots=True)
class SetImage:
    """Change a container image, producing an IMAGE_CHANGE."""

    deployment: str
    container: str
    image: str

    def describe(self) -> str:
        return f"{self.deployment}/{self.container} image {self.image}"

    def apply(self, context: Context) -> None:
        kubectl(
            "set",
            "image",
            f"deployment/{self.deployment}",
            f"{self.container}={self.image}",
            context=context,
        )


@dataclass(frozen=True, slots=True)
class Scale:
    """Change replica count, producing a SCALE_CHANGE."""

    deployment: str
    replicas: int

    def describe(self) -> str:
        return f"{self.deployment} scale to {self.replicas}"

    def apply(self, context: Context) -> None:
        kubectl(
            "scale", f"deployment/{self.deployment}", f"--replicas={self.replicas}", context=context
        )


@dataclass(frozen=True, slots=True)
class RolloutRestart:
    """Restart a deployment, producing a ROLLOUT_RESTART."""

    deployment: str

    def describe(self) -> str:
        return f"{self.deployment} rollout restart"

    def apply(self, context: Context) -> None:
        kubectl("rollout", "restart", f"deployment/{self.deployment}", context=context)


@dataclass(frozen=True, slots=True)
class WaitRollout:
    """Block until a deployment rollout settles."""

    deployment: str
    timeout_seconds: int = 180

    def describe(self) -> str:
        return f"wait for {self.deployment} rollout"

    def apply(self, context: Context) -> None:
        kubectl(
            "rollout",
            "status",
            f"deployment/{self.deployment}",
            f"--timeout={self.timeout_seconds}s",
            context=context,
        )


@dataclass(frozen=True, slots=True)
class ApplyManifest:
    """Create or update an object from an inline manifest."""

    body: dict[str, Any]
    label: str

    def describe(self) -> str:
        return f"apply {self.label}"

    def apply(self, context: Context) -> None:
        if context.dry_run:
            return
        document = json.dumps(self.body)
        command = ("kubectl", "-n", context.namespace, "apply", "-f", "-")
        result = subprocess.run(
            command, input=document, capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            raise ActionError("apply_manifest", {"label": self.label, "stderr": result.stderr})


@dataclass(frozen=True, slots=True)
class ApplyFile:
    """Re-apply a manifest from the repository, used to undo a deletion."""

    path: str

    def describe(self) -> str:
        return f"apply {self.path}"

    def apply(self, context: Context) -> None:
        kubectl("apply", "-f", self.path, context=context)


@dataclass(frozen=True, slots=True)
class DeleteObject:
    """Delete an object, producing an OBJECT_DELETED finding."""

    kind: str
    name: str
    missing_ok: bool = True

    def describe(self) -> str:
        return f"delete {self.kind}/{self.name}"

    def apply(self, context: Context) -> None:
        args = ["delete", self.kind, self.name]
        if self.missing_ok:
            args.append("--ignore-not-found")
        kubectl(*args, context=context)


@dataclass(frozen=True, slots=True)
class SetResources:
    """Apply container resource limits, driving pressure or OOM behaviour."""

    deployment: str
    container: str
    limits: dict[str, str] = field(default_factory=dict)
    requests: dict[str, str] = field(default_factory=dict)

    def describe(self) -> str:
        return f"{self.deployment}/{self.container} limits {sorted(self.limits.items())}"

    def apply(self, context: Context) -> None:
        args = [
            "set",
            "resources",
            f"deployment/{self.deployment}",
            f"--containers={self.container}",
        ]
        if self.limits:
            rendered = ",".join(f"{key}={value}" for key, value in sorted(self.limits.items()))
            args.append(f"--limits={rendered}")
        if self.requests:
            rendered = ",".join(f"{key}={value}" for key, value in sorted(self.requests.items()))
            args.append(f"--requests={rendered}")
        kubectl(*args, context=context)


@dataclass(frozen=True, slots=True)
class RestartContainer:
    """Kill the main process repeatedly so the container restarts."""

    deployment: str
    times: int = 2

    def describe(self) -> str:
        return f"{self.deployment} crash x{self.times}"

    def apply(self, context: Context) -> None:
        for _ in range(self.times):
            kubectl(
                "exec",
                f"deployment/{self.deployment}",
                "--",
                "kill",
                "1",
                context=context,
                check=False,
            )


@dataclass(frozen=True, slots=True)
class PatchConfigMap:
    """Change a ConfigMap value, producing a CONFIG_CHANGE."""

    name: str
    values: dict[str, str]

    def describe(self) -> str:
        return f"ConfigMap/{self.name} {sorted(self.values.items())}"

    def apply(self, context: Context) -> None:
        patch = json.dumps({"data": self.values})
        kubectl("patch", "configmap", self.name, "--type=merge", "-p", patch, context=context)


@dataclass(frozen=True, slots=True)
class PatchService:
    """Patch a Service spec, e.g. to break its selector."""

    name: str
    spec: dict[str, Any]

    def describe(self) -> str:
        return f"Service/{self.name} spec {sorted(self.spec)}"

    def apply(self, context: Context) -> None:
        patch = json.dumps({"spec": self.spec})
        kubectl("patch", "service", self.name, "--type=merge", "-p", patch, context=context)


@dataclass(frozen=True, slots=True)
class Forward:
    """One service port to expose on localhost for the duration of a run."""

    service: str
    local_port: int
    remote_port: int = 8000
    namespace: str = NAMESPACE


DEFAULT_FORWARDS: tuple[Forward, ...] = (
    Forward("order-service", 18000),
    Forward("payment-service", 18001),
    Forward("control-plane", 18080),
)


def _port_is_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


@contextmanager
def port_forwards(
    forwards: tuple[Forward, ...] = DEFAULT_FORWARDS,
    *,
    timeout_seconds: float = 30.0,
) -> Iterator[None]:
    """Expose the demo services on localhost while the block runs.

    A port that is already open is left alone, so this composes with a
    port-forward the user started themselves for the UI.
    """
    processes: list[subprocess.Popen[str]] = []
    started: list[Forward] = []
    try:
        for forward in forwards:
            if _port_is_open(forward.local_port):
                continue
            processes.append(
                subprocess.Popen(
                    (
                        "kubectl",
                        "port-forward",
                        "-n",
                        forward.namespace,
                        f"svc/{forward.service}",
                        f"{forward.local_port}:{forward.remote_port}",
                    ),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
            )
            started.append(forward)
        deadline = time.monotonic() + timeout_seconds
        for forward in started:
            while not _port_is_open(forward.local_port):
                if time.monotonic() > deadline:
                    raise ActionError(
                        "port_forward",
                        {"service": forward.service, "port": forward.local_port},
                    )
                time.sleep(0.5)
        yield
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()

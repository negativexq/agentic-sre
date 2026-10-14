"""Authoritative lifecycle watch for owned, isolated Kubernetes DEV captures."""

from __future__ import annotations

import json
import re
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.rca.model import EntityRef, Lifecycle, ObjectVersion


class DevelopmentObjectWatch:
    def __init__(self, namespace: str, resource: str, output: Path) -> None:
        if not re.fullmatch(r"binding-dev-[0-9a-f]{8}", namespace) or resource not in {
            "pods",
            "replicasets",
        }:
            raise ValueError(
                "Watch requires an isolated binding DEV namespace and allowed resource"
            )
        self.namespace, self.resource, self.output = namespace, resource, output
        self.records: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.closed = threading.Event()
        self.process: subprocess.Popen[str] | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        listing = json.loads(
            subprocess.run(
                ["kubectl", "-n", self.namespace, "get", self.resource, "-o", "json"],
                check=True,
                capture_output=True,
                text=True,
                timeout=20,
            ).stdout
        )
        (self.output / f"{self.resource}-watch-start.json").write_text(
            json.dumps(listing, indent=2) + "\n"
        )
        api = "/api/v1" if self.resource == "pods" else "/apis/apps/v1"
        path = (
            f"{api}/namespaces/{self.namespace}/{self.resource}?watch=true"
            f"&resourceVersion={listing['metadata']['resourceVersion']}&timeoutSeconds=150"
        )
        self.process = subprocess.Popen(
            ["kubectl", "get", "--raw", path, "--request-timeout=180s"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()
        self.closed.wait(0.1)
        if self.process.poll() is not None:
            self.stop()
            raise RuntimeError("DEV Kubernetes watch exited during startup")

    def _read(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        pending = ""
        decoder = json.JSONDecoder()
        for line in self.process.stdout:
            pending += line
            try:
                value, end = decoder.raw_decode(pending.lstrip())
            except json.JSONDecodeError:
                continue
            pending = pending.lstrip()[end:].lstrip()
            if value.get("type") == "ERROR":
                self.errors.append("KUBERNETES_WATCH_ERROR")
                continue
            body = value.get("object", {})
            if (
                value.get("type") in {"ADDED", "MODIFIED", "DELETED"}
                and body.get("metadata", {}).get("namespace") != self.namespace
            ):
                self.errors.append("WATCH_OBJECT_NAMESPACE_MISMATCH")
                continue
            self.records.append({"observed_at": datetime.now(UTC).isoformat(), "event": value})
        if not self.closed.is_set():
            self.errors.append("WATCH_ENDED_BEFORE_CAPTURE_STOP")
        if pending.strip():
            self.errors.append("INCOMPLETE_WATCH_JSON")

    def stop(self) -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        if self.process:
            self.process.terminate()
            self.process.wait(timeout=10)
        if self.thread:
            self.thread.join(timeout=10)
        if self.process and self.process.stderr:
            (self.output / f"{self.resource}-watch.stderr").write_text(self.process.stderr.read())
        (self.output / f"{self.resource}-watch.json").write_text(
            json.dumps({"records": self.records, "errors": self.errors}, indent=2) + "\n"
        )


def apply_watch_lifecycle(
    versions: list[ObjectVersion], records: list[dict[str, Any]]
) -> list[ObjectVersion]:
    """Only explicit watch ADDED/DELETED provides watched-kind lifecycle authority."""
    watched = {"Pod", "ReplicaSet"}
    output = [
        v.model_copy(update={"lifecycle": Lifecycle.OBSERVED})
        if v.entity.kind in watched and v.lifecycle is Lifecycle.CREATED
        else v
        for v in versions
        if not (v.entity.kind in watched and v.lifecycle is Lifecycle.DELETED)
    ]
    for index, row in enumerate(records):
        event = row["event"]
        body = event.get("object", {})
        metadata = body.get("metadata", {})
        kind = body.get("kind")
        if event.get("type") not in {"ADDED", "DELETED"} or kind not in watched:
            continue
        output.append(
            ObjectVersion(
                entity=EntityRef(namespace=metadata["namespace"], kind=kind, name=metadata["name"]),
                uid=metadata["uid"],
                observed_at=datetime.fromisoformat(row["observed_at"]),
                body=body,
                evidence_id=f"dev-watch:{index}",
                lifecycle=Lifecycle.CREATED if event["type"] == "ADDED" else Lifecycle.DELETED,
            )
        )
    return sorted(output, key=lambda v: v.observed_at)

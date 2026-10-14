"""Loopback-only DEV HTTP body-byte/timing measurement; no provider semantics."""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import urlopen


@contextmanager
def measure_tempo_http(upstream: str) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    parsed = urlsplit(upstream)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.username
    ):
        raise ValueError("Cost proxy requires a plain loopback HTTP Tempo endpoint")
    rows: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path != "/api/search" and not re.fullmatch(r"/api/v2/traces/[0-9a-f]{16,32}", path):
                self.send_error(403)
                return
            started = time.perf_counter()
            body = b""
            downloaded_bytes = 0
            status = 502
            error: str | None = None
            try:
                try:
                    response = urlopen(upstream.rstrip("/") + self.path, timeout=5)
                except HTTPError as exc:
                    response = exc
                with response:
                    status = response.status
                    body = response.read(4_194_305)
                    downloaded_bytes = len(body)
                if len(body) > 4_194_304:
                    raise ValueError("DEV response exceeded byte bound")
            except Exception as exc:
                error = type(exc).__name__
                status, body = 502, b"{}"
            elapsed = time.perf_counter() - started
            rows.append(
                {
                    "path": path,
                    "status": status,
                    "response_body_bytes": downloaded_bytes,
                    "upstream_seconds": elapsed,
                    "error": error,
                }
            )
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", rows
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

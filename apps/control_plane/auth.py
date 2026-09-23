"""Shared bearer-token guard for state-changing endpoints.

Unset ``SRE_API_TOKEN`` keeps today's default-open behavior (the offline demo
and kind walkthrough need no setup); setting it requires every caller — the
Alertmanager webhook, and the console's report-create and email-share endpoints
— to present it as ``Authorization: Bearer <token>``. Read-only endpoints stay
open in the built-in local/demo deployment.
"""

from __future__ import annotations

import hmac
import os

from fastapi import HTTPException, Request

API_TOKEN_ENV = "SRE_API_TOKEN"


def require_api_token(request: Request) -> None:
    """Reject a state-changing request when a token is configured but not presented."""
    token = os.environ.get(API_TOKEN_ENV)
    if not token:
        return
    header = request.headers.get("authorization", "")
    prefix = "Bearer "
    presented = header[len(prefix) :] if header.startswith(prefix) else ""
    if not presented or not hmac.compare_digest(presented, token):
        raise HTTPException(
            status_code=401,
            detail="missing or invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

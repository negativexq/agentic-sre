"""Read-only effective configuration for the console settings screen.

Values are derived from the environment. Secrets (API token, SMTP password,
upstream bearer tokens) are never returned — only whether each is configured —
so the settings screen can be shown without exposing credentials (G11).
"""

from __future__ import annotations

import os

from apps.control_plane.console.dto import SettingsView
from packages.report.model import REPORT_VERSION


def _csv(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _flag(name: str, *, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.casefold() == "true"


# The connector capabilities that read each evidence backend (same mapping as the system status).
_BACKEND_CAPABILITIES = {
    "prometheus": frozenset({"resource_pressure", "traffic"}),
    "loki": frozenset({"logs"}),
    "tempo": frozenset({"runtime_traces"}),
}


def read_settings(connector_capabilities: frozenset[str] | None = None) -> SettingsView:
    """Assemble the current effective configuration for display.

    In remote mode (``connector_capabilities`` given) the evidence backends are configured on the connector,
    not here, so whether each is configured comes from what the connector reports, as on the system status;
    otherwise from this process's environment.
    """

    def backend(name: str, env_var: str) -> bool:
        if connector_capabilities is not None:
            return bool(_BACKEND_CAPABILITIES[name] & connector_capabilities)
        return bool(os.environ.get(env_var))

    return SettingsView(
        watched_namespaces=_csv("SRE_WATCH_NAMESPACES"),
        evidence_namespaces=_csv("SRE_EVIDENCE_NAMESPACES"),
        auto_diagnose=_flag("SRE_AUTO_DIAGNOSE"),
        watch_interval_seconds=float(os.environ.get("SRE_WATCH_INTERVAL_SECONDS", "0") or "0"),
        cluster_access=os.environ.get("SRE_CLUSTER_ACCESS", "not set"),
        llm_enabled=_flag("SRE_LLM_ENABLED"),
        llm_model=os.environ.get("SRE_LLM_MODEL"),
        llm_max_calls=int(os.environ.get("SRE_LLM_MAX_CALLS", "0") or "0"),
        api_token_configured=bool(os.environ.get("SRE_API_TOKEN")),
        email_configured=bool(os.environ.get("SRE_SMTP_HOST")),
        email_sender=os.environ.get("SRE_SMTP_FROM"),
        prometheus_configured=backend("prometheus", "PROMETHEUS_URL"),
        loki_configured=backend("loki", "SRE_LOKI_URL"),
        tempo_configured=backend("tempo", "TEMPO_URL"),
        report_version=REPORT_VERSION,
    )

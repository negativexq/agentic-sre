"""Email delivery for shared reports.

Delivery is real SMTP when ``SRE_SMTP_HOST`` is configured, and cleanly absent
otherwise: the console reports "not configured" and the share endpoint refuses
rather than pretending to send. Nothing here sends on its own — an operator
triggers each share, and every attempt is recorded for audit by the caller.
"""

from __future__ import annotations

import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

from packages.report.email import EmailPayload


class EmailNotConfigured(RuntimeError):
    """Raised when a share is attempted without SMTP configured."""


@dataclass(frozen=True)
class SmtpSettings:
    host: str
    port: int
    username: str | None
    password: str | None
    sender: str
    starttls: bool

    @classmethod
    def from_env(cls) -> SmtpSettings | None:
        host = os.environ.get("SRE_SMTP_HOST")
        if not host:
            return None
        return cls(
            host=host,
            port=int(os.environ.get("SRE_SMTP_PORT", "587")),
            username=os.environ.get("SRE_SMTP_USERNAME"),
            password=os.environ.get("SRE_SMTP_PASSWORD"),
            sender=os.environ.get("SRE_SMTP_FROM", "agentic-sre@localhost"),
            starttls=os.environ.get("SRE_SMTP_STARTTLS", "true").casefold() != "false",
        )


class EmailDelivery:
    """Sends rendered report emails over SMTP."""

    def __init__(self, settings: SmtpSettings) -> None:
        self._settings = settings

    @property
    def sender(self) -> str:
        return self._settings.sender

    def _build(self, recipients: list[str], payload: EmailPayload) -> EmailMessage:
        message = EmailMessage()
        message["Subject"] = payload.subject
        message["From"] = self._settings.sender
        message["To"] = ", ".join(recipients)
        message.set_content(payload.text_body)
        message.add_alternative(payload.html_body, subtype="html")
        if payload.pdf is not None and payload.pdf_filename is not None:
            message.add_attachment(
                payload.pdf,
                maintype="application",
                subtype="pdf",
                filename=payload.pdf_filename,
            )
        return message

    def send(self, recipients: list[str], payload: EmailPayload) -> None:
        settings = self._settings
        message = self._build(recipients, payload)
        with smtplib.SMTP(settings.host, settings.port, timeout=15) as client:
            if settings.starttls:
                client.starttls()
            if settings.username and settings.password:
                client.login(settings.username, settings.password)
            client.send_message(message)


def email_delivery_from_env() -> EmailDelivery | None:
    """Build a delivery backend from the environment, or None if unconfigured."""
    settings = SmtpSettings.from_env()
    return EmailDelivery(settings) if settings is not None else None

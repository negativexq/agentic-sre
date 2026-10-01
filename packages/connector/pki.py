"""Static certificates for the connector transport (contract §12.4).

A CA, one server certificate for the control plane and one leaf certificate per connector. No
rotation and no revocation list in this version; expiry is a hard failure. A private key is written
only to the directory of the host that owns it.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

DEFAULT_VALIDITY_DAYS = 90
CONNECTOR_SAN_PREFIX = "connector:"


@dataclass(frozen=True)
class Identity:
    """A certificate and its private key, PEM encoded."""

    certificate: bytes
    private_key: bytes


def _key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _pem_key(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def new_ca(
    common_name: str = "agentic-sre connector CA",
    *,
    days: int = DEFAULT_VALIDITY_DAYS,
    now: datetime | None = None,
) -> Identity:
    start = now or datetime.now(UTC)
    key = _key()
    subject = _name(common_name)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start - timedelta(minutes=5))
        .not_valid_after(start + timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    return Identity(
        certificate=certificate.public_bytes(serialization.Encoding.PEM), private_key=_pem_key(key)
    )


def _issue(
    ca: Identity,
    common_name: str,
    sans: list[x509.GeneralName],
    usage: x509.ObjectIdentifier,
    *,
    days: int,
    now: datetime | None,
) -> Identity:
    start = now or datetime.now(UTC)
    ca_certificate = x509.load_pem_x509_certificate(ca.certificate)
    ca_key = serialization.load_pem_private_key(ca.private_key, password=None)
    assert isinstance(ca_key, ec.EllipticCurvePrivateKey)
    key = _key()
    certificate = (
        x509.CertificateBuilder()
        .subject_name(_name(common_name))
        .issuer_name(ca_certificate.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start - timedelta(minutes=5))
        .not_valid_after(start + timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.ExtendedKeyUsage([usage]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    return Identity(
        certificate=certificate.public_bytes(serialization.Encoding.PEM), private_key=_pem_key(key)
    )


def issue_server(
    ca: Identity,
    hostnames: list[str],
    *,
    days: int = DEFAULT_VALIDITY_DAYS,
    now: datetime | None = None,
) -> Identity:
    """The control plane's server certificate, valid for the given DNS names and IP addresses."""
    sans: list[x509.GeneralName] = []
    for host in hostnames:
        try:
            sans.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            sans.append(x509.DNSName(host))
    return _issue(ca, hostnames[0], sans, ExtendedKeyUsageOID.SERVER_AUTH, days=days, now=now)


def issue_connector(
    ca: Identity,
    connector_id: str,
    *,
    days: int = DEFAULT_VALIDITY_DAYS,
    now: datetime | None = None,
) -> Identity:
    """A connector's client certificate; its identity is the URI SAN ``connector:<id>``."""
    sans: list[x509.GeneralName] = [
        x509.UniformResourceIdentifier(f"{CONNECTOR_SAN_PREFIX}{connector_id}")
    ]
    return _issue(
        ca, f"connector {connector_id}", sans, ExtendedKeyUsageOID.CLIENT_AUTH, days=days, now=now
    )


def write_identity(identity: Identity, directory: Path, name: str) -> tuple[Path, Path]:
    """Write ``<name>.crt`` and ``<name>.key`` (the key readable by its owner only)."""
    directory.mkdir(parents=True, exist_ok=True)
    certificate, key = directory / f"{name}.crt", directory / f"{name}.key"
    certificate.write_bytes(identity.certificate)
    key.touch(mode=0o600)
    key.write_bytes(identity.private_key)
    return certificate, key

"""The lab's certificates and its Alertmanager configuration (testbed control plane design §4, §5)."""

from __future__ import annotations

import stat
from pathlib import Path

import yaml
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec

from packages.connector.lab import lab_pki, main, render_alertmanager

ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "infra/observability/alertmanager.yml").read_text(encoding="utf-8")
URL = "http://connector-webhook.connector.svc.cluster.local:9095/webhook"


def certificate(path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(path.read_bytes())


def test_the_lab_certificates_chain_to_one_authority_with_the_right_names(tmp_path: Path) -> None:
    paths = lab_pki(tmp_path)
    ca, server, client = (certificate(paths[k]) for k in ("ca", "server_crt", "client_crt"))
    for leaf in (server, client):
        assert leaf.issuer == ca.subject
        public = ca.public_key()
        assert isinstance(public, ec.EllipticCurvePublicKey)
        algorithm = leaf.signature_hash_algorithm
        assert algorithm is not None
        public.verify(leaf.signature, leaf.tbs_certificate_bytes, ec.ECDSA(algorithm))
    names = server.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "host.docker.internal" in names.get_values_for_type(x509.DNSName)
    assert "localhost" in names.get_values_for_type(x509.DNSName)
    uris = client.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert uris.get_values_for_type(x509.UniformResourceIdentifier) == ["connector:lab"]


def test_private_keys_are_readable_by_their_owner_only(tmp_path: Path) -> None:
    lab_pki(tmp_path)
    for key in tmp_path.glob("*.key"):
        assert stat.S_IMODE(key.stat().st_mode) == 0o600, key.name


def test_a_second_run_keeps_the_existing_identities(tmp_path: Path) -> None:
    first = lab_pki(tmp_path)
    before = {name: path.read_bytes() for name, path in first.items()}
    second = lab_pki(tmp_path)
    assert {name: path.read_bytes() for name, path in second.items()} == before


def test_a_partial_set_is_replaced_whole_so_authorities_never_mix(tmp_path: Path) -> None:
    paths = lab_pki(tmp_path)
    old_ca = paths["ca"].read_bytes()
    paths["client_crt"].unlink()
    lab_pki(tmp_path)
    assert paths["ca"].read_bytes() != old_ca  # everything was issued again under a new CA
    assert certificate(paths["client_crt"]).issuer == certificate(paths["ca"]).subject


def test_the_lab_alertmanager_keeps_its_routing_and_replaces_only_the_receiver() -> None:
    rendered = yaml.safe_load(render_alertmanager(SOURCE, url=URL, token="t0ken"))
    source = yaml.safe_load(SOURCE)
    assert rendered["route"]["group_by"] == source["route"]["group_by"]
    assert rendered["route"]["receiver"] == "connector"
    (receiver,) = rendered["receivers"]
    assert receiver["name"] == "connector"
    (hook,) = receiver["webhook_configs"]
    assert hook["url"] == URL and hook["send_resolved"] is True
    assert hook["http_config"]["authorization"] == {"type": "Bearer", "credentials": "t0ken"}
    assert "control-plane" not in yaml.safe_dump(rendered)


def test_the_command_line_prints_the_rendered_configuration(tmp_path: Path, capsys: object) -> None:
    token = tmp_path / "token"
    token.write_text("abc123\n")
    source = tmp_path / "alertmanager.yml"
    source.write_text(SOURCE)
    assert (
        main(["alertmanager", "--source", str(source), "--url", URL, "--token-file", str(token)])
        == 0
    )
    out = capsys.readouterr().out  # type: ignore[attr-defined]
    assert yaml.safe_load(out)["receivers"][0]["webhook_configs"][0]["url"] == URL
    assert "abc123" in out


def test_the_command_line_names_the_control_plane_for_a_real_install(tmp_path: Path) -> None:
    # README quick start: the server certificate must carry the name the customer's cluster dials
    assert main(["pki", "--out", str(tmp_path), "--hostname", "sre.example.com"]) == 0
    names = certificate(tmp_path / "server.crt").extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    )
    assert names.value.get_values_for_type(x509.DNSName) == ["sre.example.com"]

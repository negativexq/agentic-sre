"""M20.2: the investigation seed mode is explicit, persisted and digested."""

from __future__ import annotations

import json
from hashlib import sha256

import pytest

from packages.rca.engine import EngineConfig
from packages.rca.investigation.state import (
    RCA_CONFIG_SCHEMA,
    SEED_BOUNDED_INITIAL_VIEW,
    SEED_FULL_SOURCE,
    InvestigationConfig,
    _encode_config,
    investigation_config_document,
    investigation_config_from_document,
    rca_config_digest,
)


def test_the_default_seed_is_the_bounded_benchmark_view() -> None:
    assert InvestigationConfig().seed_mode == SEED_BOUNDED_INITIAL_VIEW


def test_an_unknown_seed_mode_is_refused() -> None:
    with pytest.raises(ValueError, match="seed_mode"):
        InvestigationConfig(seed_mode="PARTIAL")


@pytest.mark.parametrize("seed", [SEED_BOUNDED_INITIAL_VIEW, SEED_FULL_SOURCE])
def test_new_documents_always_persist_the_seed_explicitly(seed: str) -> None:
    config = InvestigationConfig(engine=EngineConfig(), seed_mode=seed)
    document = json.loads(json.dumps(investigation_config_document(config)))
    assert document["seed_mode"] == seed
    assert investigation_config_from_document(document) == config


def test_a_legacy_seedless_document_decodes_as_the_bounded_view() -> None:
    document = investigation_config_document(InvestigationConfig(engine=EngineConfig()))
    legacy = {key: value for key, value in document.items() if key != "seed_mode"}
    decoded = investigation_config_from_document(legacy)
    assert decoded.seed_mode == SEED_BOUNDED_INITIAL_VIEW
    assert decoded == InvestigationConfig(engine=EngineConfig())


def test_only_the_seed_field_is_backfilled() -> None:
    document = investigation_config_document(InvestigationConfig(engine=EngineConfig()))
    missing_other = {key: value for key, value in document.items() if key != "max_turns"}
    with pytest.raises(ValueError, match="max_turns"):
        investigation_config_from_document(missing_other)
    seedless_and_missing = {
        key: value for key, value in document.items() if key not in {"seed_mode", "max_turns"}
    }
    with pytest.raises(ValueError, match="max_turns"):
        investigation_config_from_document(seedless_and_missing)
    with pytest.raises(ValueError, match="surprise"):
        investigation_config_from_document({**document, "surprise": 1})
    with pytest.raises(ValueError, match="seed_mode"):
        investigation_config_from_document({**document, "seed_mode": "PARTIAL"})


def test_the_seed_mode_changes_the_investigation_config_digest() -> None:
    engine = EngineConfig()
    bounded = rca_config_digest(engine, InvestigationConfig(engine=engine))
    full = rca_config_digest(engine, InvestigationConfig(engine=engine, seed_mode=SEED_FULL_SOURCE))
    assert bounded != full


def test_a_run_without_investigation_keeps_its_v1_config_digest() -> None:
    """Base replay (M20.1b) compares this digest; the seed must not enter it."""
    engine = EngineConfig()
    envelope = {
        "schema": RCA_CONFIG_SCHEMA,
        "engine": _encode_config(engine),
        "investigation": None,
    }
    canonical = json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert RCA_CONFIG_SCHEMA == "agentic-sre.rca-config.v1"
    assert rca_config_digest(engine, None) == sha256(canonical.encode("utf-8")).hexdigest()

"""M21 amendment 4: the read-only alert-channel coverage client and its segment rule."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from email.message import Message
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from packages.rca.alert_coverage import (
    AlertCoverageConfig,
    AlertmanagerConfig,
    AlertmanagerError,
    AlertmanagerProtocolError,
    AlertmanagerReader,
)

T0 = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


class _Response(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200) -> None:
        super().__init__(body)
        self.status = status

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class Opener:
    def __init__(self, body: Any = (), status: int = 200, error: Exception | None = None) -> None:
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.status = status
        self.error = error
        self.requests: list[Request] = []

    def __call__(self, request: Request, *, timeout: float) -> _Response:
        del timeout
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return _Response(self.body, self.status)


def _reader(opener: Opener, token: str | None = None) -> AlertmanagerReader:
    return AlertmanagerReader(
        AlertmanagerConfig("http://alertmanager:9093/", bearer_token=token), opener=opener
    )


@pytest.mark.parametrize(
    ("seconds", "continues"),
    [(60, True), (89, True), (120.001, True), (149, True), (150, False), (180, False)],
)
def test_a_gap_is_counted_in_polling_slots_so_jitter_never_splits(
    seconds: float, continues: bool
) -> None:
    config = AlertCoverageConfig(poll_interval=timedelta(seconds=60), max_gap_polls=2)
    assert config.continues(T0, T0 + timedelta(seconds=seconds)) is continues


def test_the_cadence_and_the_gap_are_configuration_not_code() -> None:
    config = AlertCoverageConfig.from_environment(
        {"SRE_ALERT_COVERAGE_POLL_SECONDS": "30", "SRE_ALERT_COVERAGE_MAX_GAP_POLLS": "3"}
    )
    assert config == AlertCoverageConfig(poll_interval=timedelta(seconds=30), max_gap_polls=3)
    assert AlertCoverageConfig.from_environment({}) == AlertCoverageConfig()
    with pytest.raises(ValueError):
        AlertCoverageConfig.from_environment({"SRE_ALERT_COVERAGE_POLL_SECONDS": "0"})
    with pytest.raises(ValueError):
        AlertCoverageConfig.from_environment({"SRE_ALERT_COVERAGE_MAX_GAP_POLLS": "zero"})


def test_no_alertmanager_url_means_no_coverage_client() -> None:
    assert AlertmanagerConfig.from_environment({}) is None
    config = AlertmanagerConfig.from_environment(
        {"SRE_ALERTMANAGER_URL": "https://am.example:9093", "SRE_ALERTMANAGER_TOKEN": "secret"}
    )
    assert config is not None and config.bearer_token == "secret"
    assert "secret" not in repr(config)
    for url in ("ftp://am:9093", "http://user:pw@am:9093", "http://am:9093/?x=1"):
        with pytest.raises(ValueError):
            AlertmanagerConfig(url)


def test_the_client_only_ever_issues_get_alerts() -> None:
    opener = Opener([{"labels": {"alertname": "A"}}, {"labels": {"alertname": "B"}}])
    assert _reader(opener, token="t").active_alert_count() == 2
    (request,) = opener.requests
    assert request.get_method() == "GET"
    assert request.full_url == "http://alertmanager:9093/api/v2/alerts"
    assert request.get_header("Authorization") == "Bearer t"
    public = {name for name in dir(AlertmanagerReader) if not name.startswith("_")}
    assert public - {"config", "opener"} == {"active_alert_count"}


@pytest.mark.parametrize(
    ("opener", "error"),
    [
        (Opener({"alerts": []}), AlertmanagerProtocolError),
        (Opener(b"not json"), AlertmanagerProtocolError),
        (Opener([], status=503), AlertmanagerError),
        (Opener(error=HTTPError("u", 401, "no", Message(), None)), AlertmanagerError),
        (Opener(error=URLError("refused")), AlertmanagerError),
        (Opener(error=TimeoutError()), AlertmanagerError),
    ],
)
def test_a_poll_that_did_not_observe_the_alert_channel_raises(
    opener: Opener, error: type[Exception]
) -> None:
    with pytest.raises(error):
        _reader(opener).active_alert_count()

"""Byte counts refer to downloaded HTTP bodies, not reserialized JSON."""

from unittest.mock import MagicMock
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from packages.evals.tempo_cost_proxy import measure_tempo_http


def test_proxy_preserves_body_and_records_http_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    response = MagicMock()
    response.status = 200
    response.read.return_value = b'{ "traces": [] }\n'
    response.__enter__.return_value = response
    upstream = MagicMock(return_value=response)
    monkeypatch.setattr("packages.evals.tempo_cost_proxy.urlopen", upstream)
    with measure_tempo_http("http://127.0.0.1:13200") as (endpoint, records):
        with urlopen(endpoint + "/api/search?limit=8", timeout=2) as result:
            assert result.read() == b'{ "traces": [] }\n'
        assert len(records) == 1
        assert records[0]["response_body_bytes"] == len(b'{ "traces": [] }\n')
        assert records[0]["status"] == 200
        assert records[0]["upstream_seconds"] >= 0
        with pytest.raises(HTTPError) as error:
            urlopen(endpoint + "/not-tempo", timeout=2)
        assert error.value.code == 403
        assert upstream.call_count == 1


def test_proxy_rejects_nonlocal_or_credentialed_upstreams() -> None:
    for endpoint in ("https://example.org", "http://user@localhost", "http://127.0.0.1/path"):
        with pytest.raises(ValueError, match="loopback"):
            with measure_tempo_http(endpoint):
                pass

"""T005: VictoriaMetrics driver — fixture parsing + totality (feature 108).

Every failure mode must come back as a typed report: the driver never raises. The wire is
httpx.MockTransport; envelope fixtures were recorded read-only from the live store (see
tests/fixtures/README.md).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from obi.core.drivers.victoria import VictoriaMetricsSource
from obi.core.models import SourceOutcome
from tests.fakes import VICTORIA_FIXTURES_DIR, victoria_settings

_START = datetime(2026, 8, 28, 0, 0, tzinfo=UTC)
_END = datetime(2026, 8, 28, 1, 0, tzinfo=UTC)


def _fixture_bytes(name: str) -> bytes:
    return (VICTORIA_FIXTURES_DIR / f"{name}.json").read_bytes()


def _source(
    handler: Any, **kwargs: Any
) -> tuple[VictoriaMetricsSource, list[httpx.Request]]:
    """A driver over a MockTransport; returns it plus the captured request log."""

    requests: list[httpx.Request] = []

    def _handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    src = VictoriaMetricsSource(
        victoria_settings(), transport=httpx.MockTransport(_handle), **kwargs
    )
    return src, requests


def _ok(body: bytes) -> Any:
    return lambda _request: httpx.Response(200, content=body)


# --- envelope parsing (recorded fixtures) ---


def test_instant_vector_parses_rf_fixture() -> None:
    src, _ = _source(_ok(_fixture_bytes("instant_rf_vector")))
    report = src.query('{hostname="magnolia-ptp-manzanita-60"}', timeout=5.0)
    assert report.outcome is SourceOutcome.REACHABLE
    assert report.detail is None
    assert not report.truncated
    assert len(report.series) >= 10  # the RF superset for one AF60 radio
    by_name = {s.labels.get("__name__"): s for s in report.series}
    cinr = by_name["cinr_Dbm"]
    assert cinr.labels["hostname"] == "magnolia-ptp-manzanita-60"
    assert cinr.labels["board"] == "AF60-LR"
    assert len(cinr.samples) == 1
    assert isinstance(cinr.samples[0].value, float)
    assert cinr.samples[0].ts.tzinfo is not None


def test_range_matrix_parses_cinr_fixture() -> None:
    src, _ = _source(_ok(_fixture_bytes("range_cinr_matrix")))
    report = src.query_range("cinr_Dbm", start=_START, end=_END, step_s=300, timeout=5.0)
    assert report.outcome is SourceOutcome.REACHABLE
    assert len(report.series) == 1
    series = report.series[0]
    assert series.labels["site"] == "magnolia"
    assert len(series.samples) > 5  # 1 h @ 300 s
    ts = [s.ts for s in series.samples]
    assert ts == sorted(ts)


def test_scalar_expression_is_a_bare_vector() -> None:
    """VM normalizes scalar expressions to a vector with empty labels (verified live)."""

    src, _ = _source(_ok(_fixture_bytes("instant_scalar")))
    report = src.query("scalar(42)", timeout=5.0)
    assert report.outcome is SourceOutcome.REACHABLE
    assert len(report.series) == 1
    assert report.series[0].labels == {}
    assert report.series[0].samples[0].value == 42.0


def test_error_envelope_is_reachable_with_store_detail() -> None:
    """A store-side query/matcher error is REACHABLE + the store's own text (model-correctable)."""

    body = _fixture_bytes("error_envelope")
    src, _ = _source(lambda _r: httpx.Response(422, content=body))
    report = src.query('bad{{', timeout=5.0)
    assert report.outcome is SourceOutcome.REACHABLE
    assert report.series == []
    assert report.detail is not None and "cannot parse" in report.detail


def test_label_values_parses_hostnames_fixture() -> None:
    src, requests = _source(_ok(_fixture_bytes("label_values_hostnames")))
    report = src.label_values("hostname", match='{role="ptp-rf"}', timeout=5.0)
    assert report.outcome is SourceOutcome.REACHABLE
    assert "magnolia-ptp-manzanita-60" in report.values
    assert len(report.values) == 24
    assert requests[0].url.path == "/api/v1/label/hostname/values"
    assert requests[0].url.params["match[]"] == '{role="ptp-rf"}'


def test_series_parses_weather_fixture() -> None:
    src, _ = _source(_ok(_fixture_bytes("series_weather")))
    report = src.series("weather_temp_f", timeout=5.0)
    assert report.outcome is SourceOutcome.REACHABLE
    stations = {ls.get("station") for ls in report.labelsets}
    assert stations == {"black_hole", "gate_actual"}


# --- totality: every failure is a typed outcome ---


def test_timeout_after_retry_is_timed_out() -> None:
    def _handle(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    src, requests = _source(_handle)
    report = src.query("up", timeout=3.0)
    assert report.outcome is SourceOutcome.TIMED_OUT
    assert report.detail is not None and "3" in report.detail
    assert len(requests) == 2  # transient → retried once


def test_connect_error_after_retry_is_unreachable() -> None:
    def _handle(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    src, requests = _source(_handle)
    report = src.label_values("hostname", match=None, timeout=3.0)
    assert report.outcome is SourceOutcome.UNREACHABLE
    assert len(requests) == 2


def test_transient_failure_then_success_recovers() -> None:
    calls = {"n": 0}
    body = _fixture_bytes("instant_scalar")

    def _handle(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("blip")
        return httpx.Response(200, content=body)

    src, _ = _source(_handle)
    report = src.query("42", timeout=3.0)
    assert report.outcome is SourceOutcome.REACHABLE
    assert calls["n"] == 2


def test_http_500_retried_then_unreachable() -> None:
    src, requests = _source(lambda _r: httpx.Response(500, content=b"boom"))
    report = src.query("up", timeout=3.0)
    assert report.outcome is SourceOutcome.UNREACHABLE
    assert report.detail is not None and "500" in report.detail
    assert len(requests) == 2


def test_http_403_is_auth_failed_not_retried() -> None:
    src, requests = _source(lambda _r: httpx.Response(403, content=b"denied"))
    report = src.query("up", timeout=3.0)
    assert report.outcome is SourceOutcome.AUTH_FAILED
    assert len(requests) == 1


def test_non_json_body_is_unreachable() -> None:
    src, _ = _source(lambda _r: httpx.Response(200, content=b"<html>surprise</html>"))
    report = src.query("up", timeout=3.0)
    assert report.outcome is SourceOutcome.UNREACHABLE
    assert report.detail is not None and "JSON" in report.detail


def test_unexpected_exception_never_escapes() -> None:
    def _handle(_request: httpx.Request) -> httpx.Response:
        raise RuntimeError("cosmic ray")

    src, _ = _source(_handle)
    for report in (
        src.query("up", timeout=1.0),
        src.query_range("up", start=_START, end=_END, step_s=60, timeout=1.0),
        src.label_values("site", match=None, timeout=1.0),
        src.series("up", timeout=1.0),
    ):
        assert report.outcome is SourceOutcome.UNREACHABLE
        assert report.detail is not None and "unexpected" in report.detail


# --- bounds and hygiene ---


def test_series_cap_truncates_and_flags() -> None:
    result = [
        {"metric": {"__name__": "m", "idx": str(i)}, "value": [1700000000, "1"]}
        for i in range(5)
    ]
    body = json.dumps(
        {"status": "success", "data": {"resultType": "vector", "result": result}}
    ).encode()
    src, _ = _source(_ok(body), max_series=2)
    report = src.query("m", timeout=3.0)
    assert report.truncated
    assert len(report.series) == 2


def test_malformed_rows_and_samples_are_skipped_not_fatal() -> None:
    body = json.dumps(
        {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    "not-a-dict",
                    {"metric": {"__name__": "ok"}, "values": [[1, "1"], "bad", [2, "2"]]},
                    {"metric": {"__name__": "no-values"}},
                ],
            },
        }
    ).encode()
    src, _ = _source(_ok(body))
    report = src.query_range("m", start=_START, end=_END, step_s=60, timeout=3.0)
    assert report.outcome is SourceOutcome.REACHABLE
    ok = [s for s in report.series if s.labels.get("__name__") == "ok"]
    assert len(ok) == 1 and len(ok[0].samples) == 2


def test_invalid_label_name_issues_no_request() -> None:
    src, requests = _source(_ok(b"{}"))
    report = src.label_values("../evil", match=None, timeout=3.0)
    assert report.outcome is SourceOutcome.UNREACHABLE
    assert report.detail is not None and "label" in report.detail
    assert requests == []


def test_query_range_sends_bounded_params() -> None:
    body = _fixture_bytes("range_cinr_matrix")
    src, requests = _source(_ok(body))
    src.query_range("cinr_Dbm", start=_START, end=_END, step_s=300, timeout=7.0)
    params = requests[0].url.params
    assert requests[0].url.path == "/api/v1/query_range"
    assert params["query"] == "cinr_Dbm"
    assert int(params["start"]) == int(_START.timestamp())
    assert int(params["end"]) == int(_END.timestamp())
    assert params["step"] == "300s"
    assert params["timeout"] == "7s"


@pytest.mark.parametrize("method", ["query", "label_values", "series"])
def test_only_get_is_ever_issued(method: str) -> None:
    src, requests = _source(_ok(_fixture_bytes("instant_scalar")))
    if method == "query":
        src.query("up", timeout=1.0)
    elif method == "label_values":
        src.label_values("site", match=None, timeout=1.0)
    else:
        src.series("up", timeout=1.0)
    assert all(r.method == "GET" for r in requests)

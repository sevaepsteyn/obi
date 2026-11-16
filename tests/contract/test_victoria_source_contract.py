"""T006: TimeseriesQuerySource contract (feature 108) — protocol conformance, the
five-endpoint read allowlist under hostile input, and timeout propagation.

The allowlist is what makes FR-005/SC-004 true BY CONSTRUCTION: no sequence of model-composed
inputs may produce a request outside the store's read/query paths, on any verb but GET.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from obi.core.drivers.base import TimeseriesQuerySource
from obi.core.drivers.victoria import VictoriaMetricsSource
from tests.fakes import victoria_settings

_ALLOWED_PATH_PREFIXES = (
    "/api/v1/query_range",
    "/api/v1/query",
    "/api/v1/labels",
    "/api/v1/label/",
    "/api/v1/series",
)

_START = datetime(2026, 8, 28, 0, 0, tzinfo=UTC)
_END = datetime(2026, 8, 28, 1, 0, tzinfo=UTC)

_HOSTILE = [
    '../admin/tsdb/delete_series?match[]=up',
    "up} or {__name__=~'.+'} \n/api/v1/import",
    'http://evil.example/api/v1/write',
    'up#/../../-/reload',
    'up&query=../admin',
]


def _capture() -> tuple[VictoriaMetricsSource, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def _handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"status": "success", "data": {"resultType": "vector", "result": []}},
        )

    return (
        VictoriaMetricsSource(victoria_settings(), transport=httpx.MockTransport(_handle)),
        requests,
    )


def test_implements_the_protocol() -> None:
    src, _ = _capture()
    assert isinstance(src, TimeseriesQuerySource)
    assert src.name == "victoria"


def test_hostile_inputs_never_leave_the_read_allowlist() -> None:
    src, requests = _capture()
    for hostile in _HOSTILE:
        src.query(hostile, timeout=1.0)
        src.query_range(hostile, start=_START, end=_END, step_s=60, timeout=1.0)
        src.series(hostile, timeout=1.0)
        src.label_values("site", match=hostile, timeout=1.0)
    assert requests  # the hostile text travels as a parameter, not a path
    for request in requests:
        assert request.method == "GET"
        assert request.url.host == "vm.example.org"
        assert request.url.path.startswith(_ALLOWED_PATH_PREFIXES), request.url


def test_hostile_label_is_refused_before_any_request() -> None:
    src, requests = _capture()
    report = src.label_values("../admin/tsdb/delete_series", match=None, timeout=1.0)
    assert requests == []
    assert report.values == []
    assert report.detail is not None


def test_timeout_reaches_the_store_side_deadline() -> None:
    src, requests = _capture()
    src.query("up", timeout=4.0)
    src.query_range("up", start=_START, end=_END, step_s=60, timeout=4.0)
    for request in requests:
        assert request.url.params["timeout"] == "4s"

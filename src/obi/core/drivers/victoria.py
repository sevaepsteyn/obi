"""VictoriaMetrics adapter — strictly read-only (Constitution Principles I, IV; feature 108).

Talks to the external team's VictoriaMetrics store (a Prometheus-compatible HTTP API,
credential-free on
the ops network) and implements the :class:`~obi.core.drivers.base.TimeseriesQuerySource`
contract. READ-ONLY BY CONSTRUCTION: the only requests ever issued are ``GET``\\ s to the five
literal read paths in :data:`_ENDPOINTS`; PromQL and matchers travel as URL-encoded query
*parameters* and can never select a different path, so the store's administrative surfaces
(``/api/v1/admin/*``, ``/api/v1/import``, ``/api/v1/write``, …) are not expressible through
this module no matter what query text arrives. This module is one of the few permitted to
import ``httpx`` (Principle IV quarantine), like ``uisp.py``/``netbox.py``/``wled.py``.

A store-side query error (bad PromQL / bad matcher) is NOT a transport failure: it comes back
as a ``REACHABLE`` report whose ``detail`` is the store's own error text verbatim, so the model
can correct its query. GETs retry once on a transient failure (the wled precedent — a single
dropped request on the playa network must not kill a field question). The transport is
injectable (``transport=``) so tests run offline against recorded fixtures.
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from obi.core.audit import record_device_command
from obi.core.config import VictoriaSettings
from obi.core.drivers.base import (
    TsLabelValuesReport,
    TsQueryReport,
    TsSample,
    TsSeries,
    TsSeriesReport,
)
from obi.core.models import SourceOutcome

# The COMPLETE read allowlist (FR-005/SC-004). ``label`` below is validated against
# _LABEL_RE before it is interpolated, so no input can escape these paths.
_ENDPOINTS = (
    "/api/v1/query",
    "/api/v1/query_range",
    "/api/v1/labels",
    "/api/v1/label/{label}/values",
    "/api/v1/series",
)

_LABEL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# TRANSPORT safety bound: series beyond this are dropped (truncated=True). Deliberately well
# above the toolset's 25-line RENDER cap — a curated handler must receive every series its
# scoped query matches (the weather query alone matches ~22), while a runaway free-form
# selector still can't transfer the store.
_DEFAULT_MAX_SERIES = 100

# How much of a query string goes into the per-request audit record (no secrets exist here —
# the bound just keeps audit rows sane).
_AUDIT_QUERY_CHARS = 200


class VictoriaError(Exception):
    """A normalized source-level failure (carries the obi :class:`SourceOutcome`)."""

    def __init__(self, outcome: SourceOutcome, detail: str, *, transient: bool = False) -> None:
        super().__init__(detail)
        self.outcome = outcome
        self.detail = detail
        self.transient = transient


def _http_error(exc: httpx.HTTPError, timeout: float) -> VictoriaError:
    """Classify an httpx failure into a typed, honestly-worded :class:`VictoriaError`."""

    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            return VictoriaError(
                SourceOutcome.AUTH_FAILED, f"the store rejected the request (HTTP {code})"
            )
        return VictoriaError(
            SourceOutcome.UNREACHABLE,
            f"the store returned HTTP {code}",
            transient=code >= 500,
        )
    if isinstance(exc, httpx.TimeoutException):
        return VictoriaError(
            SourceOutcome.TIMED_OUT,
            f"the store did not respond within {timeout:g}s",
            transient=True,
        )
    if isinstance(exc, httpx.ConnectError):
        return VictoriaError(
            SourceOutcome.UNREACHABLE,
            "could not connect to the store — check base_url / network",
            transient=True,
        )
    return VictoriaError(SourceOutcome.UNREACHABLE, f"store request failed: {exc}")


def _parse_ts(raw: object) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(raw), tz=UTC)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None


def _parse_sample(raw: object) -> TsSample | None:
    """One ``[ts, "value"]`` pair → a sample; anything malformed → ``None`` (never fatal)."""

    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        return None
    ts = _parse_ts(raw[0])
    if ts is None:
        return None
    try:
        value = float(raw[1])
    except (TypeError, ValueError):
        return None
    return TsSample(ts=ts, value=value)


def _parse_result_row(raw: object) -> TsSeries | None:
    """One vector/matrix result element → a series; malformed rows are skipped."""

    if not isinstance(raw, dict):
        return None
    metric = raw.get("metric")
    labels = (
        {str(k): str(v) for k, v in metric.items()} if isinstance(metric, dict) else {}
    )
    samples: list[TsSample] = []
    if "values" in raw:  # matrix
        values = raw.get("values")
        rows = values if isinstance(values, list) else []
        samples = [s for s in (_parse_sample(v) for v in rows) if s is not None]
    elif "value" in raw:  # vector
        sample = _parse_sample(raw.get("value"))
        samples = [sample] if sample is not None else []
    if not samples:
        return None
    return TsSeries(labels=labels, samples=samples)


class VictoriaMetricsSource:
    """Read-only VictoriaMetrics adapter implementing :class:`TimeseriesQuerySource`."""

    name = "victoria"

    def __init__(
        self,
        settings: VictoriaSettings,
        *,
        transport: httpx.BaseTransport | None = None,
        max_series: int = _DEFAULT_MAX_SERIES,
    ) -> None:
        self._settings = settings
        # Test seam: httpx.MockTransport in tests; None (real network) in production.
        self._transport = transport
        self._max_series = max_series

    # --- transport ---

    def _get(self, path: str, params: dict[str, str], timeout: float) -> Any:
        """One audited, retry-once GET to an allowlisted path; returns the parsed JSON body.

        Raises :class:`VictoriaError` only — callers translate it into their typed report.
        A non-2xx response whose body is the store's own ``{"status":"error",…}`` envelope is
        returned as that envelope (a query-level error, not a transport failure).
        """

        target = urlparse(self._settings.base_url).netloc or self._settings.base_url
        command = f"GET {path} {json.dumps(params, sort_keys=True)[:_AUDIT_QUERY_CHARS]}"
        start = time.monotonic()
        try:
            body = self._get_with_retry(path, params, timeout)
        except VictoriaError as exc:
            record_device_command(
                target,
                "http",
                command,
                duration_ms=(time.monotonic() - start) * 1000.0,
                outcome="error",
                error=exc.detail,
            )
            raise
        record_device_command(
            target,
            "http",
            command,
            duration_ms=(time.monotonic() - start) * 1000.0,
        )
        return body

    def _get_with_retry(self, path: str, params: dict[str, str], timeout: float) -> Any:
        retries = 1  # GETs retry ONCE on a transient failure (timeout / connect / 5xx)
        while True:
            try:
                return self._get_once(path, params, timeout)
            except VictoriaError as exc:
                if retries > 0 and exc.transient:
                    retries -= 1
                    continue
                raise

    def _get_once(self, path: str, params: dict[str, str], timeout: float) -> Any:
        url = f"{self._settings.base_url}{path}"
        try:
            with httpx.Client(timeout=timeout, transport=self._transport) as client:
                response = client.get(url, params=params)
                if response.status_code >= 400:
                    envelope = self._error_envelope(response)
                    if envelope is not None:
                        return envelope
                response.raise_for_status()
                return response.json()
        except httpx.HTTPError as exc:
            raise _http_error(exc, timeout) from exc
        except ValueError as exc:  # response.json() on a non-JSON body
            raise VictoriaError(
                SourceOutcome.UNREACHABLE, "the store returned a non-JSON response"
            ) from exc

    @staticmethod
    def _error_envelope(response: httpx.Response) -> dict[str, Any] | None:
        """The store's own ``{"status":"error",…}`` body, when that is what came back."""

        try:
            data = response.json()
        except ValueError:
            return None
        if isinstance(data, dict) and data.get("status") == "error":
            return data
        return None

    # --- envelope mapping ---

    def _map_query_body(self, body: Any) -> TsQueryReport:
        if not isinstance(body, dict):
            return TsQueryReport(
                SourceOutcome.UNREACHABLE, [], detail="the store returned an unexpected body"
            )
        if body.get("status") == "error":
            return TsQueryReport(
                SourceOutcome.REACHABLE, [], detail=str(body.get("error") or "query error")
            )
        data = body.get("data")
        result = data.get("result") if isinstance(data, dict) else None
        rows = result if isinstance(result, list) else []
        series = [s for s in (_parse_result_row(r) for r in rows) if s is not None]
        truncated = len(series) > self._max_series
        return TsQueryReport(
            SourceOutcome.REACHABLE, series[: self._max_series], truncated=truncated
        )

    @staticmethod
    def _map_values_body(body: Any) -> TsLabelValuesReport:
        if not isinstance(body, dict):
            return TsLabelValuesReport(
                SourceOutcome.UNREACHABLE, [], detail="the store returned an unexpected body"
            )
        if body.get("status") == "error":
            return TsLabelValuesReport(
                SourceOutcome.REACHABLE, [], detail=str(body.get("error") or "query error")
            )
        data = body.get("data")
        values = [str(v) for v in data] if isinstance(data, list) else []
        return TsLabelValuesReport(SourceOutcome.REACHABLE, values)

    @staticmethod
    def _map_series_body(body: Any) -> TsSeriesReport:
        if not isinstance(body, dict):
            return TsSeriesReport(
                SourceOutcome.UNREACHABLE, [], detail="the store returned an unexpected body"
            )
        if body.get("status") == "error":
            return TsSeriesReport(
                SourceOutcome.REACHABLE, [], detail=str(body.get("error") or "query error")
            )
        data = body.get("data")
        labelsets = [
            {str(k): str(v) for k, v in entry.items()}
            for entry in (data if isinstance(data, list) else [])
            if isinstance(entry, dict)
        ]
        return TsSeriesReport(SourceOutcome.REACHABLE, labelsets)

    # --- the TimeseriesQuerySource protocol (each method TOTAL: never raises) ---

    def query(self, promql: str, *, timeout: float) -> TsQueryReport:
        params = {"query": promql, "timeout": f"{timeout:g}s"}
        try:
            body = self._get("/api/v1/query", params, timeout)
        except VictoriaError as exc:
            return TsQueryReport(exc.outcome, [], detail=exc.detail)
        except Exception as exc:  # noqa: BLE001 - the source MUST be total
            return TsQueryReport(
                SourceOutcome.UNREACHABLE, [], detail=f"unexpected error: {exc}"
            )
        return self._map_query_body(body)

    def query_range(
        self, promql: str, *, start: datetime, end: datetime, step_s: int, timeout: float
    ) -> TsQueryReport:
        params = {
            "query": promql,
            "start": str(int(start.timestamp())),
            "end": str(int(end.timestamp())),
            "step": f"{step_s}s",
            "timeout": f"{timeout:g}s",
        }
        try:
            body = self._get("/api/v1/query_range", params, timeout)
        except VictoriaError as exc:
            return TsQueryReport(exc.outcome, [], detail=exc.detail)
        except Exception as exc:  # noqa: BLE001 - the source MUST be total
            return TsQueryReport(
                SourceOutcome.UNREACHABLE, [], detail=f"unexpected error: {exc}"
            )
        return self._map_query_body(body)

    def label_values(
        self, label: str, *, match: str | None, timeout: float
    ) -> TsLabelValuesReport:
        if not _LABEL_RE.match(label):
            # Refused BEFORE any request: the label is interpolated into the path, so only a
            # well-formed label name may ever reach URL construction.
            return TsLabelValuesReport(
                SourceOutcome.UNREACHABLE, [], detail=f"invalid label name {label!r}"
            )
        params: dict[str, str] = {}
        if match:
            params["match[]"] = match
        try:
            body = self._get(f"/api/v1/label/{label}/values", params, timeout)
        except VictoriaError as exc:
            return TsLabelValuesReport(exc.outcome, [], detail=exc.detail)
        except Exception as exc:  # noqa: BLE001 - the source MUST be total
            return TsLabelValuesReport(
                SourceOutcome.UNREACHABLE, [], detail=f"unexpected error: {exc}"
            )
        return self._map_values_body(body)

    def series(self, match: str, *, timeout: float) -> TsSeriesReport:
        try:
            body = self._get("/api/v1/series", {"match[]": match}, timeout)
        except VictoriaError as exc:
            return TsSeriesReport(exc.outcome, [], detail=exc.detail)
        except Exception as exc:  # noqa: BLE001 - the source MUST be total
            return TsSeriesReport(
                SourceOutcome.UNREACHABLE, [], detail=f"unexpected error: {exc}"
            )
        return self._map_series_body(body)

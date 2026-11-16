"""Black Rock City weather fetch + parse + collection cycle (feature 110).

Two keyless public sources, fetched independently each hourly cycle: Open-Meteo (current
conditions + the next 24 hourly forecast rows; imperial units, playa-local time strings)
and the NWS active-alerts feed for the BRC forecast zone. One :func:`run_cycle` writes
exactly one snapshot per invocation into the feature's store — including on total failure,
because the recorded outcome IS the degraded mode (Principle VI). No retries: the next
hourly cron tick is the retry (the ``elevation.py``/``gnis.py`` precedent), so a cycle can
never stall or overlap its successor.

Fetchers are injectable ``Callable``s (the elevation offline-test seam); this module is the
feature's ONLY network I/O and — per the Principle IV quarantine convention — the only
weather-related importer of ``httpx``. The NWS request carries a static ``User-Agent``
(api.weather.gov requires one; a justified deviation from the header-less elevation shape,
confined to this module).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from obi.core import weatherstore
from obi.core.config import WeatherSettings
from obi.core.weatherstore import CurrentConditions, HourlyRow, StoredAlert

logger = logging.getLogger("obi")

_OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
_NWS_ALERTS_URL = "https://api.weather.gov/alerts/active"
# api.weather.gov refuses UA-less requests; static and identifying, never configurable.
_NWS_USER_AGENT = "obi (network operations agent)"

# The eight hourly variables the spec pins (FR-003) plus the current block's seven.
_HOURLY_VARS = (
    "temperature_2m",
    "relative_humidity_2m",
    "dew_point_2m",
    "precipitation",
    "precipitation_probability",
    "wind_speed_10m",
    "wind_direction_10m",
    "wind_gusts_10m",
)
_CURRENT_VARS = (
    "temperature_2m",
    "relative_humidity_2m",
    "dew_point_2m",
    "precipitation",
    "wind_speed_10m",
    "wind_direction_10m",
    "wind_gusts_10m",
)

#: A fetch outcome: (parsed JSON payload, None) on success, (None, terse error) on failure.
FetchResult = tuple[object | None, str | None]
Fetcher = Callable[[], FetchResult]


def _default_forecast_fetcher(settings: WeatherSettings, tz_name: str = "UTC") -> Fetcher:
    def fetch() -> FetchResult:
        try:
            response = httpx.get(
                _OPEN_METEO_URL,
                params={
                    "latitude": settings.latitude,
                    "longitude": settings.longitude,
                    "current": ",".join(_CURRENT_VARS),
                    "hourly": ",".join(_HOURLY_VARS),
                    "forecast_hours": 24,
                    "timezone": tz_name,
                    "temperature_unit": "fahrenheit",
                    "wind_speed_unit": "mph",
                    "precipitation_unit": "inch",
                },
                timeout=settings.timeout_s,
            )
            response.raise_for_status()
            return response.json(), None
        except (httpx.HTTPError, ValueError) as exc:
            logger.debug("open-meteo fetch failed: %s", exc)
            return None, _terse_error(exc)

    return fetch


def _default_alerts_fetcher(settings: WeatherSettings) -> Fetcher:
    def fetch() -> FetchResult:
        if not settings.nws_zone:  # 137: no zone configured ⇒ no NWS alerts (none fetched)
            return {"type": "FeatureCollection", "features": []}, None
        try:
            response = httpx.get(
                _NWS_ALERTS_URL,
                params={"zone": settings.nws_zone},
                headers={"User-Agent": _NWS_USER_AGENT},
                timeout=settings.timeout_s,
            )
            response.raise_for_status()
            return response.json(), None
        except (httpx.HTTPError, ValueError) as exc:
            logger.debug("nws alerts fetch failed: %s", exc)
            return None, _terse_error(exc)

    return fetch


def _terse_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"http {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "timed out"
    return type(exc).__name__.lower() or "failed"


# --- parsing ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedForecast:
    """One Open-Meteo response, normalized to store rows (times as epoch µs UTC)."""

    current: CurrentConditions
    hourly: list[HourlyRow]


def _opt_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _local_iso_to_us(text: object, utc_offset_s: int) -> int | None:
    """Open-Meteo time strings are NAIVE local ISO ("2026-08-28T23:00"); the payload's
    ``utc_offset_seconds`` anchors them to real time."""

    if not isinstance(text, str):
        return None
    try:
        naive = datetime.fromisoformat(text)
    except ValueError:
        return None
    if naive.tzinfo is not None:
        return int(naive.timestamp() * 1_000_000)
    local = naive.replace(tzinfo=timezone(timedelta(seconds=utc_offset_s)))
    return int(local.timestamp() * 1_000_000)


def parse_forecast(payload: object) -> ParsedForecast | None:
    """Normalize an Open-Meteo response; ``None`` when the shape is not what we asked for.

    Individual missing values stay ``None`` (rendered "unavailable", never zero — FR-006);
    only a structurally unusable payload rejects the whole parse.
    """

    if not isinstance(payload, dict):
        return None
    offset = payload.get("utc_offset_seconds")
    utc_offset_s = int(offset) if isinstance(offset, int) else 0

    hourly_block = payload.get("hourly")
    if not isinstance(hourly_block, dict):
        return None
    times = hourly_block.get("time")
    if not isinstance(times, list) or not times:
        return None

    def _series(name: str) -> list[object]:
        values = hourly_block.get(name)
        return values if isinstance(values, list) else [None] * len(times)

    series = {name: _series(name) for name in _HOURLY_VARS}
    rows: list[HourlyRow] = []
    for i, t in enumerate(times):
        hour_us = _local_iso_to_us(t, utc_offset_s)
        if hour_us is None:
            return None

        def _at(name: str, idx: int = i) -> float | None:
            values = series[name]
            return _opt_number(values[idx]) if idx < len(values) else None

        rows.append(
            HourlyRow(
                hour_us=hour_us,
                temp_f=_at("temperature_2m"),
                humidity_pct=_at("relative_humidity_2m"),
                dew_point_f=_at("dew_point_2m"),
                precip_in=_at("precipitation"),
                precip_prob_pct=_at("precipitation_probability"),
                wind_mph=_at("wind_speed_10m"),
                wind_dir_deg=_at("wind_direction_10m"),
                gust_mph=_at("wind_gusts_10m"),
            )
        )

    current_block = payload.get("current")
    current_block = current_block if isinstance(current_block, dict) else {}
    current = CurrentConditions(
        temp_f=_opt_number(current_block.get("temperature_2m")),
        humidity_pct=_opt_number(current_block.get("relative_humidity_2m")),
        dew_point_f=_opt_number(current_block.get("dew_point_2m")),
        precip_in=_opt_number(current_block.get("precipitation")),
        wind_mph=_opt_number(current_block.get("wind_speed_10m")),
        wind_dir_deg=_opt_number(current_block.get("wind_direction_10m")),
        gust_mph=_opt_number(current_block.get("wind_gusts_10m")),
    )
    return ParsedForecast(current=current, hourly=rows)


def _alert_ts_us(text: object) -> int | None:
    if not isinstance(text, str):
        return None
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        return None  # NWS stamps carry offsets; a naive one is not trustworthy
    return int(stamp.timestamp() * 1_000_000)


def parse_alerts(payload: object) -> list[StoredAlert] | None:
    """Normalize an NWS active-alerts FeatureCollection; ``None`` when unparseable.

    An empty ``features`` array is a VERIFIED "none active" — ``[]``, distinct from
    ``None`` (FR-004/FR-006: unknown is never conflated with none).
    """

    if not isinstance(payload, dict):
        return None
    features = payload.get("features")
    if not isinstance(features, list):
        return None
    alerts: list[StoredAlert] = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        props = feature.get("properties")
        if not isinstance(props, dict):
            continue
        event = props.get("event")
        if not isinstance(event, str) or not event.strip():
            continue
        severity = props.get("severity")
        headline = props.get("headline")
        alerts.append(
            StoredAlert(
                event=event.strip(),
                severity=severity.strip() if isinstance(severity, str) else None,
                onset_us=_alert_ts_us(props.get("onset")),
                ends_us=_alert_ts_us(props.get("ends") or props.get("expires")),
                headline=headline.strip() if isinstance(headline, str) else None,
            )
        )
    return alerts


# --- the collection cycle -------------------------------------------------------------------


@dataclass(frozen=True)
class CycleSummary:
    """One ``obi weather run`` outcome — what was recorded, per source."""

    snapshot_id: int
    forecast_ok: bool
    forecast_error: str | None
    alerts_ok: bool
    alerts_error: str | None
    hourly_rows: int
    active_alerts: int
    pruned: int


def run_cycle(
    settings: WeatherSettings,
    conn: weatherstore.Connection,
    *,
    now_us: int,
    forecast_fetcher: Fetcher | None = None,
    alerts_fetcher: Fetcher | None = None,
    tz_name: str = "UTC",
) -> CycleSummary:
    """Fetch both sources independently, write ONE snapshot, prune, commit — atomically.

    A source failure (fetch or parse) is a recorded outcome on the snapshot, never an
    exception out of the cycle; the previous snapshot remains what readers serve for the
    failed part. Commits before returning: insert + prune are one transaction.
    """

    fetch_forecast = forecast_fetcher or _default_forecast_fetcher(settings, tz_name)
    fetch_alerts = alerts_fetcher or _default_alerts_fetcher(settings)

    forecast_payload, forecast_error = fetch_forecast()
    parsed_forecast: ParsedForecast | None = None
    if forecast_payload is not None:
        parsed_forecast = parse_forecast(forecast_payload)
        if parsed_forecast is None:
            forecast_error = "unparseable response"

    alerts_payload, alerts_error = fetch_alerts()
    parsed_alerts: list[StoredAlert] | None = None
    if alerts_payload is not None:
        parsed_alerts = parse_alerts(alerts_payload)
        if parsed_alerts is None:
            alerts_error = "unparseable response"

    snapshot_id = weatherstore.insert_cycle(
        conn,
        fetched_us=now_us,
        forecast_ok=parsed_forecast is not None,
        forecast_error=forecast_error if parsed_forecast is None else None,
        current=parsed_forecast.current if parsed_forecast is not None else None,
        hourly=parsed_forecast.hourly if parsed_forecast is not None else [],
        alerts_ok=parsed_alerts is not None,
        alerts_error=alerts_error if parsed_alerts is None else None,
        alerts=parsed_alerts or [],
    )
    pruned = weatherstore.prune(
        conn, older_than_us=now_us - settings.retention_days * 86_400 * 1_000_000
    )
    conn.commit()
    return CycleSummary(
        snapshot_id=snapshot_id,
        forecast_ok=parsed_forecast is not None,
        forecast_error=forecast_error if parsed_forecast is None else None,
        alerts_ok=parsed_alerts is not None,
        alerts_error=alerts_error if parsed_alerts is None else None,
        hourly_rows=len(parsed_forecast.hourly) if parsed_forecast is not None else 0,
        active_alerts=len(parsed_alerts or []),
        pruned=pruned,
    )

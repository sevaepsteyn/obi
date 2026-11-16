"""T020: UniFi ingest resilience (feature 016) — failure isolation, re-login, malformed skip."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx

from obi.core import logstore
from obi.core.audit import AuditTrail
from obi.core.config import Config, LogsSettings
from obi.core.drivers.unifi_common import UnifiError
from obi.core.drivers.unifi_internal import UnifiInternalClient
from obi.core.engine import unifi_ingest
from obi.core.models import SourceOutcome
from tests.unifi_fakes import FakeUnifiInternalClient, sample_events, unifi_internal_settings

_NOW = datetime.fromtimestamp(1_782_444_500, tz=UTC)


def _cfg(tmp_path: Path) -> tuple[Config, AuditTrail]:
    config = Config(
        unifi_internal=unifi_internal_settings(),
        logs=LogsSettings(db_path=tmp_path / "logs.db", log_tz="UTC"),
        audit_path=tmp_path / "audit.jsonl",
    )
    return config, AuditTrail(config.audit_path)


def test_one_site_fails_others_proceed(tmp_path: Path) -> None:
    config, audit = _cfg(tmp_path)
    fake = FakeUnifiInternalClient(
        config.unifi_internal,  # type: ignore[arg-type]
        sites={"Alder": "g", "Bad": "b"},
        events={"g": sample_events()},
        site_errors={"b": UnifiError(SourceOutcome.UNREACHABLE, "boom")},
        ignore_window=True,
    )
    report = unifi_ingest(config, audit=audit, now=_NOW, client=fake)

    assert report.outcome is SourceOutcome.REACHABLE  # at least one site ingested (FR-008)
    by_site = {s.site: s for s in report.sites}
    assert by_site["Alder"].inserted == 2
    assert by_site["Bad"].outcome is SourceOutcome.UNREACHABLE

    conn = logstore.connect_readonly(config.logs.db_path)  # type: ignore[union-attr]
    try:
        # The failed site's watermark was not advanced (FR-007); the healthy one was.
        assert logstore.get_unifi_watermark(conn, "Bad") is None
        assert logstore.get_unifi_watermark(conn, "Alder") is not None
    finally:
        conn.close()


def test_sites_listing_failure_is_a_clean_nonerror(tmp_path: Path) -> None:
    config, audit = _cfg(tmp_path)
    fake = FakeUnifiInternalClient(
        config.unifi_internal,  # type: ignore[arg-type]
        sites_error=UnifiError(SourceOutcome.AUTH_FAILED, "bad creds"),
    )
    report = unifi_ingest(config, audit=audit, now=_NOW, client=fake)
    assert report.outcome is SourceOutcome.AUTH_FAILED
    assert report.sites == []  # nothing ingested, no crash


def test_malformed_event_is_skipped_and_counted(tmp_path: Path) -> None:
    config, audit = _cfg(tmp_path)
    events = {
        "g": [
            {
                "id": "ok1",
                "timestamp": 1_782_444_400_000,
                "category": "POWER",
                "key": "X",
                "severity": "HIGH",
                "message_raw": "m",
            },
            {"category": "POWER"},  # no id / timestamp -> skipped
        ]
    }
    fake = FakeUnifiInternalClient(
        config.unifi_internal,  # type: ignore[arg-type]
        sites={"S": "g"},
        events=events,
        ignore_window=True,
    )
    report = unifi_ingest(config, audit=audit, now=_NOW, client=fake)
    site = report.sites[0]
    assert site.inserted == 1
    assert site.skipped == 1


def test_client_relogs_in_on_401_then_succeeds() -> None:
    settings = unifi_internal_settings()
    state = {"sys_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/auth/login":
            return httpx.Response(200, headers={"x-csrf-token": "c"}, json={})
        if path.endswith("/system-log/all"):
            state["sys_calls"] += 1
            if state["sys_calls"] == 1:
                return httpx.Response(401, json={"meta": {"rc": "error"}})
            return httpx.Response(200, json={"data": sample_events()})
        return httpx.Response(404, json={})

    def factory() -> httpx.Client:
        return httpx.Client(base_url=settings.base_url, transport=httpx.MockTransport(handler))

    with UnifiInternalClient(settings, client_factory=factory) as client:
        rows = client.fetch_events("g", 0, 9_999_999_999_999)
    assert len(rows) == 2
    assert state["sys_calls"] == 2  # one 401 + one successful retry after re-login (FR-009)

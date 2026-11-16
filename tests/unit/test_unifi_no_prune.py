"""T021: UniFi events accumulate with no pruning (FR-006); CLIENT_DEVICES is excluded at request."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from obi.core import logstore
from obi.core.audit import AuditTrail
from obi.core.config import DEFAULT_UNIFI_EVENT_CATEGORIES, Config, LogsSettings
from obi.core.engine import unifi_ingest
from tests.unifi_fakes import FakeUnifiInternalClient, unifi_internal_settings

_NOW = datetime.fromtimestamp(1_782_444_500, tz=UTC)


def _event(event_id: str, ts_ms: int, message: str) -> dict:
    return {
        "id": event_id,
        "timestamp": ts_ms,
        "category": "POWER",
        "key": "POWER_EVENT",
        "severity": "HIGH",
        "message_raw": message,
    }


def _cfg(tmp_path: Path) -> tuple[Config, AuditTrail]:
    config = Config(
        unifi_internal=unifi_internal_settings(),
        logs=LogsSettings(db_path=tmp_path / "logs.db", log_tz="UTC"),
        audit_path=tmp_path / "audit.jsonl",
    )
    return config, AuditTrail(config.audit_path)


def test_repeated_ingests_accumulate_without_pruning(tmp_path: Path) -> None:
    config, audit = _cfg(tmp_path)
    older = [_event("a1", 1_782_444_000_000, "old power event")]
    unifi_ingest(
        config,
        audit=audit,
        now=_NOW,
        client=FakeUnifiInternalClient(
            config.unifi_internal,  # type: ignore[arg-type]
            sites={"S": "g"},
            events={"g": older},
            ignore_window=True,
        ),
    )
    # A later cycle brings a new event; the old one must still be present (nothing prunes it).
    newer = older + [_event("a2", 1_782_444_400_000, "new power event")]
    unifi_ingest(
        config,
        audit=audit,
        now=_NOW,
        client=FakeUnifiInternalClient(
            config.unifi_internal,  # type: ignore[arg-type]
            sites={"S": "g"},
            events={"g": newer},
            ignore_window=True,
        ),
    )

    conn = logstore.connect_readonly(config.logs.db_path)  # type: ignore[union-attr]
    try:
        rows = logstore.query_events(conn, host="S", limit=50)
    finally:
        conn.close()
    assert {r.message for r in rows} == {"old power event", "new power event"}


def test_default_categories_exclude_client_devices() -> None:
    # The request-side guarantee (FR-005): the default categories never include CLIENT_DEVICES
    # (the client omits it from the system-log request — see test_unifi_internal_client).
    assert "CLIENT_DEVICES" not in DEFAULT_UNIFI_EVENT_CATEGORIES

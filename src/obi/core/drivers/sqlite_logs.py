"""SQLite-backed :class:`LogSource` — strictly read-only (Constitution Principles I, IV).

Queries the local syslog store that ``obi logs ingest`` populates (see
:mod:`obi.core.logstore`), applying the host / time-window / severity / full-text filters and
returning a bounded, newest-first :class:`LogReport`. It opens the database **read-only**
(``mode=ro``) and issues only ``SELECT``s — the query path never writes. Total by contract: a
missing or unreadable store yields an empty report carrying the store-level outcome, never an
exception, so a chat conversation degrades cleanly instead of crashing.

This is the first :class:`LogSource` implementation; a VictoriaLogs-backed one could replace it
behind the same Protocol without touching the engine or chat tool.
"""

from __future__ import annotations

from datetime import datetime

from obi.core import logstore
from obi.core.config import LogsSettings
from obi.core.models import LogReport, SourceOutcome


class SqliteLogSource:
    """Read-only adapter over the local SQLite syslog store implementing :class:`LogSource`."""

    name = "logs"

    def __init__(self, settings: LogsSettings) -> None:
        self._settings = settings

    def query(
        self,
        *,
        host: str | None,
        since: datetime | None,
        until: datetime | None,
        severity: str | None,
        contains: str | None,
        limit: int,
        timeout: float,
        source: str | None = None,
    ) -> LogReport:
        """READ-ONLY, TOTAL, bounded. Apply the filters and return at most ``limit`` events."""

        bounded = max(1, min(limit, self._settings.max_limit))
        db_path = self._settings.db_path
        if not db_path.exists():
            return LogReport(
                [],
                SourceOutcome.UNREACHABLE,
                f"no log store at {db_path} — run `obi logs ingest` after syncing the mirror",
                query_host=host,
                query_severity=severity,
            )
        try:
            conn = logstore.connect_readonly(db_path)
            try:
                conn.execute(f"PRAGMA busy_timeout = {int(max(0.0, timeout) * 1000)}")
                events = logstore.query_events(
                    conn,
                    host=host,
                    since=since,
                    until=until,
                    severity=severity,
                    contains=contains,
                    source=source,
                    limit=bounded,
                )
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 - the source MUST be total
            return LogReport(
                [],
                SourceOutcome.UNREACHABLE,
                f"log store unavailable: {exc}",
                query_host=host,
                query_severity=severity,
            )
        return LogReport(
            events,
            SourceOutcome.REACHABLE,
            truncated=len(events) >= bounded,
            query_host=host,
            query_severity=severity,
        )

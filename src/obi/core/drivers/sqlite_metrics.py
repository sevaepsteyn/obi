"""SQLite-backed :class:`MetricsHistorySource` — strictly read-only (Constitution Principles I, IV).

Queries the local time-series store that ``obi metrics ingest`` populates (see
:mod:`obi.core.metricstore`), summarizing each matching series over a window: counters as
reset-aware per-second rates, gauges as values. It opens the database **read-only** (``mode=ro``)
and issues only ``SELECT``s — and, unlike every other source kind, it contacts **no device at all**,
which is what exempts the tool above it from the live-call budget.

Total by contract: a missing or unreadable store yields an empty report carrying the store-level
outcome, never an exception, so a chat conversation degrades cleanly instead of crashing.

This is the first :class:`MetricsHistorySource` implementation; a Prometheus/VictoriaMetrics-backed
one could replace it behind the same Protocol without touching the engine or chat tool.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING

from obi.core import metricstore
from obi.core.config import SnmpHistorySettings
from obi.core.models import MetricsHistoryReport, SourceOutcome

if TYPE_CHECKING:  # sqlite3 stays quarantined in the store modules — this is a type only
    import sqlite3


def _us(when: datetime) -> int:
    return int(when.timestamp() * 1_000_000)


class SqliteMetricsHistorySource:
    """Read-only adapter over the local SQLite metrics store implementing
    :class:`~obi.core.drivers.base.MetricsHistorySource`."""

    name = "metrics-history"

    def __init__(self, settings: SnmpHistorySettings) -> None:
        self._settings = settings

    def summarize(
        self,
        *,
        device: str,
        metrics: Sequence[str],
        ifname_contains: str | None,
        since: datetime,
        until: datetime,
        limit: int,
    ) -> MetricsHistoryReport:
        """READ-ONLY, TOTAL, bounded. Summarize each stored series for ``device`` in the window."""

        db_path = self._settings.db_path
        if not db_path.exists():
            return MetricsHistoryReport(
                outcome=SourceOutcome.UNREACHABLE,
                detail=(
                    f"no metrics store at {db_path} — collection has not run "
                    "(`obi metrics ingest`)"
                ),
            )
        try:
            conn = metricstore.connect_readonly(db_path)
            try:
                summaries = metricstore.query_summaries(
                    conn,
                    device=device,
                    metrics=metrics,
                    since_us=_us(since),
                    until_us=_us(until),
                    ifname_contains=ifname_contains,
                )
                if summaries:
                    bounded = max(1, limit)
                    return MetricsHistoryReport(
                        summaries=summaries[:bounded],
                        outcome=SourceOutcome.REACHABLE,
                        truncated=len(summaries) > bounded,
                        matched_series=len(summaries),
                    )
                # Nothing matched. Diagnose WHY once, here, where the store is already open —
                # the caller turns this into a sentence instead of an unhelpful silence, and a
                # silence must never be rendered as zero traffic (spec FR-011).
                return self._diagnose_empty(
                    conn, device=device, metrics=metrics, ifname_contains=ifname_contains
                )
            finally:
                conn.close()
        except metricstore.StoreError as exc:
            return MetricsHistoryReport(
                outcome=SourceOutcome.UNREACHABLE,
                detail=f"metrics store unreadable: {exc}",
            )

    def _diagnose_empty(
        self,
        conn: sqlite3.Connection,
        *,
        device: str,
        metrics: Sequence[str],
        ifname_contains: str | None,
    ) -> MetricsHistoryReport:
        """Fill exactly ONE explanatory field for a zero-match read (research.md D6).

        Ordered most- to least-specific: an unknown device explains everything else, so it is
        checked first; only then can an unmatched interface filter be the cause; failing both, the
        device and interface exist and the WINDOW is what came up empty.
        """

        known = metricstore.devices(conn)
        if device not in known:
            return MetricsHistoryReport(
                outcome=SourceOutcome.REACHABLE, known_devices=known
            )
        ifnames = metricstore.interfaces_for(conn, device, metrics=metrics)
        if (
            ifname_contains is not None
            and ifnames
            and not any(ifname_contains.lower() in name.lower() for name in ifnames)
        ):
            return MetricsHistoryReport(
                outcome=SourceOutcome.REACHABLE, known_ifnames=ifnames
            )
        return MetricsHistoryReport(
            outcome=SourceOutcome.REACHABLE, store_oldest_us=metricstore.oldest_us(conn)
        )

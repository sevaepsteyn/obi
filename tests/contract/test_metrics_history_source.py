"""Contract (feature 090, T006/T009): ``MetricsHistorySource`` and its SQLite implementation.

Asserts the guarantees in ``specs/090-metrics-history-tool/contracts/metrics-history-source.md``:
READ-ONLY (and, uniquely among source kinds, contacts NO device), TOTAL (never raises — a missing
or corrupt store degrades to an ``UNREACHABLE`` report), BOUNDED independently of the window, and
the zero-match diagnosis that keeps "no data" from ever being rendered as "no traffic".

Also covers the single ``metrics_history`` audit record ``engine.query_metrics_history`` writes.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from obi.core import metricstore
from obi.core.audit import AuditTrail
from obi.core.config import SnmpHistorySettings
from obi.core.drivers.base import MetricsHistorySource
from obi.core.drivers.sqlite_metrics import SqliteMetricsHistorySource
from obi.core.engine import query_metrics_history
from obi.core.metricstore import StoredSample
from obi.core.models import SourceOutcome

_BASE = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
_DEVICE = "tamarack-internet-gw"


def _sample(
    ts_off: float, value: int, *, ifindex: str = "1", ifname: str = "ether1"
) -> StoredSample:
    t = int((_BASE.timestamp() + ts_off) * 1_000_000)
    return StoredSample(
        ts_us=t,
        ts_iso=datetime.fromtimestamp(t / 1_000_000, tz=UTC).isoformat(),
        device=_DEVICE,
        address="172.18.81.1",
        profile="interfaces",
        metric="ifHCInOctets",
        ifindex=ifindex,
        ifname=ifname,
        kind="counter64",
        value=value,
    )


def _seed(tmp_path: Path, rows: list[StoredSample]) -> Path:
    db_path = tmp_path / "metrics.db"
    conn = metricstore.connect(db_path)
    metricstore.insert_samples(conn, rows)
    conn.commit()
    conn.close()
    return db_path


def _source(db_path: Path) -> SqliteMetricsHistorySource:
    return SqliteMetricsHistorySource(SnmpHistorySettings(db_path=db_path))


def _summarize(source: SqliteMetricsHistorySource, **overrides: object):
    kwargs: dict[str, object] = {
        "device": _DEVICE,
        "metrics": ("ifHCInOctets",),
        "ifname_contains": None,
        "since": _BASE - timedelta(seconds=1),
        "until": _BASE + timedelta(hours=1),
        "limit": 40,
    }
    kwargs.update(overrides)
    return source.summarize(**kwargs)  # type: ignore[arg-type]


# --- protocol conformance ---------------------------------------------------------------------


def test_sqlite_source_satisfies_the_protocol(tmp_path: Path) -> None:
    assert isinstance(_source(tmp_path / "metrics.db"), MetricsHistorySource)
    assert _source(tmp_path / "metrics.db").name == "metrics-history"


# --- TOTAL ------------------------------------------------------------------------------------


def test_missing_store_degrades_instead_of_raising(tmp_path: Path) -> None:
    report = _summarize(_source(tmp_path / "absent.db"))
    assert report.outcome is SourceOutcome.UNREACHABLE
    assert report.summaries == []
    assert "absent.db" in (report.detail or "")


def test_corrupt_store_degrades_instead_of_raising(tmp_path: Path) -> None:
    db_path = tmp_path / "metrics.db"
    db_path.write_text("this is not a database")
    report = _summarize(_source(db_path))
    assert report.outcome is SourceOutcome.UNREACHABLE
    assert report.summaries == []
    assert report.detail  # a short, non-secret diagnostic


def test_store_without_the_schema_degrades_instead_of_raising(tmp_path: Path) -> None:
    # A real SQLite file that simply has no `samples` table (e.g. an unrelated database).
    db_path = tmp_path / "metrics.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE unrelated (x INTEGER)")
    conn.commit()
    conn.close()
    report = _summarize(_source(db_path))
    assert report.outcome is SourceOutcome.UNREACHABLE
    assert report.summaries == []


# --- REACHABLE-but-empty, and the diagnosis ---------------------------------------------------


def test_unknown_device_is_reachable_with_the_known_device_list(tmp_path: Path) -> None:
    db_path = _seed(tmp_path, [_sample(0, 0), _sample(10, 1_000)])
    report = _summarize(_source(db_path), device="tamarack-sw")
    # A readable store that matched nothing is REACHABLE — distinct from an unavailable store.
    assert report.outcome is SourceOutcome.REACHABLE
    assert report.summaries == []
    assert report.known_devices == [_DEVICE]
    assert report.known_ifnames == [] and report.store_oldest_us is None


def test_unmatched_interface_is_reported_with_the_interfaces_that_exist(tmp_path: Path) -> None:
    db_path = _seed(tmp_path, [_sample(0, 0), _sample(10, 1_000)])
    report = _summarize(_source(db_path), ifname_contains="sycamore")
    assert report.outcome is SourceOutcome.REACHABLE
    assert report.known_ifnames == ["ether1"]
    assert report.known_devices == []


def test_empty_window_reports_the_oldest_reading_the_store_holds(tmp_path: Path) -> None:
    db_path = _seed(tmp_path, [_sample(0, 0), _sample(10, 1_000)])
    report = _summarize(
        _source(db_path),
        since=_BASE - timedelta(days=2),
        until=_BASE - timedelta(days=1),
    )
    assert report.outcome is SourceOutcome.REACHABLE
    assert report.summaries == []
    assert report.store_oldest_us == int(_BASE.timestamp() * 1_000_000)
    assert report.known_devices == [] and report.known_ifnames == []


# --- BOUNDED ----------------------------------------------------------------------------------


def test_result_is_capped_and_reports_what_it_dropped(tmp_path: Path) -> None:
    rows: list[StoredSample] = []
    for i in range(1, 11):
        rows += [
            _sample(0, 0, ifindex=str(i), ifname=f"ether{i}"),
            _sample(10, i * 1_000, ifindex=str(i), ifname=f"ether{i}"),
        ]
    report = _summarize(_source(_seed(tmp_path, rows)), limit=3)
    assert len(report.summaries) == 3
    assert report.matched_series == 10
    assert report.truncated is True
    # busiest first survives the cap
    assert [s.ifname for s in report.summaries] == ["ether10", "ether9", "ether8"]


def test_a_long_window_returns_no_more_data_than_a_short_one(tmp_path: Path) -> None:
    # The window-independence guarantee (FR-008): summaries are fixed-size records, not points.
    rows = [_sample(i * 300, i * 1_000_000) for i in range(288)]  # 24 h at 5-minute cadence
    source = _source(_seed(tmp_path, rows))
    short = _summarize(source, until=_BASE + timedelta(minutes=30))
    long = _summarize(source, until=_BASE + timedelta(days=90))
    assert len(long.summaries) == len(short.summaries) == 1
    assert len(json.dumps(long.summaries[0].__dict__)) < 400  # a handful of numbers, not a series


# --- the audit record (T009) ------------------------------------------------------------------


def _records(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_engine_writes_exactly_one_audit_record_with_the_question_as_intent(
    tmp_path: Path,
) -> None:
    db_path = _seed(tmp_path, [_sample(0, 0), _sample(10, 1_000_000)])
    audit_path = tmp_path / "audit.jsonl"
    report = query_metrics_history(
        _source(db_path),
        audit=AuditTrail(audit_path),
        db_label=str(db_path),
        device=_DEVICE,
        metrics=("ifHCInOctets",),
        since=_BASE - timedelta(seconds=1),
        until=_BASE + timedelta(hours=1),
        ifname_contains="ether",
        reason="how much traffic on ether1?",
    )
    assert report.outcome is SourceOutcome.REACHABLE

    records = _records(audit_path)
    assert len(records) == 1
    (record,) = records
    assert record["action"] == "metrics_history"
    assert record["reason"] == "how much traffic on ether1?"
    assert record["outcome"] == "ok"
    params = record["params"]
    assert isinstance(params, dict)
    assert params["device"] == _DEVICE
    assert params["interface"] == "ether"
    assert params["matched"] == 1 and params["returned"] == 1
    # The SHAPE of the query is audited, never the measured values.
    assert "100000" not in json.dumps(record)


def test_unreadable_store_is_audited_as_unreachable(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    query_metrics_history(
        _source(tmp_path / "absent.db"),
        audit=AuditTrail(audit_path),
        db_label=str(tmp_path / "absent.db"),
        device=_DEVICE,
        metrics=("ifHCInOctets",),
        since=_BASE,
        until=_BASE + timedelta(hours=1),
    )
    (record,) = _records(audit_path)
    assert record["outcome"] == "unreachable"
    assert record["detail"]

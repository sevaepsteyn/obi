"""Unit (feature 059): the shared schema-versioning applier.

Covers the guarantee that closed the field-break — a store's on-disk shape converging to the code's
expectation whether the database is fresh, already at the current version, or predates version
stamping (the ``no such column`` scenario the old ``executescript(IF NOT EXISTS)`` would have hit
in production). Also asserts the three real stores stamp a version on ``connect``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from obi.core import auditstore, logstore, metricstore, storebase

_BASE = "CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, a TEXT NOT NULL);"
_ADD_B = "ALTER TABLE t ADD COLUMN b TEXT;"
_ADD_C = "ALTER TABLE t ADD COLUMN c TEXT;"


def _version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _columns(conn: sqlite3.Connection) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(t)")}


def test_fresh_db_stamped_v1_when_no_migrations() -> None:
    conn = sqlite3.connect(":memory:")
    result = storebase.apply_schema(conn, _BASE)
    assert result == 1
    assert _version(conn) == 1
    assert _columns(conn) == {"id", "a"}


def test_idempotent_second_call_is_a_noop() -> None:
    conn = sqlite3.connect(":memory:")
    storebase.apply_schema(conn, _BASE, (_ADD_B,))
    conn.execute("INSERT INTO t (a, b) VALUES ('x', 'y')")
    # Re-running must not re-apply the migration (a repeat ALTER raises "duplicate column").
    result = storebase.apply_schema(conn, _BASE, (_ADD_B,))
    assert result == 2
    assert _version(conn) == 2
    assert conn.execute("SELECT a, b FROM t").fetchone() == ("x", "y")


def test_migration_applied_once_and_in_order() -> None:
    conn = sqlite3.connect(":memory:")
    result = storebase.apply_schema(conn, _BASE, (_ADD_B, _ADD_C))
    assert result == 3
    assert _version(conn) == 3
    assert _columns(conn) == {"id", "a", "b", "c"}


def test_preversioning_db_treated_as_v1_then_migrated() -> None:
    """The regression this feature exists for: a store created before version stamping.

    Its tables already exist and ``user_version`` is 0. A later code version adds a column via a
    migration; without the applier the old code path (``executescript(IF NOT EXISTS)``) would leave
    the column absent and every read of it would raise ``no such column`` in the field.
    """

    conn = sqlite3.connect(":memory:")
    # Simulate the old, unversioned on-disk state: base schema applied directly, version still 0.
    conn.executescript(_BASE)
    conn.execute("INSERT INTO t (a) VALUES ('legacy-row')")
    assert _version(conn) == 0
    assert "b" not in _columns(conn)

    # New code ships a migration.
    result = storebase.apply_schema(conn, _BASE, (_ADD_B,))
    assert result == 2
    assert _version(conn) == 2
    assert "b" in _columns(conn)
    # The pre-existing data survives, with the new column NULL.
    assert conn.execute("SELECT a, b FROM t").fetchone() == ("legacy-row", None)


def test_partial_upgrade_only_runs_the_new_migration() -> None:
    conn = sqlite3.connect(":memory:")
    storebase.apply_schema(conn, _BASE, (_ADD_B,))  # now at v2
    assert _version(conn) == 2
    # A later release appends a second migration; only it should run (v2 -> v3), not _ADD_B again.
    result = storebase.apply_schema(conn, _BASE, (_ADD_B, _ADD_C))
    assert result == 3
    assert _columns(conn) == {"id", "a", "b", "c"}


def test_real_stores_stamp_a_version_on_connect(tmp_path: Path) -> None:
    """Each production store routes ``connect`` through the applier, so a fresh DB is at v1+."""

    log_conn = logstore.connect(tmp_path / "logs.db")
    try:
        assert _version(log_conn) >= 1
    finally:
        log_conn.close()

    metric_conn = metricstore.connect(tmp_path / "metrics.db")
    try:
        assert _version(metric_conn) >= 1
    finally:
        metric_conn.close()

    audit_conn = auditstore.connect(tmp_path / "audit.db")
    try:
        assert _version(audit_conn) >= 1
    finally:
        audit_conn.close()

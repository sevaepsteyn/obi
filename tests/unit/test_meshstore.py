"""Unit (feature 117): the mesh position-history store — schema, dedupe, queries.

All against tmp-path databases; no serial port, no radio, nothing transmitted. Pins the
dedupe contract (SC-004: re-observing an unchanged fix adds no rows, including two
connections racing) and the effective-fix-instant rule that keeps garbage device clocks
from producing absurd ages.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from obi.core.meshstore import (
    RecordedFix,
    StoreError,
    connect,
    connect_readonly,
    effective_fix_s,
    last_known,
    record_fix,
    track,
)

_BUGA = "!91378e3a"


def _fix(
    *,
    node_id: str = _BUGA,
    latitude: float = 40.7797341,
    longitude: float = -119.2150581,
    altitude_m: int | None = 1191,
    fix_time_s: int = 1_787_990_000,
    observed_at_s: int = 1_787_990_100,
    hops_away: int | None = 0,
    snr_db: float | None = 11.75,
    address: str = "6:25 & Esplanade",
) -> RecordedFix:
    return RecordedFix(
        node_id=node_id,
        latitude=latitude,
        longitude=longitude,
        altitude_m=altitude_m,
        fix_time_s=fix_time_s,
        observed_at_s=observed_at_s,
        hops_away=hops_away,
        snr_db=snr_db,
        address=address,
    )


# --- schema ------------------------------------------------------------------------------


def test_connect_creates_schema_and_stamps_version(tmp_path: Path) -> None:
    conn = connect(tmp_path / "nested" / "positions.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    columns = {row[1] for row in conn.execute("PRAGMA table_info(position_fix)")}
    assert {
        "node_id",
        "lat_udeg",
        "lon_udeg",
        "altitude_m",
        "fix_time_s",
        "observed_at_s",
        "hops_away",
        "snr_db",
        "address",
    } <= columns
    conn.close()


def test_store_error_is_the_sqlite_alias(tmp_path: Path) -> None:
    assert StoreError is sqlite3.Error


# --- recording + dedupe ------------------------------------------------------------------


def test_record_fix_round_trips_every_field(tmp_path: Path) -> None:
    conn = connect(tmp_path / "p.db")
    assert record_fix(conn, _fix()) is True
    got = last_known(conn, _BUGA)
    assert got is not None
    assert got.node_id == _BUGA
    assert got.latitude == pytest.approx(40.7797341)
    assert got.longitude == pytest.approx(-119.2150581)
    assert got.altitude_m == 1191
    assert got.fix_time_s == 1_787_990_000
    assert got.observed_at_s == 1_787_990_100
    assert got.hops_away == 0
    assert got.snr_db == pytest.approx(11.75)
    assert got.address == "6:25 & Esplanade"
    conn.close()


def test_reobserving_the_same_fix_adds_no_row(tmp_path: Path) -> None:
    conn = connect(tmp_path / "p.db")
    assert record_fix(conn, _fix()) is True
    # A later read of the unchanged radio table: same fix, later observation clock.
    assert record_fix(conn, _fix(observed_at_s=1_787_990_700)) is False
    assert conn.execute("SELECT COUNT(*) FROM position_fix").fetchone()[0] == 1
    conn.close()


def test_two_connections_racing_converge_on_one_row(tmp_path: Path) -> None:
    # The sweep and a chat query observing the same new fix concurrently.
    a = connect(tmp_path / "p.db")
    b = connect(tmp_path / "p.db")
    assert record_fix(a, _fix()) is True
    assert record_fix(b, _fix()) is False
    assert a.execute("SELECT COUNT(*) FROM position_fix").fetchone()[0] == 1
    a.close()
    b.close()


def test_fix_without_device_time_stores_zero_and_still_dedupes(tmp_path: Path) -> None:
    conn = connect(tmp_path / "p.db")
    assert record_fix(conn, _fix(fix_time_s=0)) is True
    assert record_fix(conn, _fix(fix_time_s=0, observed_at_s=1_787_991_000)) is False
    assert conn.execute("SELECT COUNT(*) FROM position_fix").fetchone()[0] == 1
    conn.close()


def test_a_moved_fix_is_a_new_row(tmp_path: Path) -> None:
    conn = connect(tmp_path / "p.db")
    assert record_fix(conn, _fix()) is True
    assert record_fix(conn, _fix(latitude=40.78, fix_time_s=1_787_990_600)) is True
    assert conn.execute("SELECT COUNT(*) FROM position_fix").fetchone()[0] == 2
    conn.close()


# --- effective fix instant ---------------------------------------------------------------


def test_effective_fix_prefers_a_sane_device_time() -> None:
    assert effective_fix_s(1_787_990_000, 1_787_990_100) == 1_787_990_000


def test_effective_fix_falls_back_on_zero_and_garbage() -> None:
    assert effective_fix_s(0, 1_787_990_100) == 1_787_990_100  # device gave none
    # Device clock in the future beyond skew allowance: the observation clock is honest.
    assert effective_fix_s(1_999_999_999, 1_787_990_100) == 1_787_990_100
    # Small forward skew (≤ 5 min) is accepted as a fix time.
    assert effective_fix_s(1_787_990_200, 1_787_990_100) == 1_787_990_200


# --- last known --------------------------------------------------------------------------


def test_last_known_empty_store_is_none(tmp_path: Path) -> None:
    conn = connect(tmp_path / "p.db")
    assert last_known(conn, _BUGA) is None
    conn.close()


def test_last_known_picks_greatest_effective_instant(tmp_path: Path) -> None:
    conn = connect(tmp_path / "p.db")
    record_fix(conn, _fix(fix_time_s=1_787_990_000, observed_at_s=1_787_990_100))
    # Garbage future fix_time: effective instant = its observation time (earlier).
    record_fix(
        conn,
        _fix(latitude=40.781, fix_time_s=1_999_999_999, observed_at_s=1_787_989_000),
    )
    got = last_known(conn, _BUGA)
    assert got is not None
    assert got.fix_time_s == 1_787_990_000
    conn.close()


def test_last_known_is_per_node(tmp_path: Path) -> None:
    conn = connect(tmp_path / "p.db")
    record_fix(conn, _fix())
    assert last_known(conn, "!deadbeef") is None
    conn.close()


# --- track -------------------------------------------------------------------------------


def test_track_is_ascending_and_range_bounded(tmp_path: Path) -> None:
    conn = connect(tmp_path / "p.db")
    for offset in (300, 100, 200):
        record_fix(
            conn,
            _fix(
                latitude=40.77 + offset * 1e-5,
                fix_time_s=1_787_990_000 + offset,
                observed_at_s=1_787_990_050 + offset,
            ),
        )
    fixes = track(conn, _BUGA, since_s=1_787_990_000, until_s=1_787_990_250)
    assert [f.fix_time_s for f in fixes] == [1_787_990_100, 1_787_990_200]
    conn.close()


def test_track_empty_range_is_empty_list(tmp_path: Path) -> None:
    conn = connect(tmp_path / "p.db")
    record_fix(conn, _fix())
    assert track(conn, _BUGA, since_s=1, until_s=2) == []
    conn.close()


def test_readonly_connection_reads_but_cannot_write(tmp_path: Path) -> None:
    path = tmp_path / "p.db"
    rw = connect(path)
    record_fix(rw, _fix())
    ro = connect_readonly(path)
    assert last_known(ro, _BUGA) is not None
    with pytest.raises(StoreError):
        record_fix(ro, _fix(latitude=40.9, fix_time_s=1))
    rw.close()
    ro.close()

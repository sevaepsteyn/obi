"""Unit (feature 072): the ICMP reachability state store (``icmpstore``).

Schema versioning, insert-or-replace upsert, round-trip read, and prune-absent — plus that a
read-only open on a missing file raises (so the watch pass's guard returns ``[]``).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from obi.core import icmpstore
from obi.core.icmpstore import DeviceState


def _state(device: str, *, up: bool, misses: int = 0, now: int = 1_000) -> DeviceState:
    return DeviceState(
        device=device,
        address=f"172.25.110.{len(device)}",
        up=up,
        consecutive_misses=misses,
        last_seen_us=now if up else None,
        last_change_us=now,
        updated_us=now,
    )


def test_schema_at_version_2(tmp_path: Path) -> None:
    # v2 = feature 095's last_rtt_us migration on top of the frozen v1 schema.
    conn = icmpstore.connect(tmp_path / "icmp.db")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert version == 2


def _make_v1_store(path: Path) -> None:
    """A field-realistic v1 database: the frozen base schema, no rtt column, one row."""

    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE device_state ("
        "device TEXT PRIMARY KEY, address TEXT NOT NULL, up INTEGER NOT NULL, "
        "consecutive_misses INTEGER NOT NULL, last_seen_us INTEGER, "
        "last_change_us INTEGER NOT NULL, updated_us INTEGER NOT NULL)"
    )
    conn.execute("PRAGMA user_version = 1")
    conn.execute(
        "INSERT INTO device_state VALUES ('core-gw-1', '172.18.84.1', 1, 0, 1000, 1000, 1000)"
    )
    conn.commit()
    conn.close()


def test_v1_store_migrates_once_and_reads_null_rtt(tmp_path: Path) -> None:
    db = tmp_path / "icmp.db"
    _make_v1_store(db)
    conn = icmpstore.connect(db)  # applies the v1→v2 migration
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    states = icmpstore.load_states(conn)
    assert states[0].last_rtt_us is None  # pre-migration row reads as unmeasured
    conn.close()
    conn = icmpstore.connect(db)  # reconnect: migration must not re-apply (run-once)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    conn.close()


def test_readonly_v1_store_reads_null_rtt_without_error(tmp_path: Path) -> None:
    # mode=ro never migrates: until the collector's next connect() upgrades the store, readers
    # (watch pass, obi icmp status, the dash cycle) must see NULL rtt rather than erroring.
    db = tmp_path / "icmp.db"
    _make_v1_store(db)
    conn = icmpstore.connect_readonly(db)
    states = icmpstore.load_states(conn)
    conn.close()
    assert [s.device for s in states] == ["core-gw-1"]
    assert states[0].last_rtt_us is None


def test_upsert_inserts_then_updates_in_place(tmp_path: Path) -> None:
    conn = icmpstore.connect(tmp_path / "icmp.db")
    icmpstore.upsert_state(conn, [_state("core-gw-1", up=True, now=1_000)])
    conn.commit()
    icmpstore.upsert_state(
        conn, [_state("core-gw-1", up=False, misses=1, now=2_000)]
    )  # same PK → update
    conn.commit()
    states = icmpstore.load_states(conn)
    conn.close()
    assert len(states) == 1  # one row, not two
    assert states[0].up is False
    assert states[0].consecutive_misses == 1
    assert states[0].updated_us == 2_000


def test_load_states_round_trips_all_fields(tmp_path: Path) -> None:
    conn = icmpstore.connect(tmp_path / "icmp.db")
    row = DeviceState(
        device="chestnut-sw",
        address="172.18.81.20",
        up=False,
        consecutive_misses=3,
        last_seen_us=None,
        last_change_us=5_000,
        updated_us=9_000,
        last_rtt_us=2_345,
    )
    icmpstore.upsert_state(conn, [row])
    conn.commit()
    got = icmpstore.load_states(conn)[0]
    conn.close()
    assert got == row


def test_prune_absent_removes_only_unlisted(tmp_path: Path) -> None:
    conn = icmpstore.connect(tmp_path / "icmp.db")
    icmpstore.upsert_state(
        conn,
        [_state("a", up=True), _state("b", up=True), _state("c", up=False)],
    )
    conn.commit()
    removed = icmpstore.prune_absent(conn, ["a", "c"])  # b is gone from the target set
    conn.commit()
    devices = {s.device for s in icmpstore.load_states(conn)}
    conn.close()
    assert removed == 1
    assert devices == {"a", "c"}


def test_prune_empty_keep_clears_store(tmp_path: Path) -> None:
    conn = icmpstore.connect(tmp_path / "icmp.db")
    icmpstore.upsert_state(conn, [_state("a", up=True), _state("b", up=True)])
    conn.commit()
    removed = icmpstore.prune_absent(conn, [])
    conn.commit()
    assert removed == 2
    assert icmpstore.load_states(conn) == []
    conn.close()


def test_connect_readonly_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(sqlite3.Error):
        conn = icmpstore.connect_readonly(tmp_path / "nope.db")
        conn.execute("SELECT 1 FROM device_state")  # ro-open is lazy; the query forces the error

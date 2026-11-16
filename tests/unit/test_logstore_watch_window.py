"""A1 store layer: query_watch_candidates / watch_window_stats / newest_event / like-literal."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from obi.core import logstore
from obi.core.logstore import (
    WindowExclusions,
    _like_literal,
    newest_event,
    query_watch_candidates,
    watch_window_stats,
)
from tests.fakes import seed_logs

NOW = datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC)


def _seed(tmp_path: Path, rows, collector="core"):  # type: ignore[no-untyped-def]
    seed_logs(tmp_path / "mirror", tmp_path / "logs.db", rows, collector=collector)
    return logstore.connect_readonly(tmp_path / "logs.db")


def _rows(n: int):  # type: ignore[no-untyped-def]
    return [
        ("core-1", NOW - timedelta(minutes=n - i), "system,info", f"msg {i}") for i in range(n)
    ]


def test_range_is_half_open_and_ordered(tmp_path: Path) -> None:
    conn = _seed(tmp_path, _rows(5))
    ids = [i for i, _ in query_watch_candidates(
        conn, after_id=0, upto_id=99, limit=100, exclusions=WindowExclusions()
    )]
    assert ids == sorted(ids) and len(ids) == 5  # id-ascending, all five
    # (after_id, upto_id]: after excludes the boundary, upto includes it.
    mid = ids[2]
    assert [i for i, _ in query_watch_candidates(
        conn, after_id=mid, upto_id=ids[3], limit=100, exclusions=WindowExclusions()
    )] == [ids[3]]


def test_limit_takes_oldest_first(tmp_path: Path) -> None:
    conn = _seed(tmp_path, _rows(5))
    got = [i for i, _ in query_watch_candidates(
        conn, after_id=0, upto_id=99, limit=2, exclusions=WindowExclusions()
    )]
    assert got == sorted(got) and len(got) == 2  # the two OLDEST, not newest


def test_ignore_collectors_drops_collector(tmp_path: Path) -> None:
    seed_logs(tmp_path / "mirror", tmp_path / "logs.db", _rows(2), collector="core")
    seed_logs(
        tmp_path / "mirror", tmp_path / "logs.db",
        [("Alder", NOW, "unifi_device,info", "x")], collector="unifi",
    )
    conn = logstore.connect_readonly(tmp_path / "logs.db")
    got = query_watch_candidates(
        conn, after_id=0, upto_id=99, limit=100,
        exclusions=WindowExclusions(ignore_collectors=("unifi",)),
    )
    assert all("unifi" not in e.collectors for _, e in got) and len(got) == 2


def test_ignore_topics_is_token_subset(tmp_path: Path) -> None:
    conn = _seed(tmp_path, [
        ("core-1", NOW - timedelta(minutes=3), "system,info,account", "login"),
        ("core-1", NOW - timedelta(minutes=2), "system,error,critical", "boom"),
        ("core-1", NOW - timedelta(minutes=1), "dhcp,info", "lease"),
    ])
    got = [e.topics for _, e in query_watch_candidates(
        conn, after_id=0, upto_id=99, limit=100,
        exclusions=WindowExclusions(ignore_topics=("system,info,account", "dhcp,info")),
    )]
    assert got == ["system,error,critical"]  # only the non-ignored row survives


def test_allowed_hosts_gate(tmp_path: Path) -> None:
    seed_logs(tmp_path / "mirror", tmp_path / "logs.db", [
        ("core-1", NOW - timedelta(minutes=2), "system,info", "a"),
        ("stranger", NOW - timedelta(minutes=1), "system,info", "b"),
    ], collector="core")
    seed_logs(
        tmp_path / "mirror", tmp_path / "logs.db",
        [("Alder", NOW, "unifi_device,info", "c")], collector="unifi",
    )
    conn = logstore.connect_readonly(tmp_path / "logs.db")
    got = {e.host for _, e in query_watch_candidates(
        conn, after_id=0, upto_id=99, limit=100,
        exclusions=WindowExclusions(allowed_hosts=frozenset({"core-1"})),
    )}
    assert got == {"core-1", "Alder"}  # allowed host + unifi admitted; stranger dropped


def test_empty_allowed_hosts_admits_only_controllers(tmp_path: Path) -> None:
    seed_logs(tmp_path / "mirror", tmp_path / "logs.db",
              [("core-1", NOW, "system,info", "a")], collector="core")
    seed_logs(tmp_path / "mirror", tmp_path / "logs.db",
              [("aspen", NOW, "uisp,log", "b")], collector="uisp")
    conn = logstore.connect_readonly(tmp_path / "logs.db")
    got = {e.host for _, e in query_watch_candidates(
        conn, after_id=0, upto_id=99, limit=100,
        exclusions=WindowExclusions(allowed_hosts=frozenset()),
    )}
    assert got == {"aspen"}  # empty set ⇒ no RouterOS host qualifies; only the uisp row


def test_uisp_ignore_names_only_uisp(tmp_path: Path) -> None:
    seed_logs(tmp_path / "mirror", tmp_path / "logs.db",
              [("aspen", NOW, "system,info", "routeros aspen")], collector="core")
    seed_logs(tmp_path / "mirror", tmp_path / "logs.db",
              [("aspen", NOW, "uisp,log", "uisp aspen")], collector="uisp")
    conn = logstore.connect_readonly(tmp_path / "logs.db")
    got = [e.collectors for _, e in query_watch_candidates(
        conn, after_id=0, upto_id=99, limit=100,
        exclusions=WindowExclusions(uisp_ignore_names=("aspen",)),
    )]
    # only the uisp aspen row is dropped; the RouterOS row that shares the name survives.
    assert got == [["core"]]


def test_like_literal_escapes_wildcards() -> None:
    assert _like_literal("uni%") == "uni\\%"
    assert _like_literal("a_b") == "a\\_b"
    assert _like_literal("x\\y") == "x\\\\y"


def test_like_literal_ignore_value_matches_literally(tmp_path: Path) -> None:
    # An ignore_collectors value with a '%' must be matched literally, never as a wildcard, so it
    # does NOT drop the real "unifi" collector (FR-005 edge case).
    seed_logs(tmp_path / "mirror", tmp_path / "logs.db",
              [("Alder", NOW, "unifi_device,info", "x")], collector="unifi")
    conn = logstore.connect_readonly(tmp_path / "logs.db")
    got = query_watch_candidates(
        conn, after_id=0, upto_id=99, limit=100,
        exclusions=WindowExclusions(ignore_collectors=("uni%",)),
    )
    assert len(got) == 1  # "uni%" is literal, matches nothing → unifi row NOT dropped


def test_window_stats_and_newest(tmp_path: Path) -> None:
    conn = _seed(tmp_path, _rows(4))
    count, oldest = watch_window_stats(conn, after_id=0, upto_id=99, exclusions=WindowExclusions())
    assert count == 4 and oldest is not None
    nid, nts = newest_event(conn)  # type: ignore[misc]
    assert nid == 4 and nts >= oldest
    # a range past the newest is empty.
    assert watch_window_stats(conn, after_id=99, upto_id=99, exclusions=WindowExclusions()) == (
        0, None,
    )


def test_newest_event_none_on_empty(tmp_path: Path) -> None:
    logstore.connect(tmp_path / "logs.db").close()  # create empty store
    conn = logstore.connect_readonly(tmp_path / "logs.db")
    assert newest_event(conn) is None

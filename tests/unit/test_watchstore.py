"""Watcher persistence: cursor/run recovery, report I/O, retention (feature 014, T006)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from obi.core.watch import Evidence, WatchCursor, WatchFinding, WatchRun
from obi.core.watchstore import WatchStore

NOW = datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC)


def _finding(finding_id: str = "f-1", *, created: datetime | None = None) -> WatchFinding:
    when = created or NOW
    return WatchFinding(
        id=finding_id,
        created_at=when,
        device="core-1",
        window_start=when,
        window_end=when,
        severity="warning",
        confidence="high",
        summary="something happened",
        description="a description",
        evidence=[Evidence(kind="log", text="a log line")],
        source="llm",
    )


def _run() -> WatchRun:
    return WatchRun(
        started_at=NOW,
        finished_at=NOW,
        new_event_count=2,
        outcome="findings",
        tokens_input=10,
        tokens_output=5,
        llm_calls=1,
        cursor_after=WatchCursor(id=4),
        finding_ids=["f-1"],
    )


def test_cursor_and_run_roundtrip_recovers_after_restart(tmp_path: Path) -> None:
    store = WatchStore(tmp_path / "watch")
    assert store.load_cursor() is None
    assert store.load_last_run() is None

    store.save_state(WatchCursor(id=4), _run(), open_findings={"baseline|core-1": NOW})

    fresh = WatchStore(tmp_path / "watch")  # simulate a restart
    assert fresh.load_cursor() == WatchCursor(id=4)
    assert fresh.load_open() == {"baseline|core-1": NOW}
    last = fresh.load_last_run()
    assert last is not None
    assert last.tokens_input == 10 and last.outcome == "findings"
    assert len(fresh.recent_runs()) == 1


def test_report_json_and_markdown_io(tmp_path: Path) -> None:
    store = WatchStore(tmp_path / "watch")
    finding = _finding()
    store.write_finding(finding)

    assert (store.reports_dir / "f-1.json").exists()
    assert (store.reports_dir / "f-1.md").exists()

    listed = store.list_findings()
    assert len(listed) == 1 and listed[0].device == "core-1"

    markdown = store.get_markdown("f-1")
    assert markdown is not None and "What happened" in markdown
    assert store.get_markdown("missing") is None


def test_write_finding_display_tz_persists_local_and_preserves_instant(tmp_path: Path) -> None:
    """069: with a display tz, the on-disk .json/.md render in local time (so `obi watch --show`
    reads local), while the round-tripped instant is byte-for-byte the same UTC moment."""

    from zoneinfo import ZoneInfo

    la = ZoneInfo("America/Los_Angeles")
    store = WatchStore(tmp_path / "watch")
    finding = _finding(created=datetime(2026, 7, 13, 19, 4, 1, tzinfo=UTC))  # 12:04:01 PDT

    store.write_finding(finding, display_tz=la)

    stored = (store.reports_dir / "f-1.json").read_text()
    assert "2026-07-13T12:04:01-07:00" in stored  # local wall-clock on disk
    assert "19:04:01+00:00" not in stored
    markdown = store.get_markdown("f-1")
    assert markdown is not None and "**Detected:** 2026-07-13T12:04:01-07:00" in markdown

    # The instant survives the round-trip unchanged — internal comparisons are timezone-blind.
    (loaded,) = store.list_findings()
    assert loaded.created_at == datetime(2026, 7, 13, 19, 4, 1, tzinfo=UTC)


def test_list_filters(tmp_path: Path) -> None:
    store = WatchStore(tmp_path / "watch")
    store.write_finding(_finding("sequoia-1", created=datetime(2026, 6, 1, 12, tzinfo=UTC)))
    store.write_finding(_finding("pine-1", created=NOW))

    assert len(store.list_findings(since=datetime(2026, 6, 20, tzinfo=UTC))) == 1
    assert len(store.list_findings(limit=1)) == 1
    assert len(store.list_findings(device="core")) == 2


def test_prune_drops_old_reports(tmp_path: Path) -> None:
    store = WatchStore(tmp_path / "watch")
    store.write_finding(_finding("sequoia-1", created=datetime(2026, 5, 1, 12, tzinfo=UTC)))
    store.write_finding(_finding("pine-1", created=NOW))

    removed = store.prune(30, now=NOW)
    assert removed == 1
    assert [f.id for f in store.list_findings()] == ["pine-1"]
    assert not (store.reports_dir / "sequoia-1.json").exists()
    assert not (store.reports_dir / "sequoia-1.md").exists()

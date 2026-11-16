"""Liveness: the slack heartbeat file and its interpretation (feature 081)."""

from __future__ import annotations

import os
import time
from pathlib import Path

from obi.core import service as svc


def test_heartbeat_loop_refreshes_while_connected(tmp_path: Path) -> None:
    path = tmp_path / "hb.json"
    hb = svc.Heartbeat(path, name="slack", interval=0.05)
    hb.start()
    try:
        first = svc.read_heartbeat(path)
        assert first is not None
        time.sleep(0.16)  # let the beat loop tick at least once
        later = svc.read_heartbeat(path)
        assert later is not None and later.ts >= first.ts and later.connected is True
    finally:
        hb.stop()


def test_heartbeat_write_read_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "slack.heartbeat.json"
    hb = svc.Heartbeat(path, name="slack")
    hb.start()
    try:
        record = svc.read_heartbeat(path)
        assert record is not None
        assert record.name == "slack"
        assert record.pid == os.getpid()
        assert record.connected is True
    finally:
        hb.stop()
    # stop() marks disconnected so `status` reports "not connected"
    after = svc.read_heartbeat(path)
    assert after is not None and after.connected is False


def test_interpret_heartbeat_states() -> None:
    now = 1000.0
    assert svc.interpret_heartbeat(None, now=now).state == "unknown"

    fresh = svc.HeartbeatRecord("slack", 1, connected=True, ts=now - 5)
    assert svc.interpret_heartbeat(fresh, now=now).state == "connected"

    wedged = svc.HeartbeatRecord("slack", 1, connected=True, ts=now - 999)
    verdict = svc.interpret_heartbeat(wedged, now=now)
    assert verdict.state == "wedged" and verdict.age_s is not None and verdict.age_s > 900

    disconnected = svc.HeartbeatRecord("slack", 1, connected=False, ts=now - 5)
    assert svc.interpret_heartbeat(disconnected, now=now).state == "disconnected"


def test_read_heartbeat_missing_or_corrupt(tmp_path: Path) -> None:
    assert svc.read_heartbeat(tmp_path / "absent.json") is None
    corrupt = tmp_path / "bad.json"
    corrupt.write_text("{ not json")
    assert svc.read_heartbeat(corrupt) is None
    incomplete = tmp_path / "partial.json"
    incomplete.write_text('{"name": "slack"}')
    assert svc.read_heartbeat(incomplete) is None

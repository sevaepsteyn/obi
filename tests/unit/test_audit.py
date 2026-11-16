"""T008: unit tests for the append-only JSONL audit trail."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from obi.core.audit import AuditTrail
from obi.core.models import AuditOutcome, AuditRecord


def _record(action: str, target: str, **params: object) -> AuditRecord:
    return AuditRecord(
        timestamp=datetime.now(UTC),
        action=action,
        target=target,
        reason="operator-initiated status query",
        outcome=AuditOutcome.OK,
        params=params,
    )


def test_appends_one_line_per_action(tmp_path: Path) -> None:
    trail = AuditTrail(tmp_path / "audit.jsonl")
    trail.record(_record("discover", "scope"))
    trail.record(_record("routeros_status", "172.25.110.1"))
    trail.record(_record("routeros_status", "172.25.110.2"))

    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert len(lines) == 3
    parsed = [json.loads(line) for line in lines]
    assert [p["action"] for p in parsed] == ["discover", "routeros_status", "routeros_status"]
    assert parsed[0]["reason"] == "operator-initiated status query"
    assert parsed[1]["outcome"] == "ok"


def test_append_only_preserves_prior_records(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    AuditTrail(path).record(_record("discover", "scope"))
    # A fresh instance (e.g. a later run / after restart) must not truncate.
    AuditTrail(path).record(_record("routeros_status", "172.25.110.1"))
    assert len(path.read_text().splitlines()) == 2


def test_credentials_never_written(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    trail = AuditTrail(path)
    trail.record(
        _record(
            "routeros_status",
            "172.25.110.1",
            username="admin",
            password="fakesecret44028f",
            token="abc123",
            nested={"credentials": "topsecret"},
        )
    )
    text = path.read_text()
    assert "fakesecret44028f" not in text
    assert "topsecret" not in text
    assert "abc123" not in text
    # Non-secret params are preserved.
    record = json.loads(text)
    assert record["params"]["username"] == "admin"
    assert record["params"]["password"] == "***"
    assert record["params"]["nested"]["credentials"] == "***"

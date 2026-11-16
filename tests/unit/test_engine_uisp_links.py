"""Unit tests: ``engine.uisp_links`` (feature 034 — T006).

The engine function must be total (a controller failure becomes a classified outcome on the
report, never an exception), filter by EITHER endpoint name, sort by label, and write exactly one
``uisp_links`` audit record carrying the caller's reason.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from obi.core.audit import AuditTrail
from obi.core.drivers.uisp import UispController, UispError
from obi.core.engine import uisp_links
from obi.core.models import SourceOutcome
from tests.fakes import load_uisp_links_fixture, uisp_settings

_FIXTURES = {
    "/nms/api/v2.1/data-links": load_uisp_links_fixture("data-links"),
    "/nms/api/v2.1/devices": load_uisp_links_fixture("devices"),
}


def _transport(path: str, timeout: float) -> list[dict[str, Any]]:
    return _FIXTURES[path]


def _failing_transport(path: str, timeout: float) -> list[dict[str, Any]]:
    raise UispError(SourceOutcome.AUTH_FAILED, "controller rejected the API token (HTTP 401)")


def _controller(transport: Any = _transport) -> UispController:
    return UispController(uisp_settings(), geo_transport=transport)


def _audit_lines(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_reachable_report_sorted_by_label(tmp_path: Path) -> None:
    audit = AuditTrail(tmp_path / "audit.jsonl")
    report = uisp_links(
        _controller(), audit=audit, controller="https://uisp.example.com", timeout=5.0
    )
    assert report.outcome is SourceOutcome.REACHABLE
    assert report.counts["total"] == 40
    assert report.counts["active"] + report.counts["down"] == 40
    labels = [link.label for link in report.links]
    assert labels == sorted(labels)


def test_name_filter_matches_either_endpoint(tmp_path: Path) -> None:
    audit = AuditTrail(tmp_path / "audit.jsonl")
    unfiltered = uisp_links(
        _controller(), audit=audit, controller="c", timeout=5.0
    )
    some_name = next(
        end.name
        for link in unfiltered.links
        for end in (link.end_a, link.end_b)
        if end.name
    )
    report = uisp_links(
        _controller(), audit=audit, controller="c", name_filter=some_name, timeout=5.0
    )
    assert report.name_filter == some_name
    assert report.links, "filter must match the link via either endpoint"
    assert all(
        (link.end_a.name and some_name.lower() in link.end_a.name.lower())
        or (link.end_b.name and some_name.lower() in link.end_b.name.lower())
        for link in report.links
    )


def test_ignore_names_excludes_matching_link(tmp_path: Path) -> None:
    """feature 035: ignore_names drops any link whose either endpoint or label matches."""
    audit = AuditTrail(tmp_path / "audit.jsonl")
    unfiltered = uisp_links(_controller(), audit=audit, controller="c", timeout=5.0)
    total = unfiltered.counts["total"]
    victim = next(link.end_a.name for link in unfiltered.links if link.end_a.name)

    report = uisp_links(
        _controller(), audit=audit, controller="c", ignore_names=[victim], timeout=5.0
    )
    assert report.counts["total"] < total, "the matching link must be excluded"
    needle = victim.lower()
    assert all(
        needle not in (link.end_a.name or "").lower()
        and needle not in (link.end_b.name or "").lower()
        and needle not in link.label.lower()
        for link in report.links
    ), "no surviving link may touch the ignored name (either endpoint or label)"


def test_empty_ignore_names_leaves_report_unchanged(tmp_path: Path) -> None:
    audit = AuditTrail(tmp_path / "audit.jsonl")
    baseline = uisp_links(_controller(), audit=audit, controller="c", timeout=5.0)
    with_empty = uisp_links(
        _controller(), audit=audit, controller="c", ignore_names=[], timeout=5.0
    )
    assert [link.label for link in with_empty.links] == [link.label for link in baseline.links]


def test_exclude_wins_over_include_on_overlap(tmp_path: Path) -> None:
    """FR-009: include then exclude; a name matching both is excluded."""
    audit = AuditTrail(tmp_path / "audit.jsonl")
    unfiltered = uisp_links(_controller(), audit=audit, controller="c", timeout=5.0)
    victim = next(link.end_a.name for link in unfiltered.links if link.end_a.name)

    report = uisp_links(
        _controller(),
        audit=audit,
        controller="c",
        name_filter=victim,  # include only links touching victim …
        ignore_names=[victim],  # … then exclude the same → nothing survives
        timeout=5.0,
    )
    assert report.links == []


def test_controller_failure_is_total_with_classified_outcome(tmp_path: Path) -> None:
    audit = AuditTrail(tmp_path / "audit.jsonl")
    report = uisp_links(
        _controller(_failing_transport), audit=audit, controller="c", timeout=5.0
    )
    assert report.outcome is SourceOutcome.AUTH_FAILED
    assert report.links == []
    assert report.detail and "401" in report.detail


def test_exactly_one_audit_record_with_reason(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    audit = AuditTrail(audit_path)
    uisp_links(
        _controller(),
        audit=audit,
        controller="https://uisp.example.com",
        name_filter="chestnut",
        timeout=5.0,
        reason="how's the chestnut backhaul?",
    )
    records = _audit_lines(audit_path)
    assert len(records) == 1
    record = records[0]
    assert record["action"] == "uisp_links"
    assert record["target"] == "uisp-controller:https://uisp.example.com"
    assert record["reason"] == "how's the chestnut backhaul?"
    assert record["params"]["filtered"] is True
    assert "UISP-TOKEN" not in json.dumps(record)  # the token never reaches the audit trail


def test_failure_also_writes_one_audit_record(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    uisp_links(
        _controller(_failing_transport), audit=AuditTrail(audit_path), controller="c", timeout=5.0
    )
    records = _audit_lines(audit_path)
    assert len(records) == 1
    assert records[0]["action"] == "uisp_links"
    assert records[0]["params"]["matched"] == 0

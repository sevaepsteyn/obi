"""Display-timezone normalization of watch findings (feature 069).

DETECTED (``created_at``) is stored UTC while syslog-derived window/evidence times are already
tz-aware local; the render seam converts every human-facing timestamp to one display zone so they
line up on screen. These tests pin the conversion, its idempotence on already-local fields, and the
default (``display_tz=None`` ⇒ unchanged).
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from obi.core.watch import Evidence, WatchFinding, finding_to_dict, render_markdown

LA = ZoneInfo("America/Los_Angeles")

# 12:04:01 PDT is the same instant as 19:04:01 UTC (summer ⇒ UTC-7) — the real-world skew that made
# a DETECTED look ~7h in the future next to an 11:32 local WINDOW.
DETECTED_UTC = datetime(2026, 7, 13, 19, 4, 1, tzinfo=UTC)
WINDOW_LOCAL = datetime(2026, 7, 13, 11, 32, 57, tzinfo=LA)


def _finding() -> WatchFinding:
    return WatchFinding(
        id="f-1",
        created_at=DETECTED_UTC,
        device="chestnut-st-housing-505-05",
        window_start=WINDOW_LOCAL,
        window_end=WINDOW_LOCAL,
        severity="warning",
        confidence="high",
        summary="outage cleared",
        description="d",
        evidence=[Evidence(kind="uisp", text="outage cleared", at=DETECTED_UTC)],
        source="llm",
    )


def test_finding_to_dict_converts_detected_to_display_tz() -> None:
    payload = finding_to_dict(_finding(), display_tz=LA)

    # UTC 19:04:01 renders as the same instant in local time: 12:04:01-07:00.
    assert payload["created_at"] == "2026-07-13T12:04:01-07:00"
    assert payload["evidence"][0]["at"] == "2026-07-13T12:04:01-07:00"


def test_finding_to_dict_is_idempotent_on_already_local_window() -> None:
    payload = finding_to_dict(_finding(), display_tz=LA)

    # The window is already tz-aware local — normalizing to the same zone must not move it.
    assert payload["window_start"] == "2026-07-13T11:32:57-07:00"
    assert payload["window_end"] == "2026-07-13T11:32:57-07:00"


def test_detected_reads_after_window_in_local_time() -> None:
    payload = finding_to_dict(_finding(), display_tz=LA)

    # The whole point: once both are in one zone, DETECTED (12:04) is later than WINDOW (11:32),
    # never hours ahead / "in the future".
    assert payload["created_at"] > payload["window_end"]
    assert payload["created_at"][:13] == "2026-07-13T12"  # local hour, not UTC 19


def test_default_display_tz_is_unchanged_behavior() -> None:
    payload = finding_to_dict(_finding())  # no display_tz ⇒ stored representation

    assert payload["created_at"] == "2026-07-13T19:04:01+00:00"  # UTC, exactly as before
    assert payload["window_start"] == "2026-07-13T11:32:57-07:00"


def test_none_evidence_at_stays_none() -> None:
    finding = _finding()
    finding.evidence.append(Evidence(kind="log", text="no timestamp", at=None))

    payload = finding_to_dict(finding, display_tz=LA)

    assert payload["evidence"][1]["at"] is None


def test_render_markdown_shows_local_detected_and_window() -> None:
    body = render_markdown(_finding(), display_tz=LA)

    assert "**Detected:** 2026-07-13T12:04:01-07:00" in body
    assert "**Window:** 2026-07-13T11:32:57-07:00 → 2026-07-13T11:32:57-07:00" in body
    assert "19:04:01" not in body  # the UTC wall-clock must not leak through


def test_render_markdown_default_keeps_utc_detected() -> None:
    body = render_markdown(_finding())  # no display_tz

    assert "**Detected:** 2026-07-13T19:04:01+00:00" in body

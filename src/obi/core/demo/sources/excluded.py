"""Declared exclusions (feature 134, T048, FR-009/FR-013): what never enters a dataset, and why.

Each entry names state paths (globs allowed) that no source reads into the output. The manifest
lists every one — ``excluded`` when the live host has it, ``absent`` when it does not — so the
dataset documents its own gaps. Names and reasons are generic on purpose: the manifest is public.
"""

from __future__ import annotations

from pathlib import Path

from obi.core.demo.sources import ABSENT, EXCLUDED, SourceReport

EXCLUSIONS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("gate-traffic", ("traffic.db",), "event-only traffic feed"),
    ("radio", ("radio.db", "radio"), "conversational transcripts and audio (FR-013)"),
    ("event-places", ("playa",), "event-only camp and art listings"),
    ("city-grid", ("ar/brc.json", "ar/brc-2026"), "event-only street grid"),
    ("paging", ("paging",), "people and phone numbers"),
    ("watch-reports", ("watch",), "model-written reports (deferred to the demo-content feature)"),
    ("lighting", ("wled_snapshots.json",), "decorative lighting snapshots"),
    ("syslog-mirror", ("logs",), "raw syslog mirror (logs.db carries the events)"),
    ("archive", ("archive",), "host archive"),
    ("service", ("service",), "live host daemon heartbeats"),
    ("schedules", ("crontab.*",), "live host schedules"),
    ("daemon-logs", ("*.log",), "live host daemon logs"),
    ("tls", ("ar/ar-server.key", "ar/ar-server.crt"), "private key material"),
    (
        "terrain-cache",
        ("ar/terrain-tiles",),
        "tile cache; terrain is regenerated at the destination",
    ),
    ("mesh-daemon-state", ("meshtastic/ratelimit.json", "meshtastic/daemon.*"), "live host state"),
)


def declare(state_dir: Path) -> list[SourceReport]:
    reports = []
    for name, patterns, reason in EXCLUSIONS:
        present = any(any(state_dir.glob(pattern)) for pattern in patterns)
        reports.append(SourceReport(name, EXCLUDED if present else ABSENT, reason))
    return reports


def excluded_paths() -> tuple[str, ...]:
    """Every declared pattern (the no-excluded-path-in-output test checks these)."""

    return tuple(p for _name, patterns, _reason in EXCLUSIONS for p in patterns)

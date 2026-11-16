"""The gate's per-text and per-file API reused by the 137 identity guard (T004)."""

from __future__ import annotations

import json
from pathlib import Path

from obi.core.demo.gate import FATAL, LeakGate

from .test_gate import make_harvest

WORDS = frozenset({"orchard", "the", "gateway"})


def test_check_text_is_public_and_returns_findings() -> None:
    gate = LeakGate(make_harvest(), dictionary=WORDS)
    findings = gate.check_text("ping northfield-gw now", "f.py", "line 3")
    assert [(f.severity, f.category, f.file, f.locator) for f in findings] == [
        (FATAL, "place", "f.py", "line 3")
    ]
    assert gate.check_text("all quiet", "f.py", "line 1") == []


def test_check_text_identifier_context_flags_dictionary_place() -> None:
    gate = LeakGate(make_harvest(), dictionary=WORDS)
    assert gate.check_text("orchard", "f", "l", identifier=True)[0].severity == FATAL
    assert all(f.severity != FATAL for f in gate.check_text("the orchard", "f", "l"))


def test_scan_file_json_as_text_gives_line_locators(tmp_path: Path) -> None:
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps({"a": 1, "b": {"host": "northfield-gw"}}, indent=1))
    gate = LeakGate(make_harvest(), dictionary=WORDS)
    as_text = gate.scan_file(path, "fixture.json", json_as_text=True)
    assert as_text and all(f.locator.startswith("line ") for f in as_text)
    as_json = gate.scan_file(path, "fixture.json")
    assert as_json and all(f.locator.startswith("$") for f in as_json)


def test_scan_file_matches_scan_dispatch(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("quillon was here\n")
    gate = LeakGate(make_harvest(), dictionary=WORDS)
    whole = gate.scan(tmp_path).findings
    single = gate.scan_file(tmp_path / "notes.txt", "notes.txt")
    assert [(f.category, f.locator) for f in whole] == [(f.category, f.locator) for f in single]

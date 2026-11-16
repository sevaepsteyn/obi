"""Contract (feature 088 follow-up): RADIO board columns + the kit mechanisms they ride.

Source-marker assertions (the house style for widget contracts) in a file of their own —
deliberately NOT in test_web_workspace.py, which another workstream owns at the moment.
"""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "src" / "obi" / "web" / "static"
KIT = (STATIC / "kit.js").read_text(encoding="utf-8")
BOARDS = (STATIC / "boards.js").read_text(encoding="utf-8")
RADIO = (STATIC / "widgets" / "radio.js").read_text(encoding="utf-8")
THEME = (STATIC / "theme.css").read_text(encoding="utf-8")


def test_kit_color_receives_the_row() -> None:
    # 088: confidence coloring needs a sibling field — the row rides along as a second
    # argument; single-argument color fns ignore it (backwards compatible).
    assert re.search(r"column\.color\(value,\s*row\)", KIT)


def test_boards_exports_a_sub_minute_duration_formatter() -> None:
    assert re.search(r"export function formatDuration\(ms\)", BOARDS)
    # Sub-ten-second durations keep a decimal — radio transmissions are this short.
    assert "toFixed(1)" in BOARDS


def test_radio_board_has_the_duration_column() -> None:
    assert "formatDuration" in RADIO
    assert re.search(r"key:\s*'dur'", RADIO)
    assert re.search(r"sort:\s*'durMs'", RADIO)


def test_radio_transcript_gets_the_overflow_tooltip() -> None:
    # The sanctioned idiom: title names the column's OWN key — the native tooltip carries
    # the full text of an ellipsis-truncated cell.
    assert re.search(r"key:\s*'transcript',[^}]*title:\s*'transcript'", RADIO)


def test_radio_who_column_carries_attribution() -> None:
    assert re.search(r"title:\s*'whoTitle'", RADIO)
    assert "speakerConf" in RADIO
    assert "radio-who-low" in RADIO
    assert re.search(r"entry\.speaker \|\| entry\.caller", RADIO)


def test_theme_class_is_scoped_and_token_only() -> None:
    match = re.search(r"\.widget-radio \.radio-who-low \{([^}]*)\}", THEME)
    assert match, "the low-confidence class must exist, scoped under .widget-radio"
    body = match.group(1)
    assert "var(--" in body and "#" not in body  # tokens only, no hardcoded colors

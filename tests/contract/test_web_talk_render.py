"""Contract: talk.js renderMarkdown escapes then renders obi's markdown to safe HTML (feature 082).

renderMarkdown is DOM-free (a pure string transform), so it runs in plain node — the
``talk_control.mjs`` idiom. The web CHAT window renders obi answers with it (US2); this asserts the
escape-first safety guarantee and the constructs obi actually emits.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from obi.web.server import STATIC_DIR

HARNESS = Path(__file__).parent.parent / "web_harness" / "talk_render.mjs"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _run() -> dict:
    proc = subprocess.run(
        ["node", str(HARNESS), str(STATIC_DIR / "widgets" / "talk.js")],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_bold_renders_to_strong() -> None:
    out = _run()["bold"]
    assert "<strong>tamarack-gw</strong>" in out and "<strong>7 weeks</strong>" in out
    assert "**" not in out


def test_html_is_escaped_before_formatting() -> None:
    out = _run()["escape"]
    assert "<img" not in out  # the tag is neutralized…
    assert "&lt;img" in out  # …shown as escaped text
    assert "&amp;" in out and "&quot;" in out and "&#39;" in out


def test_unordered_and_ordered_lists_render() -> None:
    result = _run()
    assert "<ul>" in result["list"] and result["list"].count("<li>") == 2
    assert "<ol>" in result["ordered"] and result["ordered"].count("<li>") == 2


def test_only_http_links_become_anchors() -> None:
    out = _run()["link"]
    assert '<a href="https://ex.io"' in out and ">docs</a>" in out
    assert 'href="javascript:' not in out  # a non-http link is never made clickable


def test_code_span_is_not_emphasis_processed() -> None:
    out = _run()["code"]
    assert "<code>a **b** c</code>" in out  # bold markers inside code are left literal


def test_plain_text_survives_including_bare_numbers() -> None:
    # Guards the sentinel: text like "3 of 3" must not be eaten by the code/link restore step.
    assert "all links are up 3 of 3" in _run()["plain"]


# --- 089: renderReport (contracts/turn-report.md) ---------------------------------------------


def test_report_summary_names_tools_tokens_and_cost() -> None:
    out = _run()["reportTools"]
    assert out.startswith('<details class="talk-report">')
    assert "routeros_query ×2, ping" in out
    assert "12.4k in / 310 out" in out  # 812 + 11 620 cache reads, humanized
    assert "~$0.02" in out


def test_report_result_text_is_escaped_never_markup() -> None:
    out = _run()["reportTools"]
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "&quot;quoted&quot;" in out


def test_report_marks_errors_and_truncation() -> None:
    out = _run()["reportTools"]
    assert "2. routeros_query — error" in out
    assert 'class="talk-report-call error"' in out
    assert "truncated for display" in out and "audit trail" in out


def test_report_without_tools_is_a_plain_line() -> None:
    out = _run()["reportNoTools"]
    assert "<details" not in out  # FR-004: no expandable detail offered
    assert "12.4k in / 310 out" in out
    assert "🔧" not in out


def test_report_unknown_cost_is_explicit() -> None:
    out = _run()["reportUnknownCost"]
    assert "cost unknown" in out
    assert "$" not in out  # never a wrong number


def test_absent_report_renders_nothing() -> None:
    assert _run()["reportAbsent"] == ""

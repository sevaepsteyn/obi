"""Dictionary-word places in prose: capitalised or listed means a NAME (2026-11-17 incident).

The first published demo leaked real site names that are ordinary English words — a site list
inside an audit payload, access-point names in log messages — because prose only rewrote
DISTINCTIVE places and the gate only warned. Both now treat a capitalised dictionary place, or
two or more places in one comma list, as a name; lower-case English ("the office") stays.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from obi.core.demo import gate as gate_mod
from obi.core.demo import rewrite as rewrite_mod
from obi.core.demo.gate import FATAL, LeakGate
from obi.core.demo.harvest import Harvest
from obi.core.demo.mapping import PseudonymMap
from obi.core.demo.relocate import Relocator
from obi.core.demo.rewrite import Rewriter
from obi.core.demo.timeshift import TimeShift

WORDS = frozenset({"orchard", "lantern", "willow", "town"})


def _harvest() -> Harvest:
    h = Harvest()
    for name in ("orchard-gw", "Lantern", "Willow Hill", "Town", "northfield-gw"):
        h.add_name(name, WORDS)
    return h


def _rewriter(h: Harvest) -> Rewriter:
    m = PseudonymMap(b"\x07" * 32)
    m.forbid(h.forbidden_tokens())
    tz = ZoneInfo("America/Los_Angeles")
    start = datetime(2025, 6, 1, tzinfo=UTC).astimezone(tz)
    shift = TimeShift.from_window(start, date(2025, 6, 2), tz)
    return Rewriter(m, h, shift, Relocator((45.0, -100.0), (37.0, -118.0), 0.0), WORDS)


def _gate_checks(h: Harvest, text: str) -> set[str]:
    findings = LeakGate(h, dictionary=WORDS).check_text(text, "f", "l")
    return {f.check for f in findings if f.severity == FATAL}


def test_capitalised_dictionary_place_is_rewritten() -> None:
    h = _harvest()
    out = _rewriter(h).text("TP: Lantern 11 stopped mesh to parent Orchard.")
    assert "Lantern" not in out and "Orchard" not in out
    assert "capitalised" in _gate_checks(h, "TP: Lantern 11 stopped mesh")
    assert not _gate_checks(h, out)


def test_lower_case_english_is_left_alone() -> None:
    h = _harvest()
    text = "walk to the orchard under the willow"
    assert _rewriter(h).text(text) == text
    assert not _gate_checks(h, text)


def test_comma_list_of_places_is_rewritten() -> None:
    h = _harvest()
    text = "the hubs (lantern, orchard, willow and noc) are up"
    out = _rewriter(h).text(text)
    for word in ("lantern", "orchard", "willow"):
        assert word not in out
    assert "listed" in _gate_checks(h, text)
    assert not _gate_checks(h, out)


def test_list_with_one_place_stays_english() -> None:
    h = _harvest()
    text = "bring rope, lantern, and water"
    assert _rewriter(h).text(text) == text


def test_event_wording_is_never_a_name() -> None:
    h = _harvest()
    h.add_event_term("center camp")
    out = _rewriter(h).text("Center Camp is closed. Town: the AP is down.")
    assert out.startswith("Town center is closed.")  # ours, though "town" is a real place word
    assert "Town:" not in out  # the real one is a name
    assert not _gate_checks(h, out)


def test_gate_keeps_its_own_copy_of_event_wording() -> None:
    assert sorted(gate_mod.EQUIVALENT_PHRASES) == sorted(rewrite_mod.EQUIVALENT_PHRASES)


def test_name_field_inside_a_text_value_is_an_identifier() -> None:
    # A tool result replayed in a chat recording: the JSON is a string, its name fields still
    # name sites.
    h = _harvest()
    text = '{"temperature_celsius": 41.0, "location": "lantern", "rf": {}}'
    out = _rewriter(h).text(text)
    assert '"location": "lantern"' not in out and '"temperature_celsius": 41.0' in out
    assert "identifier" in _gate_checks(h, text)
    assert not _gate_checks(h, out)

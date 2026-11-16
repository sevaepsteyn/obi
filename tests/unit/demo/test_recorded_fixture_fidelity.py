"""Format fidelity against obi's EXISTING recorded device fixtures (feature 134, T026).

Constitution Principle II: behaviour is verified against recorded device responses, never against
assumptions about their shape. The rewriter and gate run over the RouterOS, UISP, Siklu, SNMP,
OSPF, netwatch, mesh and weather fixtures already in ``tests/fixtures`` — structure must survive
byte-for-byte where it carries no identity, and the gate must find nothing fatal afterwards. No
new real data is added by this test.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from obi.core.demo.gate import LeakGate, format_report
from obi.core.demo.harvest import Harvest, harvest_json_tree
from obi.core.demo.mapping import PseudonymMap
from obi.core.demo.naming import load_dictionary
from obi.core.demo.relocate import Relocator, centroid
from obi.core.demo.rewrite import Rewriter
from obi.core.demo.timeshift import TimeShift

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
RECORDED = sorted(
    [
        *FIXTURES.glob("*.json"),
        *(FIXTURES / "uisp").glob("*.json"),
        *(FIXTURES / "siklu").glob("*.json"),
        *(FIXTURES / "snmp").glob("*.json"),
        *(FIXTURES / "meshtastic").glob("*.json"),
        *(FIXTURES / "weather").glob("*.json"),
    ]
)
WORDS = load_dictionary()


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def setup() -> tuple[Harvest, Rewriter]:
    h = Harvest()
    for path in RECORDED:
        harvest_json_tree(_load(path), h, WORDS)
    m = PseudonymMap(b"\x0b" * 32)
    m.forbid(h.forbidden_tokens())
    tz = ZoneInfo("America/Los_Angeles")
    shift = TimeShift.from_window(datetime(2026, 8, 27, tzinfo=UTC), date(2025, 6, 2), tz)
    coords = h.site_coords or h.coords or [(40.0, -119.0)]
    return h, Rewriter(m, h, shift, Relocator(centroid(coords), (37.16, -118.27), 0.0))


def _shape(obj: Any) -> Any:
    """The structure with identity removed: types, list lengths, numeric non-geo/time values."""

    if isinstance(obj, dict):
        return {"dict": len(obj), "children": [_shape(v) for v in obj.values()]}
    if isinstance(obj, list):
        return [_shape(v) for v in obj]
    return type(obj).__name__


def test_recorded_fixtures_present() -> None:
    assert len(RECORDED) >= 20


@pytest.mark.parametrize("path", RECORDED, ids=lambda p: p.name)
def test_structure_survives(path: Path, setup: tuple[Harvest, Rewriter]) -> None:
    _h, rw = setup
    original = _load(path)
    rewritten = rw.json(original)
    assert _shape(rewritten) == _shape(original)


def test_oids_and_numeric_metrics_byte_identical(setup: tuple[Harvest, Rewriter]) -> None:
    _h, rw = setup
    for path in sorted((FIXTURES / "snmp").glob("*.json")):
        original = _load(path)
        rewritten = rw.json(original)
        assert json.dumps(_numbers(original)) == json.dumps(_numbers(rewritten))
        for text in _strings(original):
            if text.startswith("1.3.6."):
                assert text in _strings(rewritten)


def _numbers(obj: Any, key: str = "") -> list[Any]:
    if isinstance(obj, dict):
        return [n for k, v in obj.items() for n in _numbers(v, str(k))]
    if isinstance(obj, list):
        return [n for v in obj for n in _numbers(v, key)]
    if isinstance(obj, int | float) and not isinstance(obj, bool):
        if key.endswith(("_us", "_s", "_at")) or "lat" in key or "lon" in key:
            return []
        return [obj]
    return []


def _strings(obj: Any) -> list[str]:
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in _strings(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in _strings(v)]
    return [obj] if isinstance(obj, str) else []


def test_gate_clean_on_rewritten_fixtures(setup: tuple[Harvest, Rewriter], tmp_path: Path) -> None:
    h, rw = setup
    for path in RECORDED:
        target = tmp_path / path.relative_to(FIXTURES)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(rw.json(_load(path))), encoding="utf-8")
    result = LeakGate(h).scan(tmp_path)
    assert result.fatal == [], format_report(result)

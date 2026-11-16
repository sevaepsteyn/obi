"""Curated fictional callsigns (feature 137, research D6).

Radio and mesh name resolution is fuzzy (``difflib`` ratios against fixed thresholds), so a
generated label like ``user-3`` would destroy what the replay tests exercise. Callsign fakes are
hand-picked and stored in the private mapping's ``callsign`` class. :func:`check` proves a curated
set behaves like the real one: every pair of names, and every name against the station's own
name variants, lands on the SAME side of each threshold the code uses.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher

from obi.radio.trigger import _AMBIGUITY_MARGIN, _MATCH_THRESHOLD, _squeeze

#: The mesh suggestion cutoff (``difflib.get_close_matches(..., cutoff=0.5)`` in core/mesh.py).
SUGGESTION_CUTOFF = 0.5
_THRESHOLDS = (SUGGESTION_CUTOFF, _MATCH_THRESHOLD)


def ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, _squeeze(a), _squeeze(b)).ratio()


def _band(value: float) -> int:
    """Which side of each threshold: 0 below 0.5, 1 in [0.5, 0.75), 2 at/above 0.75."""

    return sum(value >= t for t in _THRESHOLDS)


@dataclass(frozen=True)
class Violation:
    rule: str
    left: int  # opaque indexes into the curated list — never the names themselves
    right: int


def check(pairs: list[tuple[str, str]], station_variants: Iterable[str] = ()) -> list[Violation]:
    """``pairs`` = [(real, fake), …]. Empty list ⇔ the fakes preserve every decision band."""

    out: list[Violation] = []
    variants = list(station_variants)
    for i, (real_i, fake_i) in enumerate(pairs):
        if _squeeze(fake_i) == _squeeze(real_i):
            out.append(Violation("fake equals real", i, i))
        for v, variant in enumerate(variants):
            if _band(ratio(real_i, variant)) != _band(ratio(fake_i, variant)):
                out.append(Violation("station-name band changed", i, -1 - v))
        for j in range(i + 1, len(pairs)):
            real_j, fake_j = pairs[j]
            r, f = ratio(real_i, real_j), ratio(fake_i, fake_j)
            if _band(r) != _band(f):
                out.append(Violation("pair band changed", i, j))
            elif (abs(r - _MATCH_THRESHOLD) < _AMBIGUITY_MARGIN) != (
                abs(f - _MATCH_THRESHOLD) < _AMBIGUITY_MARGIN
            ):
                out.append(Violation("ambiguity margin changed", i, j))
    return out

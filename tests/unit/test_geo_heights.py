"""Unit tests: mixed-datum height resolution (feature 022, T007 — research §3).

Every case below is a shape observed LIVE on the controller (see the probe values recorded in
specs/022-ar-links/research.md §3): plausible-AMSL altitudes, small AGL values, zero/absent.
"""

from __future__ import annotations

import pytest

from obi.core.geo import resolve_height
from obi.core.models import HeightProvenance

PLAYA = 1190.66  # the playa's ground elevation (EPQS-verified)
BASSWOOD = 1370.0

TOLERANCE = 150.0
MAST = 10.0


def _resolve(raw: float | None, ground: float) -> tuple[float, HeightProvenance]:
    return resolve_height(
        raw, ground, amsl_tolerance=TOLERANCE, default_mast_height=MAST
    )


@pytest.mark.parametrize(
    ("raw", "ground", "expected_height", "expected_provenance"),
    [
        # Rule 1 — altitude at-or-above ground within tolerance ⇒ already AMSL (live:
        # 1197.8 / 1217.7 on the ~1190 m playa). Exactly-at-ground is the boundary and passes.
        (1197.8, PLAYA, 1197.8, HeightProvenance.AMSL),
        (1217.7, PLAYA, 1217.7, HeightProvenance.AMSL),
        (BASSWOOD, BASSWOOD, BASSWOOD, HeightProvenance.AMSL),
        # Rule 2 — small altitude ⇒ height above ground (live: 10 and 1).
        (10.0, PLAYA, PLAYA + 10.0, HeightProvenance.AGL),
        (1.0, BASSWOOD, BASSWOOD + 1.0, HeightProvenance.AGL),
        # Rule 3 (083) — plausibly-AMSL but BELOW ground ⇒ junk reading, ground + default
        # mast, provenance ground_floored. Live 2026-08-19: basswood-ptp-tamarack-60 reported
        # 1355.8 on a 1362.7 DEM ridge → both tamarack↔basswood hops false-flagged near-mast
        # Fresnel "interference at meter 0". The June-observed 1367.9-on-~1370 reading (022)
        # was the same class, merely small enough to slip the old symmetric band.
        (1355.8, 1362.7, 1362.7 + MAST, HeightProvenance.GROUND_FLOORED),
        (1367.9, BASSWOOD, BASSWOOD + MAST, HeightProvenance.GROUND_FLOORED),
        # Rule 4 — zero / absent / nonsense ⇒ ground + default mast (live: 0).
        (0.0, PLAYA, PLAYA + MAST, HeightProvenance.DEFAULT_MAST),
        (None, PLAYA, PLAYA + MAST, HeightProvenance.DEFAULT_MAST),
        (-5.0, PLAYA, PLAYA + MAST, HeightProvenance.DEFAULT_MAST),
        (25000.0, PLAYA, PLAYA + MAST, HeightProvenance.DEFAULT_MAST),
    ],
)
def test_height_rule_matrix(
    raw: float | None,
    ground: float,
    expected_height: float,
    expected_provenance: HeightProvenance,
) -> None:
    height, provenance = _resolve(raw, ground)
    assert height == pytest.approx(expected_height)
    assert provenance is expected_provenance


def test_below_ground_but_agl_plausible_reads_as_agl() -> None:
    """A below-ground reading that also fits rule 2 is a height above ground, not junk (083)."""

    height, provenance = _resolve(45.0, 50.0)
    assert height == pytest.approx(95.0)
    assert provenance is HeightProvenance.AGL


def test_rule_order_amsl_wins_near_low_ground() -> None:
    """A small altitude near equally-small ground hits rule 1 (AMSL) before rule 2 (AGL)."""

    height, provenance = _resolve(60.0, 50.0)
    assert height == pytest.approx(60.0)
    assert provenance is HeightProvenance.AMSL


def test_amsl_tolerance_boundaries() -> None:
    # exactly at the tolerance boundary → still AMSL (<=)
    height, provenance = _resolve(PLAYA + TOLERANCE, PLAYA)
    assert provenance is HeightProvenance.AMSL
    assert height == pytest.approx(PLAYA + TOLERANCE)
    # just past it, and too large for AGL → default mast
    height, provenance = _resolve(PLAYA + TOLERANCE + 0.1, PLAYA)
    assert provenance is HeightProvenance.DEFAULT_MAST
    # AGL boundary: exactly `tolerance` above zero is still AGL when far from ground
    height, provenance = _resolve(TOLERANCE, PLAYA)
    assert provenance is HeightProvenance.AGL
    assert height == pytest.approx(PLAYA + TOLERANCE)

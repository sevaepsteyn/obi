"""The survey origin and plan year the city-plan test fixtures were cut around (feature 137).

The origin used to be a source constant (``core/brc.py``); it is operator config (``[brc]``)
now, so the tests carry their own. INTERIM: these are still the real plan's values, because the
fixture GeoJSON is still the real geometry — the 137 fixture pass (T060) shifts the fixtures and
this origin together by the private fixture offset.
"""

ORIGIN = (40.783242, -114.815579)
YEAR = 2026
SPIKE_LAT, SPIKE_LON = ORIGIN

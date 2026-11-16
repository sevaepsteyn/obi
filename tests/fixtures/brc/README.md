# BRC city-grid fixtures (feature 098)

Hand-trimmed GeoJSON in the published dataset's SHAPE (FeatureCollection per file, the
2026/GeoJSON file names, [lon, lat] coordinates around the 2026 golden spike
40.783242, -114.815579) — **not** the published data itself: the innovate-GIS-data
license points to Burning Man's ToS with no redistribution grant, so the real files stay
out of the repo (research D1) and live only in the operator's state-dir cache.

Deliberate contents:

- `trash_fence.geojson` — one Polygon (closed pentagon ring, 6 coordinate pairs).
- `street_outlines.geojson` — one open LineString, one closed Polygon block ring, one
  MultiLineString (two parts; covers the Multi* branch), and one CORRUPT LineString whose
  second vertex (-100, 45) lies ~1,600 km from the spike — the 15 km sanity rule must drop
  that whole polyline and count it.
- `plazas.geojson` — one Polygon plus one feature with `"geometry": null` (skipped, not fatal).

(`gate_road.geojson` existed through field round 1; the operator dropped the gate-road
layer in field round 2, 2026-08-25, and the Multi* coverage moved into the street file.)

Hand-computed projection anchor (independent of `geo.enu_offset`, for
`test_projection_matches_hand_computed_values`): the street file's open LineString STARTS
at spike + (0.002° lat, 0° lon):

```
north = radians(0.002) · R = 3.4906585e-5 · 6371008.8 = 222.39 m
east  = 0 (same longitude)
```

(±0.02 m from rounding; tests use ±0.3 m.)

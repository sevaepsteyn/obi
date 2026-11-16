"""Pure geometry core for the AR link view (feature 022) — selection, heights, ENU, health.

Every numeric decision the AR viewer displays is made HERE, in tested core code, and shipped
precomputed in the export; the viewer performs zero geodesy (Principle III, research §5). No
I/O in this module: the controller snapshot comes from ``drivers/uisp.py`` and ground
elevations from ``core/elevation.py``, both injected by the engine.

The projection is a flat-earth local tangent plane (equirectangular): at the ≤20 km scale of
this network the error versus a full geodesic is centimeters-to-a-few-meters — far inside the
~2° yaw budget of SC-003 — and the math stays hand-checkable. Cross-checked in tests against
the controller's own per-link ``devicesDistance``.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from typing import TypeGuard

from obi.core.models import (
    ArEndpoint,
    ArExcluded,
    ArGeometry,
    ArLink,
    ArSite,
    ArSiteAnchor,
    ArTowerPlacement,
    FrequencyProvenance,
    GeoDeviceRecord,
    GeoLinkRecord,
    GeometrySnapshot,
    GeoSiteRecord,
    GroundProvenance,
    HeightProvenance,
    LinkHealth,
    LinkState,
    LocationProvenance,
    LogicalLink,
    Reachability,
    SikluRadioStatus,
    _utcnow,
)
from obi.core.pairing import counterpart_name

# Mean Earth radius (IUGG), meters — sub-meter accurate for ENU offsets at this scale.
_EARTH_RADIUS_M = 6_371_008.8

# The operator's naming convention marks PtP backhaul radios: <site>-ptp-<peersite>-<band>.
_PTP_MARKER = "-ptp-"

# Trailing name token → human band label (bands observed live: 05 / 11 / 60; 70 = the
# Siklu e-band hop, merged into the export by feature 044).
_BAND_LABELS = {
    "05": "5 GHz",
    "5": "5 GHz",
    "11": "11 GHz",
    "24": "24 GHz",
    "60": "60 GHz",
    "70": "70 GHz",
}

# Band label → nominal channel (MHz) — the tier-3 frequency fallback (025 research §1).
# A few percent of Fresnel radius off the exact channel; disclosed via provenance.
# 70 GHz nominal = mid E-band low half (71–76 GHz); the live radios report their real
# tx channel, so this fallback only covers an unreachable pair.
_BAND_NOMINAL_MHZ = {
    "5 GHz": 5500.0,
    "11 GHz": 11000.0,
    "24 GHz": 24000.0,
    "60 GHz": 60000.0,
    "70 GHz": 73000.0,
}

# Speed of light, m/s — the only physical constant Fresnel geometry needs.
_C_M_PER_S = 299_792_458.0

# overview.status value meaning "the controller currently reaches the device".
_ACTIVE = "active"

# A ground-elevation lookup: (site_name, lat, lon) → (elevation_m_amsl, provenance).
GroundLookup = Callable[[str, float, float], tuple[float, GroundProvenance]]


def enu_offset(
    origin_lat: float, origin_lon: float, lat: float, lon: float
) -> tuple[float, float]:
    """(east, north) meters of ``(lat, lon)`` relative to the origin — flat-earth tangent."""

    east = math.radians(lon - origin_lon) * math.cos(math.radians(origin_lat)) * _EARTH_RADIUS_M
    north = math.radians(lat - origin_lat) * _EARTH_RADIUS_M
    return east, north


def resolve_height(
    raw_altitude: float | None,
    ground_elevation: float,
    *,
    amsl_tolerance: float,
    default_mast_height: float,
) -> tuple[float, HeightProvenance]:
    """Resolve one endpoint's mixed-datum altitude to meters AMSL (research §3, FR-004).

    Ordered rules — each exists because of a live-observed value (1197.8/1217.7 AMSL,
    10/1 AGL, 0 junk, and 083's 1355.8-on-a-1362.7-ridge underground reading):

    1. altitude at-or-above ground within tolerance (``G ≤ altitude ≤ G + tolerance``) →
       already AMSL. An installed radio is never below the ground it stands on, so the
       below-ground half of the old symmetric band no longer qualifies (feature 083).
    2. small positive altitude (``0 < altitude ≤ tolerance``) → height above ground.
    3. plausibly AMSL but BELOW ground (``G − tolerance ≤ altitude < G``) → the reading is
       junk: in the DEM-anchored frame an underground endpoint false-flags near-mast
       Fresnel verdicts (083; observed live 2026-08-19 on both showers↔mountain hops —
       1355.8 reported on a 1362.7 ridge). Resolved as ground + default mast, provenance
       ``GROUND_FLOORED`` — corrected, never silently (FR-006).
    4. anything else (absent, zero, nonsense) → ground + the configured default mast height.
    """

    if raw_altitude is not None:
        if ground_elevation <= raw_altitude <= ground_elevation + amsl_tolerance:
            return float(raw_altitude), HeightProvenance.AMSL
        if 0.0 < raw_altitude <= amsl_tolerance:
            return ground_elevation + float(raw_altitude), HeightProvenance.AGL
        if ground_elevation - amsl_tolerance <= raw_altitude < ground_elevation:
            return ground_elevation + default_mast_height, HeightProvenance.GROUND_FLOORED
    return ground_elevation + default_mast_height, HeightProvenance.DEFAULT_MAST


def is_ptp_name(name: str | None) -> bool:
    """Whether a device name marks a PtP backhaul radio per the naming convention."""

    return bool(name) and _PTP_MARKER in str(name).lower()


def derive_band(name_a: str, name_b: str) -> str:
    """The link's band from the trailing naming-convention token, when both ends agree.

    Disagreement (or an underivable token) yields ``"unknown"`` — it never excludes a link.
    """

    token_a = name_a.rsplit("-", 1)[-1].strip().lower()
    token_b = name_b.rsplit("-", 1)[-1].strip().lower()
    if not token_a or token_a != token_b:
        return "unknown"
    return _BAND_LABELS.get(token_a, token_a)


def resolve_frequency(
    link: GeoLinkRecord, end_a: GeoDeviceRecord, end_b: GeoDeviceRecord
) -> tuple[float, FrequencyProvenance] | None:
    """Resolve one link's operating frequency in MHz, first matching tier wins (FR-001).

    1. The link record's own channel — authoritative: the two ends' cached device values
       can disagree (observed live: 68040 vs 64800 on one disconnected 60 GHz pair).
    2. Either endpoint's device channel; the mean when both report one.
    3. The nominal for the naming-convention band.
    4. ``None`` — no zone, never invented (the export summary names the link, FR-002).

    Zero/negative reported values are junk and treated as absent (like ``resolve_height``).
    """

    if link.frequency_mhz is not None and link.frequency_mhz > 0:
        return float(link.frequency_mhz), FrequencyProvenance.LINK_CHANNEL
    device_channels = [
        float(d.frequency_mhz)
        for d in (end_a, end_b)
        if d.frequency_mhz is not None and d.frequency_mhz > 0
    ]
    if device_channels:
        mean = sum(device_channels) / len(device_channels)
        return mean, FrequencyProvenance.DEVICE_CHANNEL
    nominal = _BAND_NOMINAL_MHZ.get(derive_band(end_a.name, end_b.name))
    if nominal is not None:
        return nominal, FrequencyProvenance.BAND_NOMINAL
    return None


def fresnel_radius_m(length_m: float, frequency_mhz: float) -> float:
    """First Fresnel zone's maximum (mid-span) radius in meters: ``0.5·sqrt(λ·D)``.

    The full clearance ellipsoid is derivable from this one scalar — it is the ellipsoid of
    revolution with foci at the two endpoints and this semi-minor axis (research §2); the
    viewer composes it without doing physics (Principle III).
    """

    wavelength = _C_M_PER_S / (frequency_mhz * 1e6)
    return 0.5 * math.sqrt(wavelength * length_m)


def fresnel_local_radius_m(d1: float, d2: float, length_m: float, frequency_mhz: float) -> float:
    """First Fresnel radius at ``d1`` meters from one end: ``sqrt(λ·d1·d2/D)`` (feature 031).

    Reduces to :func:`fresnel_radius_m` at mid-span and to zero at the endpoints — which is
    why a datum-anchored profile can never create a false endpoint obstruction (research R3).
    """

    if length_m <= 0.0 or d1 <= 0.0 or d2 <= 0.0:
        return 0.0
    wavelength = _C_M_PER_S / (frequency_mhz * 1e6)
    return math.sqrt(wavelength * d1 * d2 / length_m)


def link_health(
    link: GeoLinkRecord,
    end_a: GeoDeviceRecord,
    end_b: GeoDeviceRecord,
    *,
    degraded_link_score: float,
) -> LinkHealth:
    """Map controller state to a :class:`LinkHealth` (research §2).

    ``unknown`` when required status fields are absent; ``down`` when the link or either
    device is not active; ``degraded`` when the WORSE endpoint linkScore (min of the two;
    endpoints without a score don't participate) is under the threshold; else ``healthy``.
    """

    if link.state is None or end_a.status is None or end_b.status is None:
        return LinkHealth.UNKNOWN
    if link.state.strip().lower() != _ACTIVE:
        return LinkHealth.DOWN
    if end_a.status.strip().lower() != _ACTIVE or end_b.status.strip().lower() != _ACTIVE:
        return LinkHealth.DOWN
    scores = [s for s in (end_a.link_score, end_b.link_score) if s is not None]
    if scores and min(scores) < degraded_link_score:
        return LinkHealth.DEGRADED
    return LinkHealth.HEALTHY


# Utilization above this is junk telemetry, not a busy link (044 data-model §Validation):
# the controller reports a 0–1 fraction; small excursions above 1.0 have been observed on
# other UISP gauges, so a modest tolerance band is accepted before discarding.
_UTILIZATION_MAX = 1.5


def link_capacity(
    end_a: GeoDeviceRecord, end_b: GeoDeviceRecord
) -> tuple[int | None, int | None, int | None, int | None]:
    """Per-direction capacity + usage for one link (044 US2, data-model §ArLink).

    Returns ``(capacity_down, usage_down, capacity_up, usage_up)`` in int bps. Per
    direction, end A's current capacity wins, else end B's (PtP pairs mirror each other —
    research R2); usage derives from the SAME end that supplied the capacity
    (``round(utilization × capacity)``) and only when both factors are present and the
    utilization is sane. Nothing is ever invented: absent data stays ``None``.
    """

    def direction(
        capacity_of: Callable[[GeoDeviceRecord], int | None],
        utilization_of: Callable[[GeoDeviceRecord], float | None],
    ) -> tuple[int | None, int | None]:
        for end in (end_a, end_b):
            capacity = capacity_of(end)
            if capacity is not None and capacity > 0:
                utilization = utilization_of(end)
                usage = (
                    round(utilization * capacity)
                    if utilization is not None and 0.0 <= utilization <= _UTILIZATION_MAX
                    else None
                )
                return capacity, usage
        return None, None

    capacity_down, usage_down = direction(
        lambda end: end.capacity_down_bps, lambda end: end.utilization_down
    )
    capacity_up, usage_up = direction(
        lambda end: end.capacity_up_bps, lambda end: end.utilization_up
    )
    return capacity_down, usage_down, capacity_up, usage_up


def _site_display(device: GeoDeviceRecord) -> str:
    """The site half of a link display name — the device's site, else its own name."""

    return device.site or device.name


def build_geometry(
    snapshot: GeometrySnapshot,
    *,
    controller: str,
    ground: GroundLookup,
    amsl_tolerance: float,
    default_mast_height: float,
    degraded_link_score: float,
    location_sanity_m: float = 75.0,
    site_towers: Mapping[str, float] | None = None,
) -> ArGeometry:
    """Assemble the full, self-contained AR geometry from one controller snapshot.

    Selection: wireless data-links whose BOTH endpoint names carry the PtP marker. A candidate
    whose endpoint is unknown, unlocated, or site-less is excluded by name with a reason —
    never silently (FR-009). Ground elevations are looked up once per involved site (the
    lookup itself caches durably); every site in the snapshot is exported so the viewer's
    site picker is complete, and each site's ``enu`` table covers every exported endpoint
    (research §5). An empty result is valid — ``excluded`` explains it.

    Site-anchoring (079): when an endpoint's device pin has strayed farther than
    ``location_sanity_m`` from its SITE pin (a stale copy-at-placement value on a GPS-less
    radio), the endpoint is placed at the site pin instead (``location_provenance = SITE``) and
    the relocation is recorded in ``site_anchored`` — named, never silent. Within the radius (a
    rounded copy is ~≤11 m) the device pin is kept (``DEVICE``). ``location_sanity_m <= 0``
    disables the correction; a missing site pin leaves the device pin in place. Horizontal-only:
    heights, sites, and everything derived from them are untouched.

    Site towers (097): ``site_towers`` maps a site name to a declared tower height (meters
    above site ground, ``[web] site_towers``). Every endpoint of such a site is placed at ONE
    shared point — the exported site record's position, at ``ground + tower height`` — with
    ``tower`` provenance on both axes, recorded in ``tower_placed``. The declaration outranks
    both corrections above: the 079 comparison and the height heuristic are skipped entirely
    for that endpoint (one named correction, not two). A configured name matching no exported
    site is reported in ``tower_unmatched`` — a typo'd or coordinate-less site degrades the
    map, never the export.
    """

    devices = {d.name: d for d in snapshot.devices if d.name}
    # 079: the operator-maintained site pins, straight from /sites (pre-synthesis, so a site
    # absent from /sites has no pin and its endpoints are never corrected — no basis to).
    site_pin = {
        s.name: (s.latitude, s.longitude)
        for s in snapshot.sites
        if s.name and s.latitude is not None and s.longitude is not None
    }
    site_anchored: list[ArSiteAnchor] = []
    towers = dict(site_towers) if site_towers else {}
    tower_placed: list[ArTowerPlacement] = []

    # --- select candidate backhauls and resolve their endpoints -------------------------------
    excluded: list[ArExcluded] = []
    candidates: list[tuple[GeoLinkRecord, GeoDeviceRecord, GeoDeviceRecord]] = []
    for link in snapshot.links:
        if (link.link_type or "").strip().lower() != "wireless":
            continue
        if not (is_ptp_name(link.from_name) and is_ptp_name(link.to_name)):
            continue  # PtMP/station or non-convention link — not a backhaul candidate
        label = f"{link.from_name} <-> {link.to_name}"
        ends: list[GeoDeviceRecord] = []
        reason: str | None = None
        for name in (link.from_name, link.to_name):
            device = devices.get(str(name))
            if device is None:
                reason = f"endpoint {name} not in the controller device list"
                break
            if device.latitude is None or device.longitude is None:
                reason = f"endpoint {name} has no coordinates in UISP"
                break
            if not device.site:
                reason = f"endpoint {name} has no site in UISP"
                break
            ends.append(device)
        if reason is not None:
            excluded.append(ArExcluded(link=label, reason=reason))
            continue
        candidates.append((link, ends[0], ends[1]))

    # --- sites: those that anchor exported geometry + ground elevation -------------------------
    # 057 revises the 022 "every snapshot site" rule: a site with no exported link endpoint
    # draws nothing on any surface but still cost two full terrain grids and dragged the
    # fleet centroid (the controller lists Burning Man's SF offices, 404 km away). Exactly
    # the endpoint-referenced sites ship; an ignored site regains its entry the moment it
    # hosts a real backhaul.
    referenced_sites = {device.site for _link, *ends in candidates for device in ends}
    site_records = {s.name: s for s in snapshot.sites if s.name}
    # A site referenced by an endpoint but absent from /sites still gets an entry, positioned
    # at that endpoint (a site record is the viewer's origin handle, so it must exist).
    for _link, *ends in candidates:
        for device in ends:
            if device.site and device.site not in site_records:
                site_records[device.site] = GeoSiteRecord(
                    name=device.site, latitude=device.latitude, longitude=device.longitude
                )

    sites: list[ArSite] = []
    degraded: list[str] = []
    for record in sorted(site_records.values(), key=lambda s: s.name):
        if record.name not in referenced_sites:
            continue  # hosts no exported endpoint — dead weight for every surface (057)
        if record.latitude is None or record.longitude is None:
            continue  # a site without coordinates cannot be an origin (none live today)
        elevation, provenance = ground(record.name, record.latitude, record.longitude)
        if provenance is GroundProvenance.DEFAULT_GROUND:
            degraded.append(record.name)
        sites.append(
            ArSite(
                name=record.name,
                latitude=record.latitude,
                longitude=record.longitude,
                ground_elevation_m=elevation,
                ground_provenance=provenance,
            )
        )
    site_by_name = {s.name: s for s in sites}
    # 097: a configured tower on a site that is not an export origin (typo, renamed, or
    # coordinate-less) can never apply — reported, never silent (FR-006/FR-008).
    tower_unmatched = sorted(set(towers) - set(site_by_name))

    # --- endpoints: resolved heights with provenance -------------------------------------------
    endpoints: dict[str, ArEndpoint] = {}
    links: list[ArLink] = []
    for link, end_a, end_b in candidates:
        label = f"{link.from_name} <-> {link.to_name}"
        resolved: list[ArEndpoint] = []
        reason = None
        for device in (end_a, end_b):
            assert device.site is not None and device.latitude is not None  # selected above
            assert device.longitude is not None
            site = site_by_name.get(device.site)
            if site is None:
                reason = f"endpoint {device.name}: site {device.site} has no coordinates"
                break
            existing = endpoints.get(device.name)
            if existing is not None:
                resolved.append(existing)
                continue
            # 097: a declared tower places the endpoint at the site record's position at
            # ground + tower height — the operator's declaration outranks the 079 stray-pin
            # comparison AND the height heuristic (one named correction, not two).
            tower = towers.get(device.site)
            if tower is not None:
                east, north = enu_offset(
                    site.latitude, site.longitude, device.latitude, device.longitude
                )
                height = site.ground_elevation_m + tower
                endpoint = ArEndpoint(
                    name=device.name,
                    site=device.site,
                    latitude=site.latitude,
                    longitude=site.longitude,
                    raw_altitude=device.altitude,
                    height_amsl_m=round(height, 2),
                    height_provenance=HeightProvenance.TOWER,
                    status=device.status or "unknown",
                    link_score=device.link_score,
                    maintenance=device.maintenance,
                    location_provenance=LocationProvenance.TOWER,
                )
                tower_placed.append(
                    ArTowerPlacement(
                        device=device.name,
                        site=device.site,
                        tower_height_m=tower,
                        device_latitude=device.latitude,
                        device_longitude=device.longitude,
                        raw_altitude=device.altitude,
                        site_latitude=site.latitude,
                        site_longitude=site.longitude,
                        height_amsl_m=round(height, 2),
                        distance_m=round(math.hypot(east, north), 1),
                    )
                )
                endpoints[device.name] = endpoint
                resolved.append(endpoint)
                continue
            # 079: prefer the site pin when the device pin has strayed. Compare device→site
            # distance (same flat-earth projection as the ENU tables); beyond the sanity radius
            # the device pin is a stale copy → place the endpoint at the site pin and name it.
            latitude, longitude = device.latitude, device.longitude
            location_provenance = LocationProvenance.DEVICE
            pin = site_pin.get(device.site)
            if location_sanity_m > 0 and pin is not None:
                east, north = enu_offset(pin[0], pin[1], device.latitude, device.longitude)
                distance = math.hypot(east, north)
                if distance > location_sanity_m:
                    latitude, longitude = pin
                    location_provenance = LocationProvenance.SITE
                    site_anchored.append(
                        ArSiteAnchor(
                            device=device.name,
                            site=device.site,
                            distance_m=round(distance, 1),
                            device_latitude=device.latitude,
                            device_longitude=device.longitude,
                            site_latitude=pin[0],
                            site_longitude=pin[1],
                        )
                    )
            height, height_provenance = resolve_height(
                device.altitude,
                site.ground_elevation_m,
                amsl_tolerance=amsl_tolerance,
                default_mast_height=default_mast_height,
            )
            endpoint = ArEndpoint(
                name=device.name,
                site=device.site,
                latitude=latitude,
                longitude=longitude,
                raw_altitude=device.altitude,
                height_amsl_m=round(height, 2),
                height_provenance=height_provenance,
                status=device.status or "unknown",
                link_score=device.link_score,
                maintenance=device.maintenance,
                location_provenance=location_provenance,
            )
            endpoints[device.name] = endpoint
            resolved.append(endpoint)
        if reason is not None:
            excluded.append(ArExcluded(link=label, reason=reason))
            continue

        a, b = resolved
        east, north = enu_offset(a.latitude, a.longitude, b.latitude, b.longitude)
        length = math.sqrt(east**2 + north**2 + (b.height_amsl_m - a.height_amsl_m) ** 2)
        band = derive_band(a.name, b.name)
        # Fresnel geometry (025): all-or-none trio — resolved frequency ⇒ zone radius;
        # unresolvable ⇒ all None (the CLI summary names such links, FR-002).
        frequency_ghz: float | None = None
        frequency_provenance: FrequencyProvenance | None = None
        zone_radius: float | None = None
        resolved_frequency = resolve_frequency(link, end_a, end_b)
        if resolved_frequency is not None:
            mhz, frequency_provenance = resolved_frequency
            frequency_ghz = round(mhz / 1000.0, 3)
            zone_radius = round(fresnel_radius_m(length, mhz), 2)
        capacity_down, usage_down, capacity_up, usage_up = link_capacity(end_a, end_b)
        links.append(
            ArLink(
                id=link.id,
                name=f"{_site_display(end_a)} <-> {_site_display(end_b)} ({band})",
                a=a.name,
                b=b.name,
                band=band,
                health=link_health(
                    link, end_a, end_b, degraded_link_score=degraded_link_score
                ),
                signal_dbm=link.signal,
                length_m=round(length, 1),
                frequency_ghz=frequency_ghz,
                frequency_provenance=frequency_provenance,
                fresnel_radius_m=zone_radius,
                capacity_down_bps=capacity_down,
                capacity_up_bps=capacity_up,
                usage_down_bps=usage_down,
                usage_up_bps=usage_up,
                # 044 US3: either flagged end puts the whole link in maintenance
                maintenance=end_a.maintenance or end_b.maintenance,
            )
        )

    # --- per-site ENU tables: every site origin × every exported endpoint (research §5) --------
    endpoint_list = sorted(endpoints.values(), key=lambda e: e.name)
    for site in sites:
        table: dict[str, tuple[float, float, float]] = {}
        for endpoint in endpoint_list:
            east, north = enu_offset(
                site.latitude, site.longitude, endpoint.latitude, endpoint.longitude
            )
            up = endpoint.height_amsl_m - site.ground_elevation_m
            table[endpoint.name] = (round(east, 2), round(north, 2), round(up, 2))
        site.enu = table

    return ArGeometry(
        generated_at=_utcnow(),
        controller=controller,
        sites=sites,
        endpoints=endpoint_list,
        links=sorted(links, key=lambda link: link.name),
        excluded=sorted(excluded, key=lambda e: e.link),
        degraded=sorted(degraded),
        site_anchored=sorted(site_anchored, key=lambda a: a.device),
        tower_placed=sorted(tower_placed, key=lambda p: p.device),
        tower_unmatched=tower_unmatched,
    )


# --- Siklu e-band merge (feature 044 US4, research R6) -------------------------------------------

# LogicalLink state → viewer link health. HALF_KNOWN (one radio unread) now DOES reach this map
# as `unknown` when the link is groundable — its topology is static (both masts known, the dark
# end's name derived from the reachable one), so a single missed SSH poll no longer erases a real
# link; only its health degrades to `unknown` (071). UNKNOWN (neither radio answered) stays
# excluded by spec: an unreachable Siklu fleet shows NO e-band link, not a gray guess
# (044 US4 acceptance scenario 2). The mapping is total.
_SIKLU_HEALTH = {
    LinkState.UP: LinkHealth.HEALTHY,
    LinkState.DEGRADED: LinkHealth.DEGRADED,
    LinkState.DOWN: LinkHealth.DOWN,
    LinkState.HALF_KNOWN: LinkHealth.UNKNOWN,
    LinkState.UNKNOWN: LinkHealth.UNKNOWN,
}

_SIKLU_BAND = "70 GHz"


def _sibling_endpoint(radio_name: str, endpoints: list[ArEndpoint]) -> ArEndpoint | None:
    """The UISP endpoint whose mast the Siklu radio shares (research R6).

    Preference order, both deterministic: (1) the exact naming-convention sibling — same
    ``<site>-ptp-<peer>-`` prefix with any band suffix (the live fleet: the -60 and -70
    radios share towers); (2) any endpoint whose leading ``<site>`` token matches,
    lexicographically first. Case-insensitive; a hostname suffix on the radio name is
    tolerated (``siteb-ptp-sitea-70.net…`` still yields prefix ``siteb-ptp-sitea-``).
    """

    bare = radio_name.split(".")[0].lower()
    if _PTP_MARKER not in bare or "-" not in bare:
        return None
    sibling_prefix = bare.rsplit("-", 1)[0] + "-"
    exact = sorted(
        (e for e in endpoints if e.name.lower().startswith(sibling_prefix)),
        key=lambda e: e.name,
    )
    if exact:
        return exact[0]
    site_token = bare.split("-", 1)[0] + "-"
    fallback = sorted(
        (e for e in endpoints if e.name.lower().startswith(site_token)),
        key=lambda e: e.name,
    )
    return fallback[0] if fallback else None


def _siklu_capacity_bps(
    end_a: SikluRadioStatus | None, end_b: SikluRadioStatus | None
) -> tuple[int | None, int | None]:
    """Per-direction air capacity in bps: down = end A's transmit figure (falling back to
    B's), up = end B's (falling back to A's). Usage is not reported by the radios. A ``None``
    end (071: a half-known link's unread radio) contributes nothing, so a held link mirrors the
    reachable end's figure to both directions."""

    def of(radio: SikluRadioStatus | None) -> int | None:
        mbps = radio.rf.air_capacity_mbps if radio is not None and radio.rf is not None else None
        return mbps * 1_000_000 if mbps is not None and mbps > 0 else None

    a, b = of(end_a), of(end_b)
    return a if a is not None else b, b if b is not None else a


def _siklu_frequency_mhz(
    end_a: SikluRadioStatus | None, end_b: SikluRadioStatus | None
) -> tuple[float, FrequencyProvenance]:
    """End A's tx channel (else B's), else the 70 GHz band nominal — always resolvable,
    since the band is fixed by construction (research R6). A ``None`` end (071) is skipped."""

    for radio in (end_a, end_b):
        mhz = radio.rf.tx_frequency_mhz if radio is not None and radio.rf is not None else None
        if mhz is not None and mhz > 0:
            return float(mhz), FrequencyProvenance.DEVICE_CHANNEL
    return _BAND_NOMINAL_MHZ[_SIKLU_BAND], FrequencyProvenance.BAND_NOMINAL


def _siklu_signal_dbm(
    end_a: SikluRadioStatus | None, end_b: SikluRadioStatus | None
) -> float | None:
    """The link RSSI: end A's, else end B's — but only falling back to B when A was not read
    at all (a half-known link's dark end), so a fully-known link's signal is exactly end A's as
    before (071). Returns ``None`` when the chosen end reports no RSSI."""

    for end in (end_a, end_b):
        if end is None:
            continue
        rssi = end.rf.rssi_dbm if end.rf is not None else None
        return float(rssi) if rssi is not None else None
    return None


def _siklu_endpoint_status(radio: SikluRadioStatus) -> str:
    if radio.reachability is not Reachability.REACHABLE:
        return "unknown"
    if radio.rf is not None and radio.rf.operational is True:
        return "active"
    return "disconnected"


def merge_siklu_links(geometry: ArGeometry, links: list[LogicalLink]) -> dict[str, int]:
    """Merge Siklu logical e-band links into the UISP viewer geometry (044 US4).

    Pure and in-place: each joinable link appends two endpoints (site/coordinates/height
    inherited from the naming-convention sibling mast — the radios carry no GPS), extends
    every site's ENU table, and adds a ``source="siklu"`` link in the 70 GHz band group
    with the same Fresnel-field contract as UISP links.

    A HALF-KNOWN link — one radio read, the other momentarily unreachable over SSH (071) — is
    HELD, not dropped: the dark end's endpoint name is derived from the reachable end via the
    naming convention (:func:`counterpart_name`), both ends are grounded in their sibling masts
    exactly as a fully-known link is, and the link is kept with health ``unknown``. So a single
    missed poll no longer erases a real, static link from the map; only its health degrades. A
    link that STILL cannot be grounded — the reachable name doesn't follow the convention, a
    derived end has no sibling mast, both ends resolve to one mast, or neither radio answered
    (``UNKNOWN``) — excludes THAT link with a named reason and touches nothing else. Returns
    ``{"merged": n, "excluded": n}`` for the export summary and audit record (Principle V); a
    held half-known link counts as merged.
    """

    def _identified(radio: SikluRadioStatus | None) -> TypeGuard[SikluRadioStatus]:
        return radio is not None and bool((radio.device.identity or "").strip())

    merged = excluded = 0
    for logical in links:
        label = f"siklu {logical.name}"
        if logical.state is LinkState.UNKNOWN:
            geometry.excluded.append(
                ArExcluded(link=label, reason="radios unreachable — link state unknown")
            )
            excluded += 1
            continue
        end_a, end_b = logical.end_a, logical.end_b

        # The reachable, identified end anchors name derivation for a dark peer (071). With
        # neither end identified there is nothing to ground the link on.
        present = end_a if _identified(end_a) else (end_b if _identified(end_b) else None)
        if present is None:
            geometry.excluded.append(
                ArExcluded(link=label, reason="an end radio is missing/unidentified")
            )
            excluded += 1
            continue
        present_name = (present.device.identity or "").split(".")[0]

        # (endpoint name, radio-or-None) for each end IN a→b order: an identified end uses its
        # own name; a dark end derives its name from the reachable peer (half-known hold, 071).
        ends_spec: list[tuple[str, SikluRadioStatus | None]] = []
        reason: str | None = None
        for end in (end_a, end_b):
            if _identified(end):
                ends_spec.append(((end.device.identity or "").split(".")[0], end))
                continue
            derived = counterpart_name(present_name)
            if derived is None:
                reason = (
                    f"half-known link: cannot derive the dark end's name from {present_name}"
                )
                break
            ends_spec.append((derived, None))
        if reason is not None:
            geometry.excluded.append(ArExcluded(link=label, reason=reason))
            excluded += 1
            continue

        resolved: list[ArEndpoint] = []
        for name, radio in ends_spec:
            existing = next((e for e in geometry.endpoints if e.name == name), None)
            if existing is not None:
                resolved.append(existing)
                continue
            sibling = _sibling_endpoint(name, geometry.endpoints)
            if sibling is None:
                reason = f"endpoint {name}: no UISP sibling mast to inherit geometry from"
                break
            resolved.append(
                ArEndpoint(
                    name=name,
                    site=sibling.site,
                    latitude=sibling.latitude,
                    longitude=sibling.longitude,
                    raw_altitude=None,
                    height_amsl_m=sibling.height_amsl_m,
                    height_provenance=sibling.height_provenance,
                    status=_siklu_endpoint_status(radio) if radio is not None else "unknown",
                    # 097: a towered sibling's provenance carries over — the e-band radio
                    # sits on the same declared tower (no tower_placed record: it never had
                    # a prior position of its own, nothing moved).
                    location_provenance=sibling.location_provenance,
                )
            )
        if reason is not None:
            geometry.excluded.append(ArExcluded(link=label, reason=reason))
            excluded += 1
            continue

        a, b = resolved
        if (a.latitude, a.longitude) == (b.latitude, b.longitude):
            geometry.excluded.append(
                ArExcluded(link=label, reason="both ends resolve to the same mast")
            )
            excluded += 1
            continue

        # Register the (possibly new) endpoints + their ENU rows before building the link.
        for endpoint in (a, b):
            if endpoint not in geometry.endpoints:
                geometry.endpoints.append(endpoint)
                for site in geometry.sites:
                    east, north = enu_offset(
                        site.latitude, site.longitude, endpoint.latitude, endpoint.longitude
                    )
                    up = endpoint.height_amsl_m - site.ground_elevation_m
                    site.enu[endpoint.name] = (round(east, 2), round(north, 2), round(up, 2))

        east, north = enu_offset(a.latitude, a.longitude, b.latitude, b.longitude)
        length = math.sqrt(east**2 + north**2 + (b.height_amsl_m - a.height_amsl_m) ** 2)
        mhz, provenance = _siklu_frequency_mhz(end_a, end_b)
        capacity_down, capacity_up = _siklu_capacity_bps(end_a, end_b)
        geometry.links.append(
            ArLink(
                id=f"siklu:{logical.name}",
                name=f"{a.site} <-> {b.site} ({_SIKLU_BAND})",
                a=a.name,
                b=b.name,
                band=_SIKLU_BAND,
                health=_SIKLU_HEALTH[logical.state],
                signal_dbm=_siklu_signal_dbm(end_a, end_b),
                length_m=round(length, 1),
                frequency_ghz=round(mhz / 1000.0, 3),
                frequency_provenance=provenance,
                fresnel_radius_m=round(fresnel_radius_m(length, mhz), 2),
                capacity_down_bps=capacity_down,
                capacity_up_bps=capacity_up,
                source="siklu",
            )
        )
        merged += 1

    geometry.links.sort(key=lambda link: link.name)
    geometry.excluded.sort(key=lambda e: e.link)
    return {"merged": merged, "excluded": excluded}

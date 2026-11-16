"""Pure, total link-pairing for Siklu point-to-point hops (feature 020).

No I/O. Correlates the two radios that form a point-to-point hop into a single
:class:`~obi.core.models.LogicalLink` and classifies its state + asymmetry. This is the first
concrete cross-end correlation in obi, scoped narrowly to the Burning Man naming convention
(``<siteA>-ptp-<siteB>-<band>`` ↔ ``<siteB>-ptp-<siteA>-<band>``) with an operator override —
exactly the pure-core split feature 017 used for ``reconcile.py`` (kept out of the engine and the
adapter because it is cross-device correlation, not vendor-specific I/O).

CRITICAL (research §6): per-direction ``mode``/``air_capacity_mbps`` differences are NORMAL on a
healthy hop (live: ``qam128``/10000 vs ``qam32``/5000) — the classifier MUST NOT read them for
asymmetry. It flags only **hard** disagreement: one end RF up while the other is RF down, or an end
unreachable (→ ``half_known``); a wide RSSI/CINR delta is surfaced as a single informational note.
Every function is total — it never raises.
"""

from __future__ import annotations

import re

from obi.core.models import LinkState, LogicalLink, Reachability, SikluRadioStatus

NAME_RE = re.compile(r"^(?P<site>[^-]+)-ptp-(?P<far>[^-]+)-(?P<band>\d+)$")

# RSSI/CINR deltas beyond these wide sanity bands are surfaced as an informational note (NOT an
# error, and NOT a state change) — they hint at a one-sided alignment/path issue worth a look.
_RSSI_DELTA_DB = 15
_CINR_DELTA_DB = 15


def counterpart_name(name: str) -> str | None:
    """``sitea-ptp-siteb-70`` → ``siteb-ptp-sitea-70``; ``None`` if it doesn't match.

    Pure name transform; no roster lookup. A name that does not follow the convention has no
    derivable counterpart (the operator may still declare one via an override).
    """

    match = NAME_RE.match(name.strip())
    if match is None:
        return None
    return f"{match['far']}-ptp-{match['site']}-{match['band']}"


def _canonical_name(a_name: str, b_name: str | None) -> str:
    """A deterministic, end-order-independent hop id.

    For convention names, ``<min(site,far)>↔<max(site,far)>-<band>`` (e.g. ``sitea↔siteb-70``)
    so both ends and any pairing direction yield the same id. Otherwise the two identities sorted,
    joined with ``↔``; a half-known link with an unknown peer uses the present name alone.
    """

    match_a = NAME_RE.match(a_name.strip())
    if b_name is not None:
        match_b = NAME_RE.match(b_name.strip())
        if match_a and match_b and match_a["band"] == match_b["band"]:
            sites = sorted({match_a["site"], match_a["far"]})
            return f"{sites[0]}↔{sites[1]}-{match_a['band']}"
        lo, hi = sorted([a_name.strip(), b_name.strip()])
        return f"{lo}↔{hi}"
    if match_a:
        sites = sorted({match_a["site"], match_a["far"]})
        return f"{sites[0]}↔{sites[1]}-{match_a['band']}"
    return a_name.strip()


def _up(status: SikluRadioStatus | None) -> bool:
    return (
        status is not None
        and status.reachability is Reachability.REACHABLE
        and status.rf is not None
        and status.rf.operational is True
    )


def _reach(status: SikluRadioStatus | None) -> bool:
    return status is not None and status.reachability is Reachability.REACHABLE


def _end_label(status: SikluRadioStatus | None) -> str:
    if status is None:
        return "peer"
    return status.device.identity or status.device.address


def classify_link(
    a: SikluRadioStatus | None, b: SikluRadioStatus | None
) -> tuple[LinkState, list[str]]:
    """Pure, total. Return ``(state, asymmetries)`` for one hop's two ends (data-model §LinkState).

    ``asymmetries`` carries only HARD disagreements (and, when both ends are up, an optional wide
    RSSI/CINR-delta note). It never flags differing ``mode``/``air_capacity_mbps`` — that is normal
    per-direction adaptation (research §6).
    """

    present = [end for end in (a, b) if end is not None]
    if not present:
        return LinkState.UNKNOWN, []

    reachable = [end for end in present if _reach(end)]

    # Exactly one end present-and-reachable → half_known (the peer is missing or unreachable).
    if len(reachable) < 2:
        # Identify the absent/unreachable end for the flag.
        if a is None or b is None:
            missing = b if a is not None else a  # the None side
            known = a if a is not None else b
            if missing is None and known is not None and _reach(known):
                return (
                    LinkState.HALF_KNOWN,
                    [f"{_end_label(known)}: peer not in roster"],
                )
        # Both ends are present objects but at least one is not REACHABLE.
        flags = [
            f"{_end_label(end)} {end.reachability.value}" for end in present if not _reach(end)
        ]
        if reachable:
            return LinkState.HALF_KNOWN, flags
        # Neither end is reachable → cannot judge the link.
        return LinkState.UNKNOWN, []

    # Both ends reachable from here on (the half_known guard above guarantees both are present);
    # the explicit None-check keeps the function total (it never raises) and narrows for the type
    # checker.
    if a is None or b is None:  # pragma: no cover - unreachable given the guard above
        return LinkState.UNKNOWN, []
    a_up, b_up = _up(a), _up(b)
    if a_up and b_up:
        return LinkState.UP, _delta_notes(a, b)
    if a_up != b_up:  # exactly one RF up → degraded (hard disagreement)
        down_end = a if not a_up else b
        return (
            LinkState.DEGRADED,
            [f"{_end_label(down_end)} rf operational down (peer up)"],
        )
    return LinkState.DOWN, []  # both reachable, neither RF up


def _delta_notes(a: SikluRadioStatus, b: SikluRadioStatus) -> list[str]:
    """Wide RSSI/CINR-delta notes for an up link (informational; never per-direction modulation)."""

    notes: list[str] = []
    rf_a, rf_b = a.rf, b.rf
    if rf_a is None or rf_b is None:
        return notes
    if rf_a.rssi_dbm is not None and rf_b.rssi_dbm is not None:
        delta = abs(rf_a.rssi_dbm - rf_b.rssi_dbm)
        if delta > _RSSI_DELTA_DB:
            notes.append(f"rssi delta {delta} dB between ends")
    if rf_a.cinr_db is not None and rf_b.cinr_db is not None:
        delta = abs(rf_a.cinr_db - rf_b.cinr_db)
        if delta > _CINR_DELTA_DB:
            notes.append(f"cinr delta {delta} dB between ends")
    return notes


def derive_links(
    radios: list[SikluRadioStatus],
    *,
    overrides: list[tuple[str, str]] | None = None,
) -> list[LogicalLink]:
    """Pure, total. Group radios into :class:`LogicalLink`s (contracts/siklu-pairing.md).

    Order of resolution: explicit ``overrides`` first (paired by identity,
    ``paired_by="override"``), then name-symmetric pairs via :func:`counterpart_name`
    (``paired_by="name"``). A radio whose name
    implies a counterpart NOT present in ``radios`` becomes a ``half_known`` link (present end set,
    peer ``None``). A radio whose name doesn't match :data:`NAME_RE` and isn't in an override is NOT
    emitted as a link (it stays only in the radios list). Each radio appears in at most one link;
    links are returned in canonical-name order.
    """

    by_identity: dict[str, SikluRadioStatus] = {
        r.device.identity: r for r in radios if r.device.identity
    }
    consumed: set[int] = set()  # ids of radios already placed in a link
    links: list[LogicalLink] = []

    # 1) explicit overrides (by identity).
    for name_a, name_b in overrides or []:
        a = by_identity.get(name_a)
        b = by_identity.get(name_b)
        if a is None and b is None:
            continue
        for end in (a, b):
            if end is not None:
                consumed.add(id(end))
        state, asymmetries = classify_link(a, b)
        # half_known wording when only one end was found in the roster.
        if a is None or b is None:
            present = a or b
            missing_name = name_b if a is not None else name_a
            asymmetries = [f"{_end_label(present)}: peer {missing_name} not in roster"]
        links.append(
            LogicalLink(
                name=_canonical_name(
                    (a or b).device.identity if (a or b) else name_a,  # type: ignore[union-attr]
                    b.device.identity if b is not None else (name_b if a is not None else None),
                ),
                state=state,
                paired_by="override",
                end_a=a,
                end_b=b,
                asymmetries=asymmetries,
            )
        )

    # 2) name-symmetric pairs.
    for radio in radios:
        if id(radio) in consumed:
            continue
        name = radio.device.identity
        if not name:
            continue
        counterpart = counterpart_name(name)
        if counterpart is None:
            continue  # non-conforming, no override → not emitted as a link
        consumed.add(id(radio))
        peer = by_identity.get(counterpart)
        if peer is not None and id(peer) not in consumed:
            consumed.add(id(peer))
            state, asymmetries = classify_link(radio, peer)
            links.append(
                LogicalLink(
                    name=_canonical_name(name, counterpart),
                    state=state,
                    paired_by="name",
                    end_a=radio,
                    end_b=peer,
                    asymmetries=asymmetries,
                )
            )
        else:
            # Counterpart not present in the roster → half-known link (peer is None), not dropped.
            state, asymmetries = classify_link(radio, None)
            asymmetries = [f"{_end_label(radio)}: peer {counterpart} not in roster"]
            links.append(
                LogicalLink(
                    name=_canonical_name(name, counterpart),
                    state=state,
                    paired_by="name",
                    end_a=radio,
                    end_b=None,
                    asymmetries=asymmetries,
                )
            )

    links.sort(key=lambda link: link.name)
    return links

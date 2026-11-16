"""Unit: pure Siklu link pairing + asymmetry classification (feature 020).

Covers the full matrix from contracts/siklu-pairing.md: the qam128/qam32 anti-false-positive case,
up/down → degraded, half-known (peer absent / unreachable), unpairable names, override pairing, and
the RSSI-delta note. All functions are pure and total (never raise).
"""

from __future__ import annotations

from obi.core.models import Device, LinkState, Reachability, RfLink, SikluRadioStatus
from obi.core.pairing import classify_link, counterpart_name, derive_links


def _radio(
    name: str,
    *,
    address: str = "172.25.110.1",
    reachability: Reachability = Reachability.REACHABLE,
    operational: bool | None = True,
    rssi: int | None = -44,
    cinr: int | None = 27,
    mode: str | None = "adaptive qam128",
    air: int | None = 10000,
) -> SikluRadioStatus:
    rf = (
        RfLink(
            operational=operational,
            rssi_dbm=rssi,
            cinr_db=cinr,
            mode=mode,
            air_capacity_mbps=air,
        )
        if reachability is Reachability.REACHABLE
        else None
    )
    return SikluRadioStatus(
        device=Device(identity=name, address=address),
        reachability=reachability,
        rf=rf,
    )


def test_counterpart_name() -> None:
    assert counterpart_name("tamarack-ptp-basswood-70") == "basswood-ptp-tamarack-70"
    assert counterpart_name("basswood-ptp-tamarack-70") == "tamarack-ptp-basswood-70"
    assert counterpart_name("core-gw-1") is None  # not the convention
    assert counterpart_name("a-ptp-b") is None  # no band


def test_qam_asymmetry_is_not_flagged() -> None:
    # The key anti-false-positive case: a healthy link with different per-direction modulation.
    a = _radio("tamarack-ptp-basswood-70", mode="adaptive qam128", air=10000, rssi=-44, cinr=27)
    b = _radio("basswood-ptp-tamarack-70", mode="adaptive qam32", air=5000, rssi=-43, cinr=27)
    state, asym = classify_link(a, b)
    assert state is LinkState.UP
    assert asym == []


def test_one_end_rf_down_is_degraded() -> None:
    a = _radio("tamarack-ptp-basswood-70", operational=True)
    b = _radio("basswood-ptp-tamarack-70", operational=False)
    state, asym = classify_link(a, b)
    assert state is LinkState.DEGRADED
    assert len(asym) == 1
    assert "basswood-ptp-tamarack-70" in asym[0]
    assert "down" in asym[0]


def test_both_down_is_down() -> None:
    a = _radio("a-ptp-b-70", operational=False)
    b = _radio("b-ptp-a-70", operational=False)
    state, asym = classify_link(a, b)
    assert state is LinkState.DOWN
    assert asym == []


def test_one_end_unreachable_is_half_known() -> None:
    a = _radio("tamarack-ptp-basswood-70", reachability=Reachability.REACHABLE)
    b = _radio("basswood-ptp-tamarack-70", reachability=Reachability.TIMED_OUT)
    state, asym = classify_link(a, b)
    assert state is LinkState.HALF_KNOWN
    assert any("timed_out" in flag for flag in asym)


def test_peer_absent_is_half_known() -> None:
    a = _radio("tamarack-ptp-basswood-70")
    state, asym = classify_link(a, None)
    assert state is LinkState.HALF_KNOWN
    assert asym and "peer not in roster" in asym[0]


def test_neither_reachable_is_unknown() -> None:
    a = _radio("a-ptp-b-70", reachability=Reachability.UNREACHABLE)
    b = _radio("b-ptp-a-70", reachability=Reachability.AUTH_FAILED)
    state, _ = classify_link(a, b)
    assert state is LinkState.UNKNOWN


def test_rssi_delta_note() -> None:
    near = classify_link(_radio("a-ptp-b-70", rssi=-44), _radio("b-ptp-a-70", rssi=-46))
    assert near == (LinkState.UP, [])  # 2 dB → no note
    far_state, far_asym = classify_link(
        _radio("a-ptp-b-70", rssi=-44), _radio("b-ptp-a-70", rssi=-69)
    )
    assert far_state is LinkState.UP  # still up — it's a NOTE, not a state change
    assert far_asym and "rssi delta" in far_asym[0]


def test_classify_is_total_on_none_combos() -> None:
    for combo in ((None, None), (_radio("a-ptp-b-70"), None), (None, _radio("b-ptp-a-70"))):
        state, asym = classify_link(*combo)
        assert isinstance(state, LinkState)
        assert isinstance(asym, list)


def test_derive_links_name_pair() -> None:
    a = _radio(
        "tamarack-ptp-basswood-70", address="172.18.89.130", mode="adaptive qam128", air=10000
    )
    b = _radio("basswood-ptp-tamarack-70", address="172.18.83.224", mode="adaptive qam32", air=5000)
    links = derive_links([a, b])
    assert len(links) == 1
    link = links[0]
    assert link.name == "basswood↔tamarack-70"
    assert link.state is LinkState.UP
    assert link.paired_by == "name"
    assert link.asymmetries == []
    assert link.end_a is not None and link.end_b is not None
    assert {link.end_a.device.identity, link.end_b.device.identity} == {
        "tamarack-ptp-basswood-70",
        "basswood-ptp-tamarack-70",
    }


def test_derive_links_half_known_when_peer_absent() -> None:
    a = _radio("tamarack-ptp-basswood-70")
    links = derive_links([a])
    assert len(links) == 1
    assert links[0].state is LinkState.HALF_KNOWN
    assert links[0].end_b is None
    assert "basswood-ptp-tamarack-70" in links[0].asymmetries[0]


def test_derive_links_unpairable_name_no_link() -> None:
    standalone = _radio("core-gw-1")  # not the convention, no override
    assert derive_links([standalone]) == []


def test_derive_links_override_pairs_non_conforming() -> None:
    a = _radio("alpha", address="172.25.110.1")
    b = _radio("beta", address="172.25.110.2")
    links = derive_links([a, b], overrides=[("alpha", "beta")])
    assert len(links) == 1
    assert links[0].paired_by == "override"
    assert links[0].state is LinkState.UP
    assert links[0].end_a is not None and links[0].end_b is not None
    assert {links[0].end_a.device.identity, links[0].end_b.device.identity} == {"alpha", "beta"}


def test_derive_links_override_takes_precedence_over_name() -> None:
    # Override consumes a radio so the name rule cannot also place it.
    a = _radio("tamarack-ptp-basswood-70")
    b = _radio("basswood-ptp-tamarack-70")
    c = _radio("oddball", address="172.25.110.3")
    links = derive_links([a, b, c], overrides=[("tamarack-ptp-basswood-70", "oddball")])
    by_name = {link.name: link for link in links}
    # tamarack is paired with oddball by override; basswood becomes half-known by name.
    assert any(link.paired_by == "override" for link in links)
    mountain_link = next(
        link
        for link in links
        if link.end_a and link.end_a.device.identity == "basswood-ptp-tamarack-70"
    )
    assert mountain_link.state is LinkState.HALF_KNOWN
    assert "tamarack-ptp-basswood-70" in (mountain_link.asymmetries[0])
    assert len(by_name) == len(links)  # deterministic, unique names

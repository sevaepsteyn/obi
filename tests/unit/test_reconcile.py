"""T006: classify_drift — the pure presence-drift classification (feature 017).

Covers the full DriftClass matrix against the live-preview baseline shapes: matched by
serial / canonical address / alt address / name fallback, undocumented, retire-candidate (+ the
last_seen annotation), outside-scope, un-correlatable, non-MikroTik exclusion, no double counting,
and reality-unavailable → [].
"""

from __future__ import annotations

from datetime import UTC, datetime

from obi.core.inventory import Inventory, InventoryEntry
from obi.core.models import DiscoveryScope, DiscoveryState, DriftClass, NetboxDevice
from obi.core.reconcile import classify_drift

SCOPE = DiscoveryScope(ranges=["172.18.0.0/16"], seeds=["172.18.89.1"])
T0 = datetime(2026, 6, 25, 6, 0, tzinfo=UTC)


def _entry(
    address: str,
    identity: str,
    *,
    serial: str | None = None,
    alt: list[str] | None = None,
    model: str | None = None,
    state: DiscoveryState = DiscoveryState.IDENTIFIED,
    present: bool = True,
    last_seen: datetime = T0,
) -> InventoryEntry:
    return InventoryEntry(
        address=address,
        identity=identity,
        discovery_state=state,
        model=model,
        serial=serial,
        alt_addresses=alt or [],
        present_in_latest=present,
        last_seen=last_seen,
    )


def _nb(
    name: str,
    primary_ip: str | None,
    *,
    manufacturer: str | None = "MikroTik",
    model: str | None = None,
    site: str | None = None,
    serial: str | None = None,
) -> NetboxDevice:
    return NetboxDevice(
        name=name,
        primary_ip=primary_ip,
        manufacturer=manufacturer,
        model=model,
        site=site,
        serial=serial,
    )


def _roster(entries: list[InventoryEntry]) -> Inventory:
    return Inventory(scope=SCOPE, entries=entries)


def _by_class(drift: list) -> dict[DriftClass, list]:
    out: dict[DriftClass, list] = {}
    for d in drift:
        out.setdefault(d.drift_class, []).append(d)
    return out


def test_matched_by_canonical_address() -> None:
    drift = classify_drift(
        _roster([_entry("172.18.86.1", "walnut-gw")]), [_nb("walnut-gw", "172.18.86.1/24")], SCOPE
    )
    assert [d.drift_class for d in drift] == [DriftClass.MATCHED]
    assert drift[0].matched_on == "address"


def test_matched_by_alt_address_multi_address_device() -> None:
    # core-gw-1 (feature 018): one entry at .0.1 with alt .0.11; NetBox documents it at .0.11.
    roster = _roster(
        [_entry("172.18.84.1", "core-gw-1", serial="18F630D8C80B", alt=["172.18.84.11"])]
    )
    drift = classify_drift(roster, [_nb("core-gw-1", "172.18.84.11/24")], SCOPE)
    # Matched via the alt address — NOT a false undocumented(.0.1) + retire(.0.11) pair.
    assert [d.drift_class for d in drift] == [DriftClass.MATCHED]
    assert drift[0].matched_on == "address"
    assert "172.18.84.11" in drift[0].addresses


def test_matched_by_serial_even_with_different_ip() -> None:
    roster = _roster([_entry("172.18.84.1", "core-gw-1", serial="SER123")])
    drift = classify_drift(roster, [_nb("core-gw-1", "10.99.99.99/24", serial="SER123")], SCOPE)
    assert drift[0].drift_class is DriftClass.MATCHED
    assert drift[0].matched_on == "serial"


def test_matched_by_name_fallback() -> None:
    roster = _roster([_entry("172.18.93.9", "weird-gw")])  # no serial; its IP isn't documented
    drift = classify_drift(roster, [_nb("weird-gw", "172.18.84.250/24")], SCOPE)
    assert drift[0].drift_class is DriftClass.MATCHED
    assert drift[0].matched_on == "name"


def test_undocumented_live_device() -> None:
    drift = classify_drift(_roster([_entry("172.18.74.1", "chestnut-internet-gw")]), [], SCOPE)
    assert [d.drift_class for d in drift] == [DriftClass.UNDOCUMENTED]
    assert drift[0].name == "chestnut-internet-gw"
    assert drift[0].addresses == ["172.18.74.1"]


def test_retire_candidate_in_scope_with_last_seen() -> None:
    live = _entry("172.18.86.1", "walnut-gw")
    gone = _entry("172.18.90.1", "wingnut-gw", present=False, last_seen=T0)  # seen before, not now
    drift = classify_drift(
        _roster([live, gone]),
        [_nb("walnut-gw", "172.18.86.1/24"), _nb("wingnut-gw", "172.18.90.1/24")],
        SCOPE,
    )
    rc = _by_class(drift)[DriftClass.RETIRE_CANDIDATE]
    assert [d.name for d in rc] == ["wingnut-gw"]
    assert rc[0].last_seen == T0  # annotated from the non-present roster entry


def test_retire_candidate_never_seen() -> None:
    drift = classify_drift(
        _roster([_entry("172.18.86.1", "walnut-gw")]),
        [_nb("walnut-gw", "172.18.86.1/24"), _nb("core-gw-2", "172.18.84.12/24")],
        SCOPE,
    )
    rc = _by_class(drift)[DriftClass.RETIRE_CANDIDATE][0]
    assert rc.name == "core-gw-2"
    assert rc.last_seen is None  # genuinely retired — never in the roster (the live core-gw-2 case)


def test_outside_scope_not_evaluated() -> None:
    drift = classify_drift(
        _roster([_entry("172.18.86.1", "walnut-gw")]),
        [_nb("walnut-gw", "172.18.86.1/24"), _nb("core-sw-mgmt", "172.24.2.15/25")],
        SCOPE,
    )
    assert [d.name for d in _by_class(drift)[DriftClass.OUTSIDE_SCOPE]] == ["core-sw-mgmt"]


def test_un_correlatable_no_primary_ip() -> None:
    drift = classify_drift(
        _roster([_entry("172.18.86.1", "walnut-gw")]),
        [_nb("walnut-gw", "172.18.86.1/24"), _nb("aspen-gw-glch", None)],
        SCOPE,
    )
    assert [d.name for d in _by_class(drift)[DriftClass.UN_CORRELATABLE]] == ["aspen-gw-glch"]


def test_non_mikrotik_excluded() -> None:
    drift = classify_drift(
        _roster([_entry("172.18.86.1", "walnut-gw")]),
        [
            _nb("walnut-gw", "172.18.86.1/24"),
            _nb("a-switch", "172.18.84.99/24", manufacturer="Ubiquiti"),
            _nb("a-pdu", "172.18.84.98/24", manufacturer="APC"),
        ],
        SCOPE,
    )
    assert {d.name for d in drift} == {"walnut-gw"}  # only the MikroTik device participates


def test_reality_unavailable_returns_empty() -> None:
    nb = [_nb("walnut-gw", "172.18.86.1/24")]
    assert classify_drift(None, nb, SCOPE) == []  # no roster
    only_unsupported = _roster([_entry("172.18.93.9", "x", state=DiscoveryState.UNSUPPORTED)])
    assert classify_drift(only_unsupported, nb, SCOPE) == []  # no live MikroTik reality


def test_no_double_count_full_matrix() -> None:
    roster = _roster(
        [_entry("172.18.86.1", "walnut-gw"), _entry("172.18.74.1", "chestnut-internet-gw")]
    )
    nb = [
        _nb("walnut-gw", "172.18.86.1/24"),           # matched
        _nb("core-gw-2", "172.18.84.12/24"),          # retire candidate
        _nb("core-sw-mgmt", "172.24.2.15/25"),      # outside scope
        _nb("aspen-gw-glch", None),                  # un-correlatable
        _nb("a-switch", "172.18.84.99/24", manufacturer="Ubiquiti"),  # excluded
    ]
    drift = classify_drift(roster, nb, SCOPE)
    classes = [d.drift_class for d in drift]
    assert (
        len(drift) == 5
    )  # chestnut-internet-gw undocumented + the 4 MikroTik above; switch excluded
    assert classes.count(DriftClass.MATCHED) == 1
    assert classes.count(DriftClass.UNDOCUMENTED) == 1
    assert classes.count(DriftClass.RETIRE_CANDIDATE) == 1
    assert classes.count(DriftClass.OUTSIDE_SCOPE) == 1
    assert classes.count(DriftClass.UN_CORRELATABLE) == 1
    # actionable drift sorts before the all-good "matched" bucket
    assert classes[0] is DriftClass.UNDOCUMENTED and classes[-1] is DriftClass.MATCHED

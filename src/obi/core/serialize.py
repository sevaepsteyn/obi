"""JSON serialization of an :class:`InventoryReport` (US2, FR-007).

Output conforms to ``contracts/status-output.schema.json``: a top-level object with the
scope, run timestamps, reachability counts, and a ``devices`` array. Each entry carries both
``identity`` (the logical key, FR-017) and ``address`` (which disambiguates blank/duplicate
identities). The array form lets duplicate or blank identities coexist without collision.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from obi.core.inventory import Inventory
from obi.core.inventory import to_dict as inventory_to_dict
from obi.core.models import (
    ArGeometry,
    BrcDocument,
    ConfigDiffResult,
    ConfigHistoryResult,
    ConfigShowResult,
    ConfigsIngestReport,
    ConfigStatusReport,
    DeviceDrift,
    DeviceStatus,
    DiscoveryState,
    DriftReport,
    EthPort,
    InventoryReport,
    LogEvent,
    LogicalLink,
    LogReport,
    MetricReport,
    MetricSample,
    NetboxDevice,
    NetboxReport,
    OspfDefaultOrigination,
    OspfEgressChoice,
    OspfInstanceInfo,
    OspfTopology,
    PortL2,
    PortNeighbor,
    PortPoe,
    PortTransceiver,
    Reachability,
    RfLink,
    RouterDetail,
    RouterPhy,
    RouterPort,
    SikluRadioStatus,
    SikluReport,
    TerrainDocument,
    UispLinkEnd,
    UispLinksReport,
    UispReport,
    UnifiReport,
)


def inventory_to_json(inventory: Inventory, *, indent: int | None = 2) -> str:
    """Serialize the persisted inventory; conforms to ``contracts/inventory.schema.json``."""

    return json.dumps(inventory_to_dict(inventory), indent=indent, ensure_ascii=False)


def device_status_to_dict(status: DeviceStatus) -> dict[str, Any]:
    device = status.device
    return {
        "identity": device.identity or None,
        "address": device.address,
        "model": device.model,
        "version": device.version,
        "reachability": status.reachability.value,
        "uptime_seconds": status.uptime_seconds,
        "cpu_load_percent": status.cpu_load_percent,
        "free_memory_bytes": status.free_memory_bytes,
        "total_memory_bytes": status.total_memory_bytes,
        "gathered_at": status.gathered_at.isoformat(),
        "notes": list(status.notes),
    }


def report_to_dict(report: InventoryReport) -> dict[str, Any]:
    return {
        "scope": {
            "ranges": list(report.scope.ranges),
            "seeds": list(report.scope.seeds),
        },
        "started_at": report.started_at.isoformat(),
        "finished_at": report.finished_at.isoformat(),
        "counts": report.counts,
        "devices": [device_status_to_dict(status) for status in report.statuses],
    }


def report_to_json(report: InventoryReport, *, indent: int | None = 2) -> str:
    return json.dumps(report_to_dict(report), indent=indent, ensure_ascii=False)


def status_report_to_dict(inventory: Inventory, report: InventoryReport) -> dict[str, Any]:
    """Join the persisted roster with live health (feature 002).

    Conforms to ``contracts/status-output.schema.json``: every roster entry appears, carrying
    its ``discovery_state``/``provenance``/``present_in_latest``; identified devices also carry
    the live health fetched this run. Non-queried entries have ``null`` live fields.
    """

    live = {status.device.address: status for status in report.statuses}
    devices: list[dict[str, Any]] = []
    for entry in inventory.entries:
        status = live.get(entry.address)
        devices.append(
            {
                "identity": entry.identity or None,
                "address": entry.address,
                "model": entry.model,
                "version": entry.version,
                "discovery_state": entry.discovery_state.value,
                "provenance": entry.provenance,
                "present_in_latest": entry.present_in_latest,
                "reachability": status.reachability.value if status else None,
                "uptime_seconds": status.uptime_seconds if status else None,
                "cpu_load_percent": status.cpu_load_percent if status else None,
                "free_memory_bytes": status.free_memory_bytes if status else None,
                "total_memory_bytes": status.total_memory_bytes if status else None,
                "gathered_at": status.gathered_at.isoformat() if status else None,
                "notes": list(status.notes) if status else [],
            }
        )

    counts: dict[str, int] = {"total": len(inventory.entries)}
    for reachability in Reachability:
        counts[reachability.value] = sum(
            1 for s in report.statuses if s.reachability is reachability
        )
    counts["unsupported"] = sum(
        1 for e in inventory.entries if e.discovery_state is DiscoveryState.UNSUPPORTED
    )
    counts["absent"] = sum(1 for e in inventory.entries if not e.present_in_latest)

    return {
        "scope": {"ranges": list(inventory.scope.ranges), "seeds": list(inventory.scope.seeds)},
        "started_at": report.started_at.isoformat(),
        "finished_at": report.finished_at.isoformat(),
        "counts": counts,
        "devices": devices,
    }


def status_report_to_json(
    inventory: Inventory, report: InventoryReport, *, indent: int | None = 2
) -> str:
    return json.dumps(status_report_to_dict(inventory, report), indent=indent, ensure_ascii=False)


def uisp_device_to_dict(status: DeviceStatus) -> dict[str, Any]:
    """One device in the live UISP report (``contracts/uisp-status-output.schema.json``)."""

    device = status.device
    return {
        "vendor": "uisp",
        "identity": device.identity or None,
        "address": device.address,
        "model": device.model,
        "version": device.version,
        "reachability": status.reachability.value,
        "uptime_seconds": status.uptime_seconds,
        "cpu_load_percent": status.cpu_load_percent,
        "free_memory_bytes": status.free_memory_bytes,
        "total_memory_bytes": status.total_memory_bytes,
        "gathered_at": status.gathered_at.isoformat(),
        "notes": list(status.notes),
        # 095 follow-on: the controller's operator-set maintenance flag (the dash fleet tile
        # shows these yellow without letting them taint the rollup).
        "maintenance": status.maintenance,
    }


def uisp_report_to_dict(report: UispReport) -> dict[str, Any]:
    """Serialize the live UISP report (feature 003) to the shape in
    ``contracts/uisp-status-output.schema.json``. No roster fields — UISP is not persisted."""

    return {
        "controller": report.controller,
        "controller_outcome": report.outcome.value,
        "filter": report.name_filter,
        "started_at": report.started_at.isoformat(),
        "finished_at": report.finished_at.isoformat(),
        "counts": report.counts,
        "devices": [uisp_device_to_dict(status) for status in report.statuses],
    }


def uisp_report_to_json(report: UispReport, *, indent: int | None = 2) -> str:
    return json.dumps(uisp_report_to_dict(report), indent=indent, ensure_ascii=False)


def _uisp_link_end_to_dict(end: UispLinkEnd) -> dict[str, Any]:
    radio = end.radio
    return {
        "name": end.name,
        "site": end.site,
        "radio": None
        if radio is None
        else {
            "status": radio.status,
            "signal_dbm": radio.signal_dbm,
            "frequency_mhz": radio.frequency_mhz,
            "score": radio.score,
            "link_score": radio.link_score,
            "downlink_score": radio.downlink_score,
            "uplink_score": radio.uplink_score,
            "airtime": radio.airtime,
            "capacity_down_bps": radio.capacity_down_bps,
            "capacity_up_bps": radio.capacity_up_bps,
        },
    }


def uisp_links_report_to_dict(report: UispLinksReport) -> dict[str, Any]:
    """Serialize the live UISP link-health report (feature 034; contracts/surfaces.md)."""

    return {
        "controller": report.controller,
        "outcome": report.outcome.value,
        "detail": report.detail,
        "name_filter": report.name_filter,
        "counts": report.counts,
        "links": [
            {
                "id": link.id,
                "label": link.label,
                "state": link.state,
                "signal_dbm": link.signal_dbm,
                "ssid": link.ssid,
                "frequency_mhz": link.frequency_mhz,
                "distance_m": link.distance_m,
                "ends": [
                    _uisp_link_end_to_dict(link.end_a),
                    _uisp_link_end_to_dict(link.end_b),
                ],
            }
            for link in report.links
        ],
        "started_at": report.started_at.isoformat(),
        "finished_at": report.finished_at.isoformat(),
    }


def uisp_links_report_to_json(report: UispLinksReport, *, indent: int | None = 2) -> str:
    return json.dumps(uisp_links_report_to_dict(report), indent=indent, ensure_ascii=False)


def unifi_device_to_dict(status: DeviceStatus) -> dict[str, Any]:
    """One device in the live UniFi report (feature 011)."""

    device = status.device
    return {
        "vendor": "unifi",
        "identity": device.identity or None,
        "address": device.address,
        "model": device.model,
        "version": device.version,
        "reachability": status.reachability.value,
        "uptime_seconds": status.uptime_seconds,
        "cpu_load_percent": status.cpu_load_percent,
        "free_memory_bytes": status.free_memory_bytes,
        "total_memory_bytes": status.total_memory_bytes,
        "gathered_at": status.gathered_at.isoformat(),
        "notes": list(status.notes),
    }


def unifi_report_to_dict(report: UnifiReport) -> dict[str, Any]:
    """Serialize the live UniFi report (feature 011). No roster fields — UniFi is not persisted."""

    return {
        "console": report.console,
        "console_outcome": report.outcome.value,
        "filter": report.name_filter,
        "started_at": report.started_at.isoformat(),
        "finished_at": report.finished_at.isoformat(),
        "counts": report.counts,
        "devices": [unifi_device_to_dict(status) for status in report.statuses],
    }


def unifi_report_to_json(report: UnifiReport, *, indent: int | None = 2) -> str:
    return json.dumps(unifi_report_to_dict(report), indent=indent, ensure_ascii=False)


def netbox_device_to_dict(device: NetboxDevice) -> dict[str, Any]:
    """One device in the documented NetBox report (``contracts/netbox-output.schema.json``).

    Documented attributes only — NO live-health fields (FR-004, SC-008).
    """

    return {
        "name": device.name,
        "site": device.site,
        "role": device.role,
        "model": device.model,
        "manufacturer": device.manufacturer,
        "status": device.status,
        "primary_ip": device.primary_ip,
        "serial": device.serial,
        "notes": list(device.notes),
    }


def netbox_report_to_dict(report: NetboxReport) -> dict[str, Any]:
    """Serialize the documented NetBox report (feature 007) to the shape in
    ``contracts/netbox-output.schema.json``. No roster fields and no live-health fields — NetBox
    is read live and documents intended state."""

    return {
        "source": report.source,
        "source_outcome": report.outcome.value,
        "filter": report.name_filter,
        "started_at": report.started_at.isoformat(),
        "finished_at": report.finished_at.isoformat(),
        "counts": report.counts,
        "devices": [netbox_device_to_dict(device) for device in report.devices],
    }


def netbox_report_to_json(report: NetboxReport, *, indent: int | None = 2) -> str:
    return json.dumps(netbox_report_to_dict(report), indent=indent, ensure_ascii=False)


def device_drift_to_dict(drift: DeviceDrift) -> dict[str, Any]:
    """One classified device in the drift report (``contracts/drift-output.schema.json``)."""

    return {
        "drift_class": drift.drift_class.value,
        "name": drift.name,
        "addresses": list(drift.addresses),
        "model": drift.model,
        "serial": drift.serial,
        "site": drift.site,
        "last_seen": drift.last_seen.isoformat() if drift.last_seen else None,
        "matched_on": drift.matched_on,
        "notes": list(drift.notes),
    }


def drift_report_to_dict(report: DriftReport) -> dict[str, Any]:
    """Serialize the read-only reconcile report (feature 017) to the shape in
    ``contracts/drift-output.schema.json``. Writes nothing; presence drift only."""

    return {
        "source": report.source,
        "source_outcome": report.source_outcome.value,
        "roster_generated_at": (
            report.roster_generated_at.isoformat() if report.roster_generated_at else None
        ),
        "scope": (
            {"ranges": list(report.scope.ranges), "seeds": list(report.scope.seeds)}
            if report.scope is not None
            else None
        ),
        "name_filter": report.name_filter,
        "detail": report.detail,
        "started_at": report.started_at.isoformat(),
        "finished_at": report.finished_at.isoformat(),
        "counts": report.counts,
        "drift": [device_drift_to_dict(d) for d in report.drift],
    }


def drift_report_to_json(report: DriftReport, *, indent: int | None = 2) -> str:
    return json.dumps(drift_report_to_dict(report), indent=indent, ensure_ascii=False)


def metric_sample_to_dict(sample: MetricSample) -> dict[str, Any]:
    """One SNMP reading (``contracts/snmp-report.schema.json``)."""

    return {
        "name": sample.name,
        "oid": sample.oid,
        "kind": sample.kind.value,
        "value": sample.value,
        "index": sample.index,
        "label": sample.label,
        "note": sample.note,
    }


def metric_report_to_dict(report: MetricReport) -> dict[str, Any]:
    """One device's SNMP read (``contracts/snmp-report.schema.json``). Pure data — raw values."""

    return {
        "target": {
            "address": report.target.address,
            "identity": report.target.identity or None,
            "source": report.target.source,
        },
        "outcome": report.outcome.value,
        "profile": report.profile,
        "gathered_at": report.gathered_at.isoformat(),
        "notes": list(report.notes),
        "samples": [metric_sample_to_dict(sample) for sample in report.samples],
    }


def metric_reports_to_json(reports: list[MetricReport], *, indent: int | None = 2) -> str:
    """Serialize an SNMP sweep (feature 012) as ``{"reports": [...]}``."""

    payload = {"reports": [metric_report_to_dict(report) for report in reports]}
    return json.dumps(payload, indent=indent, ensure_ascii=False)


def log_event_to_dict(event: LogEvent) -> dict[str, Any]:
    """One ingested syslog line (feature 013) — historical log text, no live-health fields."""

    return {
        "timestamp": event.ts.isoformat(),
        "host": event.host,
        "subsystem": event.subsystem,
        "topics": event.topics,
        "severity": event.severity,
        "message": event.message,
        "collectors": list(event.collectors),
    }


def log_report_to_dict(report: LogReport) -> dict[str, Any]:
    """Serialize a :class:`LogReport` (feature 013): the bounded, newest-first matched lines plus
    the store-level outcome and severity counts. No device-health fields — these are logs."""

    return {
        "outcome": report.outcome.value,
        "truncated": report.truncated,
        "counts": report.counts,
        "events": [log_event_to_dict(event) for event in report.events],
    }


def log_report_to_json(report: LogReport, *, indent: int | None = 2) -> str:
    return json.dumps(log_report_to_dict(report), indent=indent, ensure_ascii=False)


# --- Siklu EtherHaul (feature 020) ------------------------------------------------------------


def _siklu_rf_to_dict(rf: RfLink) -> dict[str, Any]:
    """One RF link snapshot (``contracts/siklu-status-output.schema.json`` ``#/definitions/rf``)."""

    return {
        "operational": rf.operational,
        "tx_state": rf.tx_state,
        "rx_state": rf.rx_state,
        "rssi_dbm": rf.rssi_dbm,
        "cinr_db": rf.cinr_db,
        "mode": rf.mode,
        "lowest_modulation": rf.lowest_modulation,
        "tx_power_dbm": rf.tx_power_dbm,
        "air_capacity_mbps": rf.air_capacity_mbps,
        "channel_width_mhz": rf.channel_width_mhz,
        "tx_frequency_mhz": rf.tx_frequency_mhz,
        "rx_frequency_mhz": rf.rx_frequency_mhz,
        "alignment_status": rf.alignment_status,
    }


def _siklu_eth_port_to_dict(port: EthPort) -> dict[str, Any]:
    """One Ethernet port (``#/definitions/eth_port``)."""

    return {
        "name": port.name,
        "admin_up": port.admin_up,
        "operational_up": port.operational_up,
        "mac": port.mac,
        "mtu": port.mtu,
        "speed_type": port.speed_type,
        "connector_type": port.connector_type,
        "tx_throughput": port.tx_throughput,
        "rx_throughput": port.rx_throughput,
        "sfp_present": port.sfp_present,
        "sfp_ddm": dict(port.sfp_ddm) if port.sfp_ddm is not None else None,
    }


def siklu_radio_to_dict(radio: SikluRadioStatus) -> dict[str, Any]:
    """One Siklu radio (``#/definitions/radio``); always carries ``vendor="siklu"`` (FR-018)."""

    device = radio.device
    return {
        "vendor": "siklu",
        "identity": device.identity or None,
        "address": device.address,
        "model": device.model,
        "version": device.version,
        "serial": device.serial,
        "reachability": radio.reachability.value,
        "uptime_seconds": radio.uptime_seconds,
        "temperature_celsius": radio.temperature_celsius,
        "location": radio.location,
        "rf": _siklu_rf_to_dict(radio.rf) if radio.rf is not None else None,
        "eth_ports": [_siklu_eth_port_to_dict(port) for port in radio.eth_ports],
        "gathered_at": radio.gathered_at.isoformat(),
        "notes": list(radio.notes),
    }


def siklu_link_to_dict(link: LogicalLink) -> dict[str, Any]:
    """One logical point-to-point link (``#/definitions/link``)."""

    return {
        "name": link.name,
        "state": link.state.value,
        "paired_by": link.paired_by,
        "asymmetries": list(link.asymmetries),
        "end_a": siklu_radio_to_dict(link.end_a) if link.end_a is not None else None,
        "end_b": siklu_radio_to_dict(link.end_b) if link.end_b is not None else None,
    }


def siklu_report_to_dict(report: SikluReport) -> dict[str, Any]:
    """Serialize a Siklu report (``contracts/siklu-status-output.schema.json``).

    ``links`` is included only when the logical-link view was requested (it is omitted otherwise so
    a plain radio report stays minimal, matching the schema's optional ``links``)."""

    payload: dict[str, Any] = {
        "started_at": report.started_at.isoformat(),
        "finished_at": report.finished_at.isoformat(),
        "counts": report.counts,
        "radios": [siklu_radio_to_dict(radio) for radio in report.radios],
    }
    if report.links:
        payload["links"] = [siklu_link_to_dict(link) for link in report.links]
    return payload


def siklu_report_to_json(report: SikluReport, *, indent: int | None = 2) -> str:
    return json.dumps(siklu_report_to_dict(report), indent=indent, ensure_ascii=False)


def ar_geometry_to_dict(geometry: ArGeometry) -> dict[str, Any]:
    """Serialize the AR geometry export (feature 022, ``contracts/ar-export.schema.json``).

    The shape is normative — the static viewer consumes it verbatim. ENU tuples become
    ``[east, north, up]`` arrays; credentials never appear anywhere in the export.
    """

    return {
        "generated_at": geometry.generated_at.isoformat(),
        "controller": geometry.controller,
        "sites": [
            {
                "name": site.name,
                "latitude": site.latitude,
                "longitude": site.longitude,
                "ground_elevation_m": site.ground_elevation_m,
                "ground_provenance": site.ground_provenance.value,
                "enu": {name: list(coords) for name, coords in site.enu.items()},
            }
            for site in geometry.sites
        ],
        "endpoints": [
            {
                "name": endpoint.name,
                "site": endpoint.site,
                "latitude": endpoint.latitude,
                "longitude": endpoint.longitude,
                "raw_altitude": endpoint.raw_altitude,
                "height_amsl_m": endpoint.height_amsl_m,
                "height_provenance": endpoint.height_provenance.value,
                "status": endpoint.status,
                "link_score": endpoint.link_score,
                # 044: the device's controller maintenance flag (endpoint dot color)
                "maintenance": endpoint.maintenance,
                # 079: which pin placed this endpoint horizontally ("device" / "site")
                "location_provenance": endpoint.location_provenance.value,
            }
            for endpoint in geometry.endpoints
        ],
        "links": [
            {
                "id": link.id,
                "name": link.name,
                "a": link.a,
                "b": link.b,
                "band": link.band,
                "health": link.health.value,
                "signal_dbm": link.signal_dbm,
                "length_m": link.length_m,
                # 025: keys always present — explicit nulls for a zoneless link, never missing
                "frequency_ghz": link.frequency_ghz,
                "frequency_provenance": (
                    link.frequency_provenance.value
                    if link.frequency_provenance is not None
                    else None
                ),
                "fresnel_radius_m": link.fresnel_radius_m,
                # 031: the clearance quadruple — same discipline, all four present or null
                "clearance_verdict": (
                    link.clearance.verdict.value if link.clearance is not None else None
                ),
                "clearance_margin_m": (
                    link.clearance.margin_m if link.clearance is not None else None
                ),
                "clearance_at_fraction": (
                    link.clearance.at_fraction if link.clearance is not None else None
                ),
                "clearance_basis": (
                    link.clearance.basis.value if link.clearance is not None else None
                ),
                # 044: capacity/usage (US2), maintenance (US3), vendor source (US4) — the
                # 025 discipline: keys always present, explicit null when unknown
                "capacity_down_bps": link.capacity_down_bps,
                "capacity_up_bps": link.capacity_up_bps,
                "usage_down_bps": link.usage_down_bps,
                "usage_up_bps": link.usage_up_bps,
                "maintenance": link.maintenance,
                "source": link.source,
            }
            for link in geometry.links
        ],
        "excluded": [
            {"link": excluded.link, "reason": excluded.reason} for excluded in geometry.excluded
        ],
        "degraded": list(geometry.degraded),
        "terrain_omitted": [
            {"item": omission.item, "reason": omission.reason}
            for omission in geometry.terrain_omitted
        ],
        # 079: endpoints relocated from a strayed device pin to their site pin — named, never
        # silent (sibling of "excluded"/"degraded"; empty when nothing needed correction).
        "site_anchored": [
            {
                "device": anchor.device,
                "site": anchor.site,
                "distance_m": anchor.distance_m,
                "device_latitude": anchor.device_latitude,
                "device_longitude": anchor.device_longitude,
                "site_latitude": anchor.site_latitude,
                "site_longitude": anchor.site_longitude,
            }
            for anchor in geometry.site_anchored
        ],
        # 097: endpoints placed on their site's declared tower — named, never silent
        # (sibling of "site_anchored"); "tower_unmatched" names configured sites that
        # matched no export origin. Both keys always present, empty when clean.
        "tower_placed": [
            {
                "device": placement.device,
                "site": placement.site,
                "tower_height_m": placement.tower_height_m,
                "device_latitude": placement.device_latitude,
                "device_longitude": placement.device_longitude,
                "raw_altitude": placement.raw_altitude,
                "site_latitude": placement.site_latitude,
                "site_longitude": placement.site_longitude,
                "height_amsl_m": placement.height_amsl_m,
                "distance_m": placement.distance_m,
            }
            for placement in geometry.tower_placed
        ],
        "tower_unmatched": list(geometry.tower_unmatched),
    }


def ar_geometry_to_json(geometry: ArGeometry, *, indent: int | None = 1) -> str:
    return json.dumps(ar_geometry_to_dict(geometry), indent=indent, ensure_ascii=False)


def terrain_to_dict(document: TerrainDocument) -> dict[str, Any]:
    """Serialize the terrain artifact (feature 031, ``contracts/terrain.schema.json``).

    The bulky, static counterpart to the polled links.json: per-link profiles keyed by
    link id and per-site heightfields + peaks keyed by site name — fetched once per viewer
    session (FR-010).
    """

    result: dict[str, Any] = {
        "generated_at": document.generated_at.isoformat(),
        "profiles": {
            link_id: {"samples_amsl": list(profile.samples_amsl)}
            for link_id, profile in sorted(document.profiles.items())
        },
        "sites": {
            name: {
                "half_extent_m": site.half_extent_m,
                "posts": site.posts,
                "rel_heights_dm": list(site.rel_heights_dm),
                "peaks": [{"name": peak.name, "enu": list(peak.enu)} for peak in site.peaks],
            }
            for name, site in sorted(document.sites.items())
        },
    }
    # 055: the optional hillshade-underlay registration; absent key ⇒ pre-055 shape (additive).
    if document.shade is not None:
        result["shade"] = {
            "generated_at": document.shade.generated_at.isoformat(),
            "width": document.shade.width,
            "height": document.shade.height,
            "step_m": document.shade.step_m,
            "sites": {
                name: list(rect) for name, rect in sorted(document.shade.sites.items())
            },
        }
    return result


def terrain_to_json(document: TerrainDocument, *, indent: int | None = None) -> str:
    # No indent by default: the grids dominate the payload and whitespace would double it.
    return json.dumps(terrain_to_dict(document), indent=indent, ensure_ascii=False)


def brc_to_dict(document: BrcDocument) -> dict[str, Any]:
    """Serialize the BRC city-grid artifact (feature 098, ``brc.json``, data-model §wire shape).

    The city ONCE in the spike frame (flat integer-decimeter polylines) plus exact per-site
    anchors for in-range sites — fetched once per viewer session (contract C2).
    """

    return {
        "generated_at": document.generated_at.isoformat(),
        "year": document.year,
        "spike": list(document.spike),
        "features": [
            {"kind": feature.kind.value, "lines": [list(line) for line in feature.lines]}
            for feature in document.features
        ],
        "sites": {
            name: {"east_m": anchor.east_m, "north_m": anchor.north_m}
            for name, anchor in sorted(document.sites.items())
        },
    }


def brc_to_json(document: BrcDocument, *, indent: int | None = None) -> str:
    # No indent by default: the polylines dominate and whitespace would double the artifact.
    return json.dumps(brc_to_dict(document), indent=indent, ensure_ascii=False)


# --- RouterOS config backup (feature 026) -----------------------------------------------------


def configs_ingest_to_dict(report: ConfigsIngestReport) -> dict[str, Any]:
    return {
        "repo_path": report.repo_path,
        "targets_matched": report.targets_matched,
        "changed": report.changed,
        "unchanged": report.unchanged,
        "failed": report.failed,
        "results": [
            {
                "identity": r.identity,
                "address": r.address,
                "outcome": r.outcome.value,
                "commit": r.commit,
                "note": r.note,
            }
            for r in report.results
        ],
        "detail": report.detail,
    }


def configs_ingest_to_json(report: ConfigsIngestReport, *, indent: int | None = 2) -> str:
    return json.dumps(configs_ingest_to_dict(report), indent=indent, ensure_ascii=False)


def config_status_to_dict(report: ConfigStatusReport) -> dict[str, Any]:
    return {
        "repo_path": report.repo_path,
        "stale_after": report.stale_after,
        "totals": report.totals,
        "rows": [
            {
                "identity": row.identity,
                "address": row.address,
                "has_backup": row.has_backup,
                "versions": row.versions,
                "last_changed_at": (
                    row.last_changed_at.isoformat() if row.last_changed_at else None
                ),
                "last_captured_at": (
                    row.last_captured_at.isoformat() if row.last_captured_at else None
                ),
                "last_commit": row.last_commit,
                "in_roster": row.in_roster,
                "stale": row.stale,
                "note": row.note,
            }
            for row in report.rows
        ],
        "detail": report.detail,
    }


def config_status_to_json(report: ConfigStatusReport, *, indent: int | None = 2) -> str:
    return json.dumps(config_status_to_dict(report), indent=indent, ensure_ascii=False)


def config_show_to_dict(result: ConfigShowResult) -> dict[str, Any]:
    return {
        "identity": result.identity,
        "at": result.at,
        "found": result.found,
        "captured_at": result.captured_at.isoformat() if result.captured_at else None,
        "router_version": result.router_version,
        "content": result.content,
        "detail": result.detail,
    }


def config_show_to_json(result: ConfigShowResult, *, indent: int | None = 2) -> str:
    return json.dumps(config_show_to_dict(result), indent=indent, ensure_ascii=False)


def config_history_to_dict(result: ConfigHistoryResult) -> dict[str, Any]:
    return {
        "identity": result.identity,
        "found": result.found,
        "truncated": result.truncated,
        "entries": [
            {
                "commit": e.commit,
                "captured_at": e.captured_at.isoformat() if e.captured_at else None,
                "summary": e.summary,
                "insertions": e.insertions,
                "deletions": e.deletions,
            }
            for e in result.entries
        ],
        "detail": result.detail,
    }


def config_history_to_json(result: ConfigHistoryResult, *, indent: int | None = 2) -> str:
    return json.dumps(config_history_to_dict(result), indent=indent, ensure_ascii=False)


def config_diff_to_dict(result: ConfigDiffResult) -> dict[str, Any]:
    return {
        "identity": result.identity,
        "from_ref": result.from_ref,
        "to_ref": result.to_ref,
        "changed": result.changed,
        "found": result.found,
        "diff": result.diff,
        "detail": result.detail,
    }


def config_diff_to_json(result: ConfigDiffResult, *, indent: int | None = 2) -> str:
    return json.dumps(config_diff_to_dict(result), indent=indent, ensure_ascii=False)


# --- metrics history/coverage (feature 021's CLI shapes, extracted by 041 so the web
# routes and the CLI share ONE builder — the parity rule made structural) -----------------


def metrics_history_to_dict(
    target: str,
    metric: str,
    since_iso: str,
    until_iso: str,
    bucket_seconds: int,
    series_list: list[Any],
) -> dict[str, Any]:
    """The `obi metrics history --json` payload (041: also served at /api/metrics.json)."""

    return {
        "target": target,
        "metric": metric,
        "since": since_iso,
        "until": until_iso,
        "bucket_seconds": bucket_seconds,
        "series": [
            {
                "ifname": s.ifname,
                "is_rate": s.is_rate,
                "unit": s.unit,
                "buckets": [
                    {"start": b.start_iso, "avg": b.avg, "min": b.min, "max": b.max, "n": b.n}
                    for b in s.buckets
                ],
            }
            for s in series_list
        ],
    }


def _us_to_iso(us: int | None) -> str | None:
    if us is None:
        return None
    return datetime.fromtimestamp(us / 1_000_000, tz=UTC).isoformat()


def metrics_coverage_to_dict(
    coverage: Any, *, db_path: Path, retain_days: int
) -> dict[str, Any]:
    """The `obi metrics status --json` payload (041: also served at /api/metrics.json)."""

    return {
        "db_path": str(db_path),
        "total_readings": coverage.total_readings,
        "oldest": _us_to_iso(coverage.oldest_us),
        "newest": _us_to_iso(coverage.newest_us),
        "retain_days": retain_days,
        "devices": [
            {"device": d.device, "readings": d.readings, "newest": _us_to_iso(d.newest_us)}
            for d in coverage.devices
        ],
    }


def metrics_devices_to_dict(
    devices: list[str], *, db_path: Path, retain_days: int
) -> dict[str, Any]:
    """The picker-shaped coverage payload served at ``/api/metrics.json`` (no ``target``).

    Deliberately NOT ``metrics_coverage_to_dict``: the counts and span that payload carries
    (``total_readings``, per-device ``readings``, ``oldest``/``newest``) each cost a full-table
    scan, took ~60s on the live store, and no browser code ever read them — MET wants the device
    names for its dropdown. ``obi metrics status`` keeps the counted view.
    """

    return {
        "db_path": str(db_path),
        "retain_days": retain_days,
        "devices": [{"device": d} for d in devices],
    }


def health_breach_to_dict(breach: Any) -> dict[str, Any]:
    """Serialize one :class:`obi.core.health.Breach` (shared by CLI --json and the route)."""

    return {
        "metric": breach.metric,
        "instance": breach.instance,
        "label": breach.label,
        "class": breach.klass,
        "value": breach.value,
        "unit": breach.unit,
        "limit": breach.limit,
    }


def health_status_to_dict(
    devices: list[Any],
    *,
    generated_at_us: int,
    db_path: Path,
    breaches_by_device: dict[str, list[Any]] | None = None,
) -> dict[str, Any]:
    """The `obi health status --json` payload (feature 096). ``devices`` are
    :class:`obi.core.health.DeviceHealth`, already worst-first; the /api/health.json route
    builds on this same shape so CLI and web can never disagree."""

    breaches_by_device = breaches_by_device or {}
    return {
        "generated_at": _us_to_iso(generated_at_us),
        "db_path": str(db_path),
        "devices": [
            {
                "device": d.device,
                "kind": d.kind,
                "stale": d.stale,
                "newest": _us_to_iso(d.newest_us),
                "breaches": [
                    health_breach_to_dict(b) for b in breaches_by_device.get(d.device, [])
                ],
                "readings": [
                    {
                        "metric": r.metric,
                        "instance": r.instance,
                        "label": r.label,
                        "value": r.value,
                        "unit": r.unit,
                        "at": _us_to_iso(r.ts_us),
                    }
                    for r in d.readings
                ],
            }
            for d in devices
        ],
    }


def router_detail_to_dict(detail: RouterDetail) -> dict[str, Any]:
    """Serialize one live router drill-down (feature 047, contracts/router-detail-api.md).

    Every value is already redacted by the engine; this is a pure shape mapping. ``device`` is
    ``None`` only for an unknown device; the two lists are empty on any non-``ok`` outcome.
    """

    device = detail.device
    return {
        "device": None
        if device is None
        else {
            "identity": device.identity,
            "address": device.address,
            "model": device.model,
            "version": device.version,
            "board_name": device.board_name,
            "uptime_seconds": device.uptime_seconds,
            "cpu_load_percent": device.cpu_load_percent,
            "free_memory_bytes": device.free_memory_bytes,
            "total_memory_bytes": device.total_memory_bytes,
        },
        "read": {
            "outcome": detail.outcome.value,
            "detail": detail.detail,
            "read_at": detail.read_at.isoformat(),
        },
        "interfaces": [
            {
                "name": iface.name,
                "type": iface.type,
                "running": iface.running,
                "disabled": iface.disabled,
                "mac": iface.mac,
                "comment": iface.comment,
                "mtu": iface.mtu,
            }
            for iface in detail.interfaces
        ],
        "addresses": [
            {
                "address": addr.address,
                "network": addr.network,
                "interface": addr.interface,
                "dynamic": addr.dynamic,
                "disabled": addr.disabled,
                "comment": addr.comment,
            }
            for addr in detail.addresses
        ],
        "truncated": detail.truncated,
    }


# --- feature 075: the PHY console payload (contracts/phy-api.md is the JSON contract) -----------


def _phy_transceiver_to_dict(transceiver: PortTransceiver | None) -> dict[str, Any] | None:
    if transceiver is None:
        return None
    return {
        "present": transceiver.present,
        "vendor": transceiver.vendor,
        "part": transceiver.part,
        "max_rate_mbps": transceiver.max_rate_mbps,
        "wavelength_nm": transceiver.wavelength_nm,
        "temperature_c": transceiver.temperature_c,
        "tx_power_dbm": transceiver.tx_power_dbm,
        "rx_power_dbm": transceiver.rx_power_dbm,
    }


def _phy_poe_to_dict(poe: PortPoe | None) -> dict[str, Any] | None:
    if poe is None:
        return None
    return {
        "out": poe.out,
        "status": poe.status,
        "voltage_v": poe.voltage_v,
        "current_ma": poe.current_ma,
        "power_w": poe.power_w,
    }


def _phy_l2_to_dict(l2: PortL2 | None) -> dict[str, Any] | None:
    if l2 is None:
        return None
    return {
        "bridge": l2.bridge,
        "pvid": l2.pvid,
        "tagged_vlans": list(l2.tagged_vlans),
        "untagged_vlans": list(l2.untagged_vlans),
        "stp_role": l2.stp_role,
        "stp_state": l2.stp_state,
    }


def _phy_neighbor_to_dict(neighbor: PortNeighbor | None) -> dict[str, Any] | None:
    if neighbor is None:
        return None
    return {
        "identity": neighbor.identity,
        "address": neighbor.address,
        "mac": neighbor.mac,
        "platform": neighbor.platform,
    }


def _phy_port_to_dict(port: RouterPort) -> dict[str, Any]:
    return {
        "name": port.name,
        "default_name": port.default_name,
        "mac": port.mac,
        "disabled": port.disabled,
        "link_up": port.link_up,
        "rate_mbps": port.rate_mbps,
        "full_duplex": port.full_duplex,
        "auto_negotiation": port.auto_negotiation,
        "transceiver": _phy_transceiver_to_dict(port.transceiver),
        "poe": _phy_poe_to_dict(port.poe),
        "l2": _phy_l2_to_dict(port.l2),
        "neighbor": _phy_neighbor_to_dict(port.neighbor),
    }


def router_phy_to_dict(phy: RouterPhy) -> dict[str, Any]:
    """Serialize one live PHY drill-down (feature 075, contracts/phy-api.md).

    Pure shape mapping — every value is already redacted by the engine. The handler adds the
    envelope keys (``store``, ``hint``) and the independent ``metrics`` section; a sub-object is
    ``null`` only when structurally inapplicable (copper port / no PoE / not bridged / no peer).
    """

    device = phy.device
    return {
        "device": None
        if device is None
        else {
            "identity": device.identity,
            "address": device.address,
            "model": device.model,
            "serial": device.serial,
            "version": device.version,
            "current_firmware": device.current_firmware,
            "upgrade_firmware": device.upgrade_firmware,
            "firmware_behind": device.firmware_behind,
        },
        "read": {
            "outcome": phy.outcome.value,
            "detail": phy.detail,
            "read_at": phy.read_at.isoformat(),
        },
        "ports": [_phy_port_to_dict(port) for port in phy.ports],
        "sections": dict(phy.sections),
        "truncated": phy.truncated,
    }


# --- feature 052: OSPF topology snapshot serializer (data-model.md is the JSON contract) -------


def _ospf_instance_to_dict(instance: OspfInstanceInfo) -> dict[str, Any]:
    return {
        "name": instance.name,
        "version": instance.version,
        "router_id": instance.router_id,
        "redistribute": instance.redistribute,
        "originate_default": instance.originate_default,
        "out_filter_chain": instance.out_filter_chain,
        "in_filter_chain": instance.in_filter_chain,
    }


def _ospf_default_origination_to_dict(item: OspfDefaultOrigination) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "router_id": item.router_id,
        "identity": item.identity,
        "metric_type": item.metric_type,
        "metric": item.metric,
        "note": item.note,
    }
    if item.takeover_condition is not None:
        payload["takeover_condition"] = item.takeover_condition
    return payload


def _ospf_egress_to_dict(choice: OspfEgressChoice) -> dict[str, Any]:
    """One per-root default egress choice (073, §default_by_root)."""
    return {
        "router_id": choice.router_id,
        "identity": choice.identity,
        "metric_type": choice.metric_type,
        "metric": choice.metric,
        "path_cost": choice.path_cost,
        "total_cost": choice.total_cost,
    }


def ospf_topology_to_dict(topology: OspfTopology) -> dict[str, Any]:
    """Render the analysed :class:`OspfTopology` as the ``ospf`` snapshot ``data`` (decision D7).

    Pure and deterministic (no wall-clock — the envelope stamps ``generated_at``); every shape
    is the data-model.md contract. The ``class`` key is spelled out here (Python reserves it)."""

    meta = topology.meta
    return {
        "meta": {
            "source_router": meta.source_router,
            "source_fallback_used": meta.source_fallback_used,
            "area_ids": list(meta.area_ids),
            "routers_polled": meta.routers_polled,
            "routers_failed": list(meta.routers_failed),
            "non_ospf_devices": list(meta.non_ospf_devices),
            # 086: blessed-layout provenance (contract ospf-layout.md §2/§4).
            "layout_blessed": meta.layout_blessed,
            "layout_ignored": meta.layout_ignored,
            "layout_error": meta.layout_error,
        },
        # 086: the blessed internet-cloud default position (null = the widget computes it).
        "cloud_seed": (
            {"x": topology.cloud_seed[0], "y": topology.cloud_seed[1]}
            if topology.cloud_seed is not None
            else None
        ),
        "nodes": [
            {
                "router_id": node.router_id,
                "identity": node.identity,
                "address": node.address,
                "site": node.site,
                "seed": {"x": node.seed[0], "y": node.seed[1]},
                "areas": list(node.areas),
                "instance": _ospf_instance_to_dict(node.instance) if node.instance else None,
                "enrichment": node.enrichment,
                "is_asbr": node.is_asbr,
                "spof": node.spof,
                "lint_count": node.lint_count,
            }
            for node in topology.nodes
        ],
        "edges": [
            {
                "id": edge.id,
                "a": edge.a,
                "b": edge.b,
                "cost_ab": edge.cost_ab,
                "cost_ba": edge.cost_ba,
                "symmetric": edge.symmetric,
                "area": edge.area,
                "iface_a": edge.iface_a,
                "iface_b": edge.iface_b,
                "network_type": edge.network_type,
                "neighbor_state": edge.neighbor_state,
                "adjacency_age": edge.adjacency_age,
                "state_changes": edge.state_changes,
                "shared_fate_group": edge.shared_fate_group,
                "bridge": edge.bridge,
                "tier": edge.tier,
            }
            for edge in topology.edges
        ],
        "ghosts": [
            {
                "id": ghost.id,
                "router_id": ghost.router_id,
                "interface": ghost.interface,
                "cost": ghost.cost,
                "class": ghost.ghost_class,
                "peer_router_id": ghost.peer_router_id,
                "detail": ghost.detail,
            }
            for ghost in topology.ghosts
        ],
        "pseudonodes": [
            {
                "id": pseudo.id,
                "dr": pseudo.dr,
                "attached": list(pseudo.attached),
                "area": pseudo.area,
            }
            for pseudo in topology.pseudonodes
        ],
        "externals": [
            {
                "prefix": external.prefix,
                "router_id": external.router_id,
                "metric_type": external.metric_type,
                "metric": external.metric,
                "forwarding_address": external.forwarding_address,
            }
            for external in topology.externals
        ],
        "default_route": (
            {
                "active": (
                    _ospf_default_origination_to_dict(topology.default_route.active)
                    if topology.default_route.active
                    else None
                ),
                "standby": [
                    _ospf_default_origination_to_dict(item)
                    for item in topology.default_route.standby
                ],
                "conflict": topology.default_route.conflict,
                "narrative": topology.default_route.narrative,
            }
            if topology.default_route is not None
            else None
        ),
        "default_by_root": {
            root: {
                "active": (
                    _ospf_egress_to_dict(per_root.active) if per_root.active else None
                ),
                "standby": [_ospf_egress_to_dict(item) for item in per_root.standby],
            }
            for root, per_root in topology.default_by_root.items()
        },
        "shared_fate_groups": [
            {
                "id": group.id,
                "router_id": group.router_id,
                "parent_port": group.parent_port,
                "edge_ids": list(group.edge_ids),
            }
            for group in topology.shared_fate_groups
        ],
        "spf": {root: dict(parents) for root, parents in topology.spf.items()},
        "lint": [
            {
                "id": finding.id,
                "category": finding.category,
                "severity": finding.severity,
                "router_id": finding.router_id,
                "edge_id": finding.edge_id,
                "ghost_id": finding.ghost_id,
                "message": finding.message,
            }
            for finding in topology.lint
        ],
    }

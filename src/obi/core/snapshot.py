"""Per-tool read-only snapshots for the web boards (feature 040).

The camp-2 idiom (specs/040-web-tool-boards/contracts/snapshots-and-routes.md): device/
controller-touching tools reach the browser through cron-written JSON files — the
``links.json`` pattern — so the web server stays device-blind. Each builder here calls the
tool's EXISTING engine function and CLI serializer (the parity rule: a board can never
disagree with ``obi <tool> --json``) and wraps the dict with the tool name and a UTC
generation timestamp.

Builders take PRE-BUILT sources/drivers (the engine-function convention) so tests inject
the recorded fakes; ``obi web snapshot`` is the only place real controllers are
constructed. A builder raises :class:`SnapshotError` when the tool's source is down as a
whole (controller unreachable) — the caller then keeps the previous snapshot file, so one
dead controller never blanks a healthy board. Per-device unreachability is NOT a failure:
a fleet report with half the fleet timed out is data (the 030 lesson), and it snapshots.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

    from obi.core.audit import AuditTrail
    from obi.core.config import Config, SikluSettings
    from obi.core.drivers.base import DocumentationSource, InventorySource, VendorDriver
    from obi.core.drivers.siklu import SikluDriver
    from obi.core.drivers.uisp import UispController
    from obi.core.inventory import Inventory

# The camp-2 catalog. server.py's /snapshots/ allowlist is parity-tested against this.
SNAPSHOT_TOOL_NAMES = ("status", "uisp", "uisp-links", "siklu", "unifi", "netbox-drift", "ospf")


class SnapshotError(Exception):
    """The tool's source failed as a whole — keep the previous snapshot, report the run."""


def _wrap(tool: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"tool": tool, "generated_at": datetime.now(UTC).isoformat(), "data": data}


def write_snapshot(directory: Path, payload: dict[str, Any]) -> Path:
    """Atomically write one wrapped snapshot (temp + ``os.replace`` — readers never see
    partials; the ``ar_export``/``inventory.save`` idiom)."""

    directory.mkdir(parents=True, exist_ok=True)
    out = directory / f"{payload['tool']}.json"
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, out)
    return out


# --- builders (one per SNAPSHOT_TOOL_NAMES entry; sources are pre-built by the caller) ---------


def build_status(
    inventory: Inventory,
    driver: VendorDriver,
    *,
    audit: AuditTrail,
    timeout: float,
    concurrency: int,
    reason: str,
) -> dict[str, Any]:
    from obi.core.engine import status_from_inventory
    from obi.core.serialize import status_report_to_dict

    report = status_from_inventory(
        inventory, driver, audit=audit, timeout=timeout, concurrency=concurrency, reason=reason
    )
    return _wrap("status", status_report_to_dict(inventory, report))


def build_uisp(
    source: InventorySource,
    *,
    audit: AuditTrail,
    controller: str,
    name_filter: str | None,
    ignore_names: tuple[str, ...] | list[str],
    timeout: float,
    reason: str,
) -> dict[str, Any]:
    from obi.core.engine import uisp_status
    from obi.core.models import SourceOutcome
    from obi.core.serialize import uisp_report_to_dict

    report = uisp_status(
        source,
        audit=audit,
        controller=controller,
        name_filter=name_filter,
        ignore_names=ignore_names,
        timeout=timeout,
        reason=reason,
    )
    if report.outcome is not SourceOutcome.REACHABLE:
        detail = f" ({report.detail})" if report.detail else ""
        raise SnapshotError(f"uisp controller {report.outcome.value}{detail}")
    return _wrap("uisp", uisp_report_to_dict(report))


def build_uisp_links(
    source: UispController,
    *,
    audit: AuditTrail,
    controller: str,
    name_filter: str | None,
    ignore_names: tuple[str, ...] | list[str],
    timeout: float,
    reason: str,
) -> dict[str, Any]:
    from obi.core.engine import uisp_links
    from obi.core.models import SourceOutcome
    from obi.core.serialize import uisp_links_report_to_dict

    report = uisp_links(
        source,
        audit=audit,
        controller=controller,
        name_filter=name_filter,
        ignore_names=ignore_names,
        timeout=timeout,
        reason=reason,
    )
    if report.outcome is not SourceOutcome.REACHABLE:
        detail = f" ({report.detail})" if report.detail else ""
        raise SnapshotError(f"uisp controller {report.outcome.value}{detail}")
    return _wrap("uisp-links", uisp_links_report_to_dict(report))


def build_siklu(
    driver: SikluDriver,
    settings: SikluSettings,
    *,
    audit: AuditTrail,
    reason: str,
) -> dict[str, Any]:
    from obi.core.engine import siklu_links, siklu_status
    from obi.core.serialize import siklu_report_to_dict

    report = siklu_status(driver, settings, audit=audit, reason=reason)
    report.links = siklu_links(report, overrides=settings.pairs)
    return _wrap("siklu", siklu_report_to_dict(report))


def build_unifi(
    source: InventorySource,
    *,
    audit: AuditTrail,
    console: str,
    name_filter: str | None,
    timeout: float,
    reason: str,
) -> dict[str, Any]:
    from obi.core.engine import unifi_status
    from obi.core.models import SourceOutcome
    from obi.core.serialize import unifi_report_to_dict

    report = unifi_status(
        source,
        audit=audit,
        console=console,
        action="unifi_status",
        target=f"unifi-network:{console}",
        name_filter=name_filter,
        timeout=timeout,
        reason=reason,
    )
    if report.outcome is not SourceOutcome.REACHABLE:
        detail = f" ({report.detail})" if report.detail else ""
        raise SnapshotError(f"unifi console {report.outcome.value}{detail}")
    return _wrap("unifi", unifi_report_to_dict(report))


def build_ospf(
    inventory: Inventory,
    driver: VendorDriver,
    *,
    audit: AuditTrail,
    timeout: float,
    reason: str,
    site_coords: dict[str, tuple[float, float]] | None = None,
    layout_path: Path | None = None,
    preferred_sources: tuple[str, ...] = (),
) -> dict[str, Any]:
    """The ``ospf`` topology snapshot (feature 052; read-only fleet OSPF collection).

    Wraps ``engine.ospf_topology`` + ``serialize.ospf_topology_to_dict`` (the ``build_status``
    shape, decision D7). A total LSDB-source failure (both cores down) raises
    :class:`SnapshotError` so the caller keeps the previous ``ospf.json``; a per-router enrichment
    failure is data, not failure (it snapshots with that router marked stale).

    ``layout_path`` (086) is the operator's blessed-layout file — read HERE (the IO boundary,
    contract ospf-layout.md §3) and overlaid on the computed seeds downstream. A missing,
    unreadable, or malformed file MUST NOT fail the snapshot: it builds on computed positions
    with the problem recorded in ``meta.layout_error``."""

    from obi.core.engine import _OspfSourceError, ospf_topology
    from obi.core.ospf import parse_layout_overrides
    from obi.core.serialize import ospf_topology_to_dict

    layout_overrides: dict[str, tuple[float, float]] = {}
    layout_cloud: tuple[float, float] | None = None
    layout_errors: list[str] = []
    if layout_path is not None:
        try:
            raw = layout_path.read_text(encoding="utf-8")
        except OSError as exc:
            layout_errors = [f"layout file {layout_path}: {exc}"]
        else:
            layout_overrides, layout_cloud, layout_errors = parse_layout_overrides(raw)

    # The OSPF sweep is RouterOS-specific (Principle IV); the CLI passes the MikrotikDriver.
    try:
        topology = ospf_topology(
            inventory,
            driver,  # type: ignore[arg-type]
            audit=audit,
            timeout=timeout,
            reason=reason,
            site_coords=site_coords,
            preferred_sources=preferred_sources,
            layout_overrides=layout_overrides,
            layout_cloud=layout_cloud,
            layout_errors=layout_errors,
        )
    except _OspfSourceError as exc:
        raise SnapshotError(str(exc)) from exc
    return _wrap("ospf", ospf_topology_to_dict(topology))


def build_netbox_drift(
    source: DocumentationSource,
    *,
    audit: AuditTrail,
    source_host: str,
    inventory_path: Path,
    name_filter: str | None,
    timeout: float,
    reason: str,
) -> dict[str, Any]:
    from obi.core.engine import netbox_reconcile
    from obi.core.models import SourceOutcome
    from obi.core.serialize import drift_report_to_dict

    report = netbox_reconcile(
        source,
        audit=audit,
        source_host=source_host,
        inventory_path=inventory_path,
        name_filter=name_filter,
        timeout=timeout,
        reason=reason,
    )
    if report.source_outcome is not SourceOutcome.REACHABLE:
        detail = f" — {report.detail}" if report.detail else ""
        raise SnapshotError(f"netbox {report.source_outcome.value}{detail}")
    return _wrap("netbox-drift", drift_report_to_dict(report))


# --- the run-all composition (feature 040; lifted from the CLI by 066) --------------------------


def run_all(
    config: Config,
    *,
    wanted: Sequence[str],
    audit: AuditTrail,
    out_dir: Path,
    reason: str,
) -> dict[str, str]:
    """Build the requested web-board snapshots into ``out_dir``; return ``{tool: outcome}``.

    The composition seam behind ``obi web snapshot`` (feature 040; lifted out of the CLI by 066 so a
    cron or another surface can reuse it). Wires each tool's driver from ``config`` and writes one
    atomic JSON per built tool. A tool builds iff its config section exists AND it is in ``wanted``;
    an unconfigured tool is ``"skipped (unconfigured)"``. A tool's failure — source down, or any
    crash — is captured as its outcome string and never raised (FR-003), so one dead controller
    never aborts the others. The caller opens the audit scope, records the run-level summary, and
    renders ``outcomes``. Driver imports stay lazy (this module's stdlib-only-top discipline)."""

    from collections.abc import Callable

    from obi.core import inventory as inv
    from obi.core.drivers.mikrotik import MikrotikDriver

    outcomes: dict[str, str] = {}

    def attempt(name: str, available: bool, build: Callable[[], dict[str, object]]) -> None:
        if name not in wanted:
            return
        if not available:
            outcomes[name] = "skipped (unconfigured)"
            return
        try:
            write_snapshot(out_dir, build())
            outcomes[name] = "built"
        except SnapshotError as exc:
            outcomes[name] = f"failed: {exc}"
        except Exception as exc:  # a tool's crash must not abort the run (FR-003)
            outcomes[name] = f"failed: {exc}"

    roster = inv.load(config.inventory_path) if config.routeros is not None else None

    def _status() -> dict[str, object]:
        assert config.routeros is not None and roster is not None  # gated by attempt()
        return build_status(
            roster,
            MikrotikDriver(config.routeros),
            audit=audit,
            timeout=config.timeout,
            concurrency=config.concurrency,
            reason=reason,
        )

    def _uisp() -> dict[str, object]:
        from obi.core.drivers.uisp import UispController

        assert config.uisp is not None  # gated by attempt()
        return build_uisp(
            UispController(config.uisp),
            audit=audit,
            controller=config.uisp.base_url,
            name_filter=config.uisp.name_filter,
            ignore_names=config.uisp.ignore_names,
            timeout=config.uisp.timeout,
            reason=reason,
        )

    def _uisp_links() -> dict[str, object]:
        from obi.core.drivers.uisp import UispController

        assert config.uisp is not None  # gated by attempt()
        return build_uisp_links(
            UispController(config.uisp),
            audit=audit,
            controller=config.uisp.base_url,
            name_filter=config.uisp.name_filter,
            ignore_names=config.uisp.ignore_names,
            timeout=config.uisp.timeout,
            reason=reason,
        )

    def _siklu() -> dict[str, object]:
        from obi.core.drivers.siklu import SikluDriver

        assert config.siklu is not None  # gated by attempt()
        return build_siklu(SikluDriver(config.siklu), config.siklu, audit=audit, reason=reason)

    def _unifi() -> dict[str, object]:
        from obi.core.drivers.unifi_network import UnifiNetworkController

        assert config.unifi_network is not None  # gated by attempt()
        return build_unifi(
            UnifiNetworkController(config.unifi_network),
            audit=audit,
            console=config.unifi_network.base_url,
            name_filter=config.unifi_network.name_filter,
            timeout=config.unifi_network.timeout,
            reason=reason,
        )

    def _ospf() -> dict[str, object]:
        assert config.routeros is not None and roster is not None  # gated by attempt()
        return build_ospf(
            roster,
            MikrotikDriver(config.routeros),
            audit=audit,
            timeout=config.timeout,
            reason=reason,
            layout_path=config.web.ospf_layout_path,  # 086: blessed layout (None = off)
            preferred_sources=tuple(config.web.ospf_sources),  # 137: [web] ospf_sources
        )

    def _netbox_drift() -> dict[str, object]:
        from obi.core.drivers.netbox import NetboxSource

        assert config.netbox is not None  # gated by attempt()
        return build_netbox_drift(
            NetboxSource(config.netbox),
            audit=audit,
            source_host=config.netbox.base_url,
            inventory_path=config.inventory_path,
            name_filter=config.netbox.name_filter,
            timeout=config.netbox.timeout,
            reason=reason,
        )

    attempt("status", config.routeros is not None and roster is not None, _status)
    attempt("uisp", config.uisp is not None, _uisp)
    attempt("uisp-links", config.uisp is not None, _uisp_links)
    attempt("siklu", config.siklu is not None, _siklu)
    attempt("unifi", config.unifi_network is not None, _unifi)
    attempt("netbox-drift", config.netbox is not None, _netbox_drift)
    attempt("ospf", config.routeros is not None and roster is not None, _ospf)
    return outcomes

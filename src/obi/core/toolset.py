"""The chat agent's read-only tools (feature 005, contracts/chat-tools.md).

Each tool wraps an existing read-only core capability, audits the call (with the operator's
question as intent), and returns a compact, model-friendly rendering. Handlers never raise to
the agent loop — a failure becomes a returned error string. The agent is wired with EXACTLY
this set; there is no state-changing tool in v1, so read-only is guaranteed by construction
(Constitution Principle I). When a mutating tool is added later it MUST be routed through a
confirmation gate before execution.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from obi.core import inventory as inv
from obi.core.audit import AuditTrail, record_event
from obi.core.config import (
    Config,
    ConfigBackupSettings,
    LogsSettings,
    MeshtasticSettings,
    WledSettings,
)
from obi.core.configstore import redact_records
from obi.core.drivers.base import (
    DocumentationSource,
    InventorySource,
    LogSource,
    MetricsHistorySource,
    MetricsSource,
    TimeseriesQuerySource,
    TsSeries,
    VendorDriver,
)
from obi.core.drivers.mikrotik import (
    MikrotikDriver,
    ReadOnlyPathError,
    assert_read_path,
    normalize_segments,
)
from obi.core.mesh import MeshLocator
from obi.core.playa import PlayaLocator

if TYPE_CHECKING:
    from obi.core.drivers.siklu import SikluDriver
from obi.core.inventory import Inventory, InventoryEntry
from obi.core.models import (
    AuditOutcome,
    AuditRecord,
    DiscoveryState,
    LogEvent,
    MetricSample,
    MetricsHistoryReport,
    Reachability,
    SeriesSummary,
    SnmpOutcome,
    SourceOutcome,
    _utcnow,
)

READ_ONLY = "read_only"
# feature 103: the decorative-lighting write class (constitution 1.15.0) — the first and only
# non-read-only risk class in use. Statically assigned to the wled write tools; never computed.
LOW_RISK = "low"
# feature 130: the Slack-DM member paging relay (constitution 3.0.0) — paging humans is not
# revertible, so it is medium; the requester's explicit DM ask is the confirming human. The
# tool carrying it lives in obi/paging/chat_tool.py and appears in NO builder in this module.
MEDIUM_RISK = "medium"

_NO_INVENTORY = "No inventory exists yet — run `obi routeros discover` first to build one."
_NO_MIKROTIK = "Live RouterOS access needs a [routeros] section; none is configured."
_NO_WLED = "No lighting device is configured — add a [wled.devices] section to obi's config."

ToolHandler = Callable[["ToolContext", dict[str, Any]], str]


@dataclass
class LiveCallBudget:
    """A per-run cap on live device-contacting calls (feature 028).

    The autonomous watcher builds one from ``[watch].investigation_live_call_budget`` and puts it on
    the :class:`ToolContext`; each live handler ``consume()``s **before** contacting a device. Chat/
    voice leave ``live_budget=None`` (only the per-call ``[routeros.live]`` bounds apply).
    """

    remaining: int

    def consume(self) -> bool:
        """Reserve one live call; ``True`` if allowed (and decrement), ``False`` when exhausted."""

        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


@dataclass
class ConfigBackupSource:
    """Presence marker + settings for the config-backup git store (feature 026).

    Present on the :class:`ToolContext` only when ``[configs]`` is configured; the ``config_query``
    handler gates on it, so chat on a host without config backup offers no config tool and never
    dereferences a ``None``. The read itself goes through the engine's ``config_*`` functions (which
    write the intent-only audit records), so this holds just the settings.
    """

    settings: ConfigBackupSettings


@dataclass
class WledSource:
    """Presence marker + settings for the ``[wled]`` lighting allowlist (feature 103).

    Present on the :class:`ToolContext` only when ``[wled].devices`` is non-empty; the three
    wled handlers gate on it, so an unconfigured host cleanly answers "no lighting device
    configured" and never dereferences a ``None``. The settings' ``devices`` map is the
    COMPLETE write allowlist — the engine (:mod:`obi.core.lighting`) resolves every target
    through it and refuses anything else before network contact.
    """

    settings: WledSettings


@dataclass
class MeshSource:
    """Presence marker + settings for the ``[meshtastic]`` mesh-locate opt-in (feature 111).

    Present on the :class:`ToolContext` only when a ``[meshtastic]`` section exists; the
    mesh_locate handler gates on it, so an unconfigured host cleanly answers "not
    configured" and never dereferences a ``None``. The section's presence is deliberate
    opt-in: the tool can transmit a position request on shared LoRa airtime, and a
    transmitting capability is operator-enabled, never ambient.
    """

    settings: MeshtasticSettings


@dataclass
class ToolContext:
    """Shared dependencies plus the current operator question (audited as intent).

    ``driver`` (Mikrotik), ``uisp_source`` (UISP controller), ``netbox_source`` (NetBox documented
    inventory), and ``log_source`` (ingested device syslog) are each present only when that source
    is configured; at least one is non-``None`` (chat supports any source combination — features
    006/010/013). The Mikrotik handlers gate on the persisted inventory before touching ``driver``,
    and the UISP/NetBox/logs handlers gate on their source, so a ``None`` accessor (e.g. a
    logs-only host) is never dereferenced.
    """

    config: Config
    driver: VendorDriver | None
    audit: AuditTrail
    uisp_source: InventorySource | None = None
    netbox_source: DocumentationSource | None = None
    unifi_network_source: InventorySource | None = None
    # feature 012: the read-only SNMP metrics source, present only when [snmp] is configured. The
    # snmp_metrics handler gates on it, so a None source (no [snmp]) is never dereferenced.
    metrics_source: MetricsSource | None = None
    # feature 013: the read-only device-syslog source, present only when [logs] is configured.
    log_source: LogSource | None = None
    # feature 090: the read-only STORED time-series source, present only when [snmp.history] is
    # configured. Distinct from ``metrics_source`` above: that one polls a device over SNMP right
    # now and returns raw counters; this one reads what the 021 collector already stored and returns
    # rates, contacting nothing. The metrics_history handler gates on it, so a None is never used.
    metrics_history_source: MetricsHistorySource | None = None
    # feature 020: the read-only Siklu EtherHaul driver, present only when [siklu] hosts are
    # configured. The siklu_status handler gates on it, so a None driver (no [siklu]) is never used.
    siklu_driver: SikluDriver | None = None
    # feature 026: the read-only RouterOS config-backup store, present only when [configs] is
    # configured. The config_query handler gates on it, so a None source is never dereferenced.
    config_source: ConfigBackupSource | None = None
    question: str = ""
    # feature 009: the requesting operator's Slack user ID when this context serves a Slack
    # request, so every audited tool call is attributed to them. None for the CLI chat path.
    actor: str | None = None
    # feature 028: a per-run budget on live device calls (routeros_query/ping/traceroute). Set by
    # the watcher; None for chat/voice (unthrottled per turn — only per-call bounds apply).
    live_budget: LiveCallBudget | None = None
    # feature 103: the [wled] lighting allowlist, present only when configured. Powers the
    # read-only wled_status (registry) and — on the operator toolset ONLY — the write tools.
    wled_source: WledSource | None = None
    # feature 108: the external VictoriaMetrics store, present only when [victoria] is configured.
    # A SECOND-TIER remote source (obi's own stores stay primary) — the four store handlers
    # gate on it, and because it is a live remote HTTP call, those tools ride the 028 budget
    # on the watcher (BUDGETED_LIVE_TOOL_NAMES), unlike the local-store 090/100 tools.
    victoria_source: TimeseriesQuerySource | None = None
    # feature 111: the [meshtastic] mesh-locate opt-in, present only when configured. The
    # mesh_locate handler gates on it; a None (no [meshtastic]) is never dereferenced.
    mesh_source: MeshSource | None = None


@dataclass
class ToolDefinition:
    """A read-only tool: its model-facing schema and the handler that runs it."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    risk_class: str = READ_ONLY
    mutating: bool = False
    #: Config sections this tool needs (feature 137). When any is unconfigured the tool is
    #: hidden from every surface by :func:`available_tools` — the filter only ever removes.
    requires: tuple[str, ...] = ()

    def schema(self) -> dict[str, Any]:
        """The Messages-API tool schema (name + description + input_schema)."""

        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


def _audit(
    ctx: ToolContext,
    *,
    action: str,
    target: str,
    outcome: AuditOutcome,
    params: dict[str, object] | None = None,
    detail: str | None = None,
) -> None:
    ctx.audit.record(
        AuditRecord(
            timestamp=_utcnow(),
            action=action,
            target=target,
            reason=ctx.question or "chat query",
            outcome=outcome,
            params=params or {},
            detail=detail,
            actor=ctx.actor,
        )
    )


def _load(ctx: ToolContext) -> Inventory | None:
    return inv.load(ctx.config.inventory_path)


def _find_entry(inventory: Inventory, target: str) -> InventoryEntry | None:
    needle = target.strip().lower()
    for entry in inventory.entries:
        if entry.address.lower() == needle or entry.identity.lower() == needle:
            return entry
    return None


def _fmt_uptime(seconds: int | None) -> str:
    if seconds is None:
        return "?"
    parts: list[str] = []
    remaining = seconds
    for label, size in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if remaining >= size:
            value, remaining = divmod(remaining, size)
            parts.append(f"{value}{label}")
        if len(parts) == 2:
            break
    return "".join(parts) if parts else "0s"


def _fmt_bytes(num: int | None) -> str:
    if num is None:
        return "?"
    size = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def _fmt_pct(value: float | None) -> str:
    return "?" if value is None else f"{value:g}%"


# --- handlers ---------------------------------------------------------------------------------


def _inventory_list(ctx: ToolContext, args: dict[str, Any]) -> str:
    inventory = _load(ctx)
    if inventory is None:
        _audit(
            ctx,
            action="inventory_list",
            target="inventory",
            outcome=AuditOutcome.ERROR,
            detail="no inventory",
        )
        return _NO_INVENTORY

    state = str(args.get("discovery_state", "any")).lower()
    identity_contains = str(args.get("identity_contains", "")).lower()
    present_only = bool(args.get("present_only", True))

    rows: list[InventoryEntry] = []
    for entry in inventory.entries:
        if present_only and not entry.present_in_latest:
            continue
        if state not in ("any", "") and entry.discovery_state.value != state:
            continue
        haystack = f"{entry.identity} {entry.address}".lower()
        if identity_contains and identity_contains not in haystack:
            continue
        rows.append(entry)

    _audit(
        ctx,
        action="inventory_list",
        target="inventory",
        outcome=AuditOutcome.OK,
        params={"matched": len(rows), "filter_state": state},
    )

    refreshed = inventory.generated_at.isoformat()
    if not rows:
        return f"No devices match (inventory last refreshed {refreshed})."
    lines = [f"Inventory (last refreshed {refreshed}; {len(rows)} match):"]
    for entry in rows:
        absent = "" if entry.present_in_latest else " [absent in latest crawl]"
        lines.append(
            f"- {entry.identity or '(no identity)'} @ {entry.address} — "
            f"state={entry.discovery_state.value}, model={entry.model or '?'}, "
            f"RouterOS={entry.version or '?'}, via={entry.provenance}{absent}"
        )
    return "\n".join(lines)


def _inventory_summary(ctx: ToolContext, args: dict[str, Any]) -> str:
    inventory = _load(ctx)
    if inventory is None:
        _audit(
            ctx,
            action="inventory_summary",
            target="inventory",
            outcome=AuditOutcome.ERROR,
            detail="no inventory",
        )
        return _NO_INVENTORY

    entries = inventory.entries
    by_state: dict[str, int] = {}
    for entry in entries:
        by_state[entry.discovery_state.value] = by_state.get(entry.discovery_state.value, 0) + 1
    present = sum(1 for entry in entries if entry.present_in_latest)
    absent = len(entries) - present

    _audit(
        ctx,
        action="inventory_summary",
        target="inventory",
        outcome=AuditOutcome.OK,
        params={"total": len(entries)},
    )

    state_summary = ", ".join(f"{k}={v}" for k, v in sorted(by_state.items())) or "none"
    return (
        f"Network summary (inventory last refreshed {inventory.generated_at.isoformat()}):\n"
        f"- {len(entries)} device(s) total; {present} present, {absent} absent in latest crawl\n"
        f"- by state: {state_summary}"
    )


def _routeros_neighbors(ctx: ToolContext, args: dict[str, Any]) -> str:
    target = str(args.get("target", "")).strip()
    if not target:
        return "error: 'target' (a device address or identity) is required."
    inventory = _load(ctx)
    if inventory is None:
        return _NO_INVENTORY
    entry = _find_entry(inventory, target)
    if entry is None:
        _audit(
            ctx,
            action="routeros_neighbors",
            target=target,
            outcome=AuditOutcome.ERROR,
            detail="unknown device",
        )
        return f"No device named '{target}' is in the inventory."
    if entry.discovery_state is not DiscoveryState.IDENTIFIED:
        return (
            f"'{entry.identity or entry.address}' is not an identified Mikrotik device "
            f"(state={entry.discovery_state.value}); its neighbors cannot be read."
        )

    if ctx.driver is None:
        return "Neighbor/topology reads are Mikrotik-only; no [routeros] section is configured."
    neighbors = ctx.driver.fetch_neighbors(entry.to_device(), timeout=ctx.config.timeout)
    _audit(
        ctx,
        action="routeros_neighbors",
        target=entry.address,
        outcome=AuditOutcome.OK,
        params={"identity": entry.identity, "neighbors": len(neighbors)},
    )
    if not neighbors:
        return f"{entry.identity or entry.address} advertises no neighbors (or none were readable)."

    lines = [f"Neighbors of {entry.identity or entry.address} ({len(neighbors)}):"]
    for neighbor in neighbors:
        is_mikrotik = bool(neighbor.platform and "mikrotik" in neighbor.platform.lower())
        scope = "" if is_mikrotik else "  [non-Mikrotik — out of scope for now]"
        lines.append(
            f"- {neighbor.identity or '(no identity)'} @ {neighbor.address or '(no IP)'} "
            f"via {neighbor.interface or '?'} — platform={neighbor.platform or '?'}{scope}"
        )
    return "\n".join(lines)


def _routeros_status(ctx: ToolContext, args: dict[str, Any]) -> str:
    from obi.core.engine import status_from_inventory

    target = str(args.get("target", "")).strip()
    if not target:
        return "error: 'target' (a device address/identity, or 'all') is required."
    inventory = _load(ctx)
    if inventory is None:
        return _NO_INVENTORY
    if ctx.driver is None:
        return "Live Mikrotik reads need a [routeros] section; none is configured."

    if target.lower() == "all":
        subject = inventory
    else:
        entry = _find_entry(inventory, target)
        if entry is None:
            _audit(
                ctx,
                action="routeros_status",
                target=target,
                outcome=AuditOutcome.ERROR,
                detail="unknown device",
            )
            return f"No device named '{target}' is in the inventory."
        if entry.discovery_state is not DiscoveryState.IDENTIFIED:
            return (
                f"'{entry.identity or entry.address}' is not an identified Mikrotik device "
                f"(state={entry.discovery_state.value}); there is no live health to read."
            )
        subject = Inventory(
            scope=inventory.scope, generated_at=inventory.generated_at, entries=[entry]
        )

    report = status_from_inventory(
        subject,
        ctx.driver,
        audit=ctx.audit,
        timeout=ctx.config.timeout,
        concurrency=ctx.config.concurrency,
        reason=ctx.question or "chat live status",
    )
    _audit(
        ctx,
        action="routeros_status",
        target=target,
        outcome=AuditOutcome.OK,
        params={"queried": len(report.statuses)},
    )
    if not report.statuses:
        return f"No identified Mikrotik devices to query for '{target}'."

    lines = [f"Live health ({len(report.statuses)} device(s)):"]
    for status in report.statuses:
        name = status.device.identity or status.device.address
        if status.reachability is not Reachability.REACHABLE:
            note = "; ".join(status.notes) or status.reachability.value
            lines.append(f"- {name}: {status.reachability.value} ({note})")
            continue
        cpu = _fmt_pct(status.cpu_load_percent)
        free_mem = _fmt_bytes(status.free_memory_bytes)
        lines.append(
            f"- {name}: reachable, uptime={_fmt_uptime(status.uptime_seconds)}, "
            f"cpu={cpu}, free_mem={free_mem}"
        )
    return "\n".join(lines)


def _uisp_status(ctx: ToolContext, args: dict[str, Any]) -> str:
    from obi.core.engine import uisp_status

    source = ctx.uisp_source
    uisp_cfg = ctx.config.uisp
    if source is None or uisp_cfg is None:
        return "No UISP controller is configured (add a [uisp] section to ask about UISP devices)."

    # feature 036: a live UISP controller read; bounded by the watcher's per-run live-call budget.
    # No-op for chat/voice/web (ctx.live_budget is None).
    denied = _budget_denied(ctx, "uisp read")
    if denied is not None:
        return denied

    name_contains = str(args.get("name_contains", "")).strip() or None
    # engine.uisp_status writes the single `uisp_status` audit record (with the operator's
    # question as intent); we do NOT add a second record here (no double-counting). The UISP
    # token never appears in that record, this rendering, or logs.
    report = uisp_status(
        source,
        audit=ctx.audit,
        controller=uisp_cfg.base_url,
        name_filter=name_contains or uisp_cfg.name_filter,
        timeout=uisp_cfg.timeout,
        reason=ctx.question or "chat uisp status",
    )
    if report.outcome is not SourceOutcome.REACHABLE:
        detail = f" ({report.detail})" if report.detail else ""
        return (
            f"UISP controller {report.outcome.value}{detail} — no UISP device health is "
            f"available right now."
        )
    if not report.statuses:
        focus = f" matching '{name_contains}'" if name_contains else ""
        return f"No UISP devices{focus} on controller {uisp_cfg.base_url}."

    lines = [f"Live UISP health ({len(report.statuses)} device(s)):"]
    for status in report.statuses:
        name = status.device.identity or status.device.address
        if status.reachability is not Reachability.REACHABLE:
            note = "; ".join(status.notes) or status.reachability.value
            lines.append(f"- UISP {name}: {status.reachability.value} ({note})")
            continue
        cpu = _fmt_pct(status.cpu_load_percent)
        free_mem = _fmt_bytes(status.free_memory_bytes)
        lines.append(
            f"- UISP {name} @ {status.device.address}: reachable, "
            f"uptime={_fmt_uptime(status.uptime_seconds)}, cpu={cpu}, free_mem={free_mem}"
        )
    return "\n".join(lines)


def _fmt_link_end(end: Any) -> str:
    """One link end for the model: ``name: score 0.82 (-65 dBm)`` (pieces drop when absent)."""

    name = end.name or "?"
    radio = end.radio
    if radio is None:
        return f"{name}: no radio detail"
    parts: list[str] = []
    if radio.score is not None:
        parts.append(f"score {radio.score:.2f}")
    if radio.signal_dbm is not None:
        parts.append(f"({radio.signal_dbm:g} dBm)")
    return f"{name}: {' '.join(parts) if parts else radio.status or 'no radio detail'}"


def _render_uisp_link(link: Any) -> str:
    """One wireless link as a compact chat line (active) or a DOWN callout."""

    if not link.is_active:
        return f"- {link.label}: DOWN (state={link.state or 'unknown'})"
    bits: list[str] = []
    if link.signal_dbm is not None:
        bits.append(f"{link.signal_dbm:g} dBm")
    if link.frequency_mhz is not None:
        bits.append(f"{link.frequency_mhz:g} MHz")
    if link.distance_m is not None:
        meters = link.distance_m
        bits.append(f"{meters / 1000:.1f} km" if meters >= 1000 else f"{meters:g} m")
    head = f"- {link.label}: active" + (", " + ", ".join(bits) if bits else "")
    return f"{head} | {_fmt_link_end(link.end_a)} | {_fmt_link_end(link.end_b)}"


def _uisp_links(ctx: ToolContext, args: dict[str, Any]) -> str:
    from obi.core.drivers.uisp import UispController
    from obi.core.engine import uisp_links

    source = ctx.uisp_source
    uisp_cfg = ctx.config.uisp
    if not isinstance(source, UispController) or uisp_cfg is None:
        return (
            "No UISP controller is configured (add a [uisp] section to ask about wireless "
            "links/backhauls)."
        )

    # feature 036: a live UISP controller read; bounded by the watcher's per-run live-call budget.
    # No-op for chat/voice/web (ctx.live_budget is None).
    denied = _budget_denied(ctx, "uisp link read")
    if denied is not None:
        return denied

    name_contains = str(args.get("name_contains", "")).strip() or None
    # engine.uisp_links writes the single `uisp_links` audit record (with the operator's
    # question as intent); we do NOT add a second record here (no double-counting). The UISP
    # token never appears in that record, this rendering, or logs.
    report = uisp_links(
        source,
        audit=ctx.audit,
        controller=uisp_cfg.base_url,
        name_filter=name_contains or uisp_cfg.name_filter,
        ignore_names=uisp_cfg.ignore_names,  # feature 035: UISP-scoped exclude
        timeout=uisp_cfg.timeout,
        reason=ctx.question or "chat uisp links",
    )
    if report.outcome is not SourceOutcome.REACHABLE:
        detail = f" ({report.detail})" if report.detail else ""
        return (
            f"UISP controller {report.outcome.value}{detail} — no wireless link health is "
            f"available right now."
        )
    if not report.links:
        focus = f" matching '{name_contains}'" if name_contains else ""
        return f"No wireless links{focus} on controller {uisp_cfg.base_url}."

    counts = report.counts
    lines = [
        f"Live UISP wireless links ({counts['total']} link(s): "
        f"active={counts['active']}, down={counts['down']}):"
    ]
    down_first = sorted(report.links, key=lambda link: link.is_active)
    lines.extend(_render_uisp_link(link) for link in down_first)
    return "\n".join(lines)


def _unifi_status(ctx: ToolContext, args: dict[str, Any]) -> str:
    from obi.core.engine import unifi_status

    source = ctx.unifi_network_source
    unifi_cfg = ctx.config.unifi_network
    if source is None or unifi_cfg is None:
        return (
            "No UniFi Network console is configured "
            "(add a [unifi-network] section to ask about UniFi devices)."
        )

    name_contains = str(args.get("name_contains", "")).strip() or None
    # engine.unifi_status writes the single `unifi_status` audit record (with the operator's
    # question as intent); we do NOT add a second record here (no double-counting). The UniFi
    # credentials never appear in that record, this rendering, or logs.
    report = unifi_status(
        source,
        audit=ctx.audit,
        console=unifi_cfg.base_url,
        action="unifi_status",
        target=f"unifi-network:{unifi_cfg.base_url}",
        name_filter=name_contains or unifi_cfg.name_filter,
        timeout=unifi_cfg.timeout,
        reason=ctx.question or "chat unifi-network status",
    )
    if report.outcome is not SourceOutcome.REACHABLE:
        detail = f" ({report.detail})" if report.detail else ""
        return (
            f"UniFi console {report.outcome.value}{detail} — no UniFi device health is "
            f"available right now."
        )
    if not report.statuses:
        focus = f" matching '{name_contains}'" if name_contains else ""
        return f"No UniFi devices{focus} on console {unifi_cfg.base_url}."

    header = f"Live UniFi health ({len(report.statuses)} device(s)):"
    if report.detail:  # a partial result — one console surface was unavailable
        header = f"Live UniFi health ({len(report.statuses)} device(s); {report.detail}):"
    lines = [header]
    for status in report.statuses:
        name = status.device.identity or status.device.address
        if status.reachability is not Reachability.REACHABLE:
            note = "; ".join(status.notes) or status.reachability.value
            lines.append(f"- UniFi {name}: {status.reachability.value} ({note})")
            continue
        cpu = _fmt_pct(status.cpu_load_percent)
        free_mem = _fmt_bytes(status.free_memory_bytes)
        lines.append(
            f"- UniFi {name} @ {status.device.address}: reachable, "
            f"uptime={_fmt_uptime(status.uptime_seconds)}, cpu={cpu}, free_mem={free_mem}"
        )
    return "\n".join(lines)


def _fmt_metric(sample: MetricSample) -> str:
    if sample.value is None:
        return f"{sample.name}=unavailable"
    suffix = f" ({sample.note})" if sample.note else ""
    return f"{sample.name}={sample.value}{suffix}"


def _snmp_metrics(ctx: ToolContext, args: dict[str, Any]) -> str:
    from obi.core.drivers.snmp_profiles import PROFILES, get_profile
    from obi.core.engine import snmp_metrics

    source = ctx.metrics_source
    if source is None or ctx.config.snmp is None:
        return "SNMP is not configured (add an [snmp] section to read live SNMP metrics)."

    target = str(args.get("target", "all")).strip() or "all"
    profile = str(args.get("profile", "system")).strip() or "system"
    source_sel = str(args.get("source", "all")).strip() or "all"
    if profile not in PROFILES:
        return f"Unknown SNMP profile '{profile}'. Choose one of: {', '.join(PROFILES)}."

    # engine.snmp_metrics writes one `snmp_metrics` audit record per target (with the operator's
    # question as intent); we do NOT add a second record here. SNMP credentials never appear in
    # that record, this rendering, or logs.
    reports = snmp_metrics(
        source,
        config=ctx.config,
        target=target,
        profile_name=profile,
        source=source_sel,
        audit=ctx.audit,
        netbox_source=ctx.netbox_source,
        reason=ctx.question or "chat snmp metrics",
    )
    if not reports:
        return f"No known devices to query over SNMP for '{target}'."

    label_column = get_profile(profile).table_index_label
    lines = [f"Live SNMP metrics (profile={profile}; {len(reports)} device(s)):"]
    for report in reports:
        name = report.target.identity or report.target.address
        head = f"- {name} @ {report.target.address} [snmp/{report.target.source}]: "
        if report.outcome is not SnmpOutcome.REACHABLE:
            note = "; ".join(report.notes) or report.outcome.value
            lines.append(head + f"{report.outcome.value} ({note})")
            continue
        scalars = [s for s in report.samples if s.index is None]
        lines.append(
            head + (", ".join(_fmt_metric(s) for s in scalars) if scalars else "reachable")
        )
        rows: dict[tuple[str, str | None], list[MetricSample]] = {}
        for sample in report.samples:
            if sample.index is not None:
                rows.setdefault((sample.index, sample.label), []).append(sample)
        for (index, label), samples in rows.items():
            body = ", ".join(_fmt_metric(s) for s in samples if s.name != label_column)
            lines.append(f"  - {label or index}: {body}")
    return "\n".join(lines)


# --- metrics history (feature 090) ------------------------------------------------------------

#: Omitting `metric` means "traffic", which is both directions — an operator asking how much a link
#: is carrying wants in AND out, and making them two tool calls invites reporting one as "the"
#: number (research D4).
_TRAFFIC_METRICS = ("ifHCInOctets", "ifHCOutOctets")

#: Hard backstop on series fetched from the store; the DISPLAY cap below is what bounds the reply.
#: One device has ~18 interfaces × 2 directions, so this is never reached in practice.
_MAX_SERIES = 200

#: How many interfaces the no-interface form enumerates before folding the rest into a tail line
#: (FR-007/FR-008) — enough to answer "what's busy?", short enough to speak on the radio.
_MAX_INTERFACES = 8

_BIT_SCALES = (("Gbit/s", 1e9), ("Mbit/s", 1e6), ("kbit/s", 1e3))


def _fmt_bits(bytes_per_second: float) -> str:
    """Render a byte rate the way operators size circuits: auto-scaled BITS per second (FR-005).

    The store is canonical in bytes/s (so `obi metrics history`, the MET widget, and the 027 watch
    pass are untouched); the ×8 happens here, once, and every number carries its unit.
    """

    bits = bytes_per_second * 8.0
    for label, scale in _BIT_SCALES:
        if bits >= scale:
            return f"{bits / scale:.3g} {label}"
    return f"{bits:.3g} bit/s"


def _fmt_summary_value(value: float, unit: str) -> str:
    if unit == "bytes/s":
        return _fmt_bits(value)
    return f"{value:.3g} {unit}" if unit else f"{value:.3g}"


def _fmt_span(since: datetime, until: datetime) -> str:
    """The window actually asked about (FR-006), stated in UTC as the store keeps it."""

    return f"{since:%Y-%m-%d %H:%M} → {until:%Y-%m-%d %H:%M} UTC"


def _peak_of(summaries: list[SeriesSummary]) -> float | None:
    """The busiest peak among a set of series; ``None`` when none of them has any data."""

    peaks = [s.peak for s in summaries if s.samples > 0 and s.peak is not None]
    return max(peaks) if peaks else None


def _describe_series(summary: SeriesSummary) -> str:
    """One series as avg + peak, or as an explicit no-data statement — NEVER as a zero (FR-011)."""

    if summary.samples == 0 or summary.avg is None or summary.peak is None:
        return "no readings"
    avg = _fmt_summary_value(summary.avg, summary.unit)
    peak = _fmt_summary_value(summary.peak, summary.unit)
    return f"{avg} avg, {peak} peak"


def _direction_of(metric: str) -> str | None:
    lower = metric.lower()
    if lower.startswith("ifhcin") or lower.startswith("ifin"):
        return "in"
    if lower.startswith("ifhcout") or lower.startswith("ifout"):
        return "out"
    return None


#: In before out on every line. The store hands back busiest-first, which would otherwise flip the
#: order between one interface and the next — the same reply reading "in … · out …" on one line and
#: "out … · in …" on the next invites misreading which direction is saturated.
_DIRECTION_ORDER = {"in": 0, "out": 1}


def _render_interface_line(ifname: str, summaries: list[SeriesSummary]) -> str:
    """One interface's line, pairing in/out onto it when both directions are present (D4)."""

    ordered = sorted(
        summaries,
        key=lambda s: (_DIRECTION_ORDER.get(_direction_of(s.metric) or "", 2), s.metric),
    )
    parts: list[str] = []
    for summary in ordered:
        direction = _direction_of(summary.metric)
        label = f"{direction} " if direction else ""
        if len(ordered) > 1 and direction is None:
            label = f"{summary.metric} "
        parts.append(f"{label}{_describe_series(summary)}")
    return f"- {ifname}: " + " · ".join(parts)


def _render_history(
    device: str,
    *,
    summaries: list[SeriesSummary],
    since: datetime,
    until: datetime,
    metric_label: str,
) -> str:
    """Render busiest-first, capped, with the omitted interfaces accounted for rather than dropped.

    Bounded by construction: at most :data:`_MAX_INTERFACES` enumerated lines plus a tail, whatever
    the window (FR-008). Measured-idle and no-readings interfaces are counted SEPARATELY — silence
    and zero are different facts (FR-011).
    """

    scalars = [s for s in summaries if s.ifname is None]
    by_iface: dict[str, list[SeriesSummary]] = {}
    for summary in summaries:
        if summary.ifname is not None:
            by_iface.setdefault(summary.ifname, []).append(summary)

    head = f"{device} — {metric_label}, {_fmt_span(since, until)}:"
    lines = [head]
    for summary in scalars:
        lines.append(f"- {summary.metric}: {_describe_series(summary)}")

    ranked = sorted(
        by_iface.items(),
        key=lambda item: -(_peak_of(item[1]) or -1.0),
    )
    active = [(name, group) for name, group in ranked if (_peak_of(group) or 0.0) > 0.0]
    measured_idle = [name for name, group in ranked if _peak_of(group) == 0.0]
    unmeasured = [name for name, group in ranked if _peak_of(group) is None]

    if by_iface and not active:
        # Nothing is moving — but WHY differs, and conflating the two is the FR-011 failure.
        if not measured_idle:
            lines.append(
                f"No readings for any of the {len(unmeasured)} interface(s) in this window — "
                "that is missing data, not zero traffic."
            )
        elif unmeasured:
            lines.append(
                f"No interface carried measurable traffic: {len(measured_idle)} read 0 bit/s, "
                f"and {len(unmeasured)} have no readings at all (missing data, not zero)."
            )
        else:
            lines.append(
                f"No interface carried measurable traffic — all {len(measured_idle)} read 0 bit/s."
            )
        return "\n".join(lines)

    for name, group in active[:_MAX_INTERFACES]:
        lines.append(_render_interface_line(name, group))

    omitted = active[_MAX_INTERFACES:]
    if omitted:
        ceiling = _peak_of(omitted[0][1])
        bound = ""
        if ceiling:
            bound = f" (peak under {_fmt_summary_value(ceiling, omitted[0][1][0].unit)})"
        lines.append(f"… {len(omitted)} more interface(s) with less traffic{bound}.")
    if measured_idle:
        lines.append(f"{len(measured_idle)} interface(s) measured idle (0 bit/s).")
    if unmeasured:
        lines.append(
            f"{len(unmeasured)} interface(s) have NO readings in this window "
            "(missing data, not zero traffic)."
        )
    return "\n".join(lines)


def _resolve_device(needle: str, candidates: list[str]) -> str | None:
    """Case-insensitive exact match, else a UNIQUE substring match; ``None`` when ambiguous.

    Ambiguity deliberately resolves to ``None`` so the caller lists the candidates and the model
    retries with a real name, rather than obi silently answering about the wrong router.
    """

    lowered = needle.strip().lower()
    for candidate in candidates:
        if candidate.lower() == lowered:
            return candidate
    hits = [c for c in candidates if lowered in c.lower()]
    return hits[0] if len(hits) == 1 else None


# feature 100: the query_health tool — hardware-health STATE from the 096 store, rendered
# through the SAME core evaluation the watch pass and the HEALTH board use.

_HEALTH_FLEET_BREACH_CAP = 8  # bounded payload: the radio budget must never truncate the fact
_HEALTH_OK_ROLL_CHARS = 220

#: Device-mode grouping order (metric-prefix / exact-name buckets, worst-story first).
_HEALTH_GROUP_ORDER = ("state", "temperature", "fan", "cpu/memory", "optics", "disk", "power")


def _health_reading_group(metric: str, unit: str) -> str:
    if metric.startswith("health:") and metric.endswith("-state"):
        return "state"
    if unit == "°C":
        return "temperature"
    if metric.startswith("health:") and metric.endswith("-speed"):
        return "fan"
    if metric in (
        "cpuLoad",
        "linux:cpuUtilPercent",
        "linux:memUsedPercent",
        "memUsedKb",
        "memTotalKb",
        "linux:memTotalKb",
        "linux:memAvailableKb",
    ):
        return "cpu/memory"
    if metric.startswith("optics:"):
        return "optics"
    if metric == "linux:diskUsedPercent":
        return "disk"
    return "power"


def _health_age_text(now_us: int, ts_us: int) -> str:
    seconds = max(0, (now_us - ts_us) // 1_000_000)
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 172_800:
        return f"{seconds // 3600}h"
    return f"{seconds // 86_400}d"


def _render_device_health(dev: Any, thresholds: Any, now_us: int) -> str:
    from obi.core import health as health_core

    breaches, _tracked = health_core.evaluate_device(dev, thresholds)
    breached_keys = {(b.metric, b.instance): b for b in breaches}
    age = _health_age_text(now_us, dev.newest_us)
    header = f"{dev.device} ({dev.kind}) — newest data {age} old"
    if dev.stale:
        header += " — STALE (device has not reported recently; values are last-known)"
    lines = [header]
    for b in breaches:
        sensor = b.label or b.instance or b.metric
        lines.append(f"- BREACHING: {sensor} = {b.value:g} {b.unit} (limit {b.limit})".rstrip())
    ordered = sorted(
        dev.readings,
        key=lambda r: (
            _HEALTH_GROUP_ORDER.index(_health_reading_group(r.metric, r.unit)),
            r.metric,
            r.instance,
        ),
    )
    for r in ordered[:30]:
        sensor = r.label or r.instance or r.metric
        group = _health_reading_group(r.metric, r.unit)
        if group == "state":
            healthy = r.value == thresholds.status_healthy_value
            state = "ok" if healthy else f"{r.value:g} (≠ ok)"
            lines.append(f"- {sensor}: {state} ({_health_age_text(now_us, r.ts_us)})")
            continue
        marker = " ← breaching" if (r.metric, r.instance) in breached_keys else ""
        value = f"{r.value:g} {r.unit}".strip()
        lines.append(f"- {sensor}: {value} ({_health_age_text(now_us, r.ts_us)}){marker}")
    if len(dev.readings) > 30:
        lines.append(f"…and {len(dev.readings) - 30} more readings.")
    lines.append("Only sensors this device exposes are listed — anything absent is not measured.")
    return "\n".join(lines)


def _render_fleet_health(fleet: list[Any], thresholds: Any, now_us: int) -> str:
    from obi.core import health as health_core

    evaluated = [(dev, health_core.evaluate_device(dev, thresholds)[0]) for dev in fleet]
    breaching = [(dev, brs) for dev, brs in evaluated if brs]
    stale = [dev for dev, brs in evaluated if not brs and dev.stale]
    healthy = [dev for dev, brs in evaluated if not brs and not dev.stale]
    newest = max((dev.newest_us for dev in fleet), default=now_us)

    lines = [
        f"Fleet health: {len(fleet)} devices · {len(breaching)} breaching · "
        f"{len(stale)} stale · newest data {_health_age_text(now_us, newest)} old"
    ]
    for dev, brs in breaching[:_HEALTH_FLEET_BREACH_CAP]:
        b = brs[0]
        sensor = b.label or b.instance or b.metric
        extra = f" (+{len(brs) - 1} more sensors)" if len(brs) > 1 else ""
        lines.append(
            f"- {dev.device}: {sensor} = {b.value:g} {b.unit} (limit {b.limit}){extra}".rstrip()
        )
    if len(breaching) > _HEALTH_FLEET_BREACH_CAP:
        lines.append(f"…and {len(breaching) - _HEALTH_FLEET_BREACH_CAP} more breaching devices.")
    for dev in stale:
        last = _health_age_text(now_us, dev.newest_us)
        lines.append(f"- {dev.device}: stale — last data {last} ago")
    if not breaching:
        lines.append("No breaches — every reading is within its thresholds.")
    if healthy:
        roll = ", ".join(dev.device for dev in healthy)
        if len(roll) > _HEALTH_OK_ROLL_CHARS:
            shown: list[str] = []
            dropped = 0
            for dev in healthy:
                if len(", ".join([*shown, dev.device])) > _HEALTH_OK_ROLL_CHARS:
                    dropped = len(healthy) - len(shown)
                    break
                shown.append(dev.device)
            roll = ", ".join(shown) + f" (+{dropped} more)"
        lines.append(f"OK: {roll}")
    return "\n".join(lines)


def _query_health(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Answer hardware-health questions from the LOCAL 096 store (feature 100) — no device.

    Self-contained by design: opens the metrics store read-only per call and evaluates via
    the shared ``core/health`` thresholds, so every chat surface (and the radio and the
    watcher) gets identical verdicts to the watch pass and the HEALTH board with zero
    per-surface wiring."""

    from obi.core import health as health_core
    from obi.core import metricstore
    from obi.core.config import default_metrics_db_path

    history_cfg = ctx.config.snmp.history if ctx.config.snmp is not None else None
    db_path = (
        history_cfg.db_path if history_cfg is not None else default_metrics_db_path()
    ).expanduser()
    absent = (
        "No health readings collected yet — the RouterOS side collects via `obi metrics "
        "ingest` and the collector VMs via `obi health probe` (both cron-scheduled)."
    )
    if not db_path.exists():
        return absent

    now_us = int(_utcnow().timestamp() * 1_000_000)
    thresholds = health_core.thresholds_from_settings(ctx.config.watch.health)
    device = str(args.get("device", "")).strip() or None

    known: list[str] = []
    try:
        conn = metricstore.connect_readonly(db_path)
        try:
            fleet = health_core.fleet_status(conn, now_us=now_us, device=device)
            if device is not None and not fleet:
                # Resolve operator shorthand against who actually HAS health data (the 090
                # rule): one extra local read, only on a miss.
                known = [d.device for d in health_core.fleet_status(conn, now_us=now_us)]
                resolved = _resolve_device(device, known) or _resolve_device(
                    device.replace(" ", "-"), known
                )
                if resolved is not None:
                    device = resolved
                    fleet = health_core.fleet_status(conn, now_us=now_us, device=device)
        finally:
            conn.close()
    except metricstore.StoreError as exc:
        return f"The health store is unavailable right now ({exc}) — try again shortly."

    if not fleet:
        if device is not None:
            hint = f" Devices with health data include: {', '.join(known[:8])}." if known else ""
            return f"No health data for {device!r}.{hint}"
        return absent

    if device is not None:
        return _render_device_health(fleet[0], thresholds, now_us)
    return _render_fleet_health(fleet, thresholds, now_us)


# feature 118: the dash_status tool — the 095 status dashboard's evaluated board, rendered as
# THE first answer to "what is the current status". Pure local reads (board.json + the pager
# state); the handler re-judges nothing — the 1-minute cycle stays the only evaluator, so this
# answer always agrees with `obi dash show` and the DASH web board.

_DASH_ITEM_CAP = 8  # bounded payload: the radio budget must never truncate the fact
_DASH_TILE_ITEM_CAP = 20  # drill-down mode lists green items too, so a higher cap
_DASH_INCIDENT_CAP = 4


def _dash_board_stale_after_s() -> float:
    """The board's own staleness idiom (the 095 tile rule: 3 × cadence + 60 s grace) applied
    to the board itself at its fixed one-minute cron cadence → 240 s. Not a config knob on
    purpose: a second tunable would drift from the tile rule (research R2)."""

    from obi.core import dash as dash_core

    return dash_core.STALE_MULTIPLIER * 60.0 + dash_core.STALE_GRACE_S


def _dash_item_line(signal: str, item: Any, now_us: int) -> str:
    from obi.core.dash import _fmt_duration

    line = f"- [{signal}] {item.label}: {item.detail}".rstrip().rstrip(":")
    line += " — in maintenance" if item.maintenance is not None else f" — {item.state}"
    if item.since_us is not None:
        line += f" for {_fmt_duration(max(0.0, (now_us - item.since_us) / 1_000_000))}"
    return line


def _dash_sorted(pairs: list[tuple[str, Any]]) -> list[tuple[str, Any]]:
    """Worst-first, then board tile order, then label — the operator reads damage first."""

    from obi.core import dash as dash_core

    signal_order = {s: i for i, s in enumerate(dash_core.SIGNALS)}
    return sorted(
        pairs,
        key=lambda p: (
            -dash_core._STATE_RANK.get(p[1].state, 0),
            signal_order.get(p[0], len(signal_order)),
            p[1].label,
        ),
    )


def _dash_open_incidents(state_dir: Any, now_us: int) -> list[str]:
    """Open PAGED incidents from the pager's recorded state (feature 106 fields, read-only).

    Open = ``paged_at_us`` set (the alert actually went out — seeded/held entries never
    render) and no ``cleared_at_us`` (presence of that key = recovery observed, per
    run_pager). Members sharing an ``incident_opened_us`` are ONE link incident. An absent
    pager state file simply means no incident section — the pager may be off."""

    from obi.core import dash as dash_core
    from obi.core.dash import _fmt_duration

    payload = dash_core.load_json(state_dir / dash_core.PAGER_STATE_FILE)
    items = payload.get("items") if payload else None
    if not isinstance(items, dict):
        return []

    groups: dict[int, list[dict[str, Any]]] = {}
    singles: list[dict[str, Any]] = []
    for entry in items.values():
        if not isinstance(entry, dict):
            continue
        if not entry.get("paged_at_us") or "cleared_at_us" in entry:
            continue
        opened = entry.get("incident_opened_us")
        if opened:
            groups.setdefault(int(opened), []).append(entry)
        else:
            singles.append(entry)
    if not groups and not singles:
        return []

    def _age(since_us: int) -> str:
        return _fmt_duration(max(0.0, (now_us - since_us) / 1_000_000))

    entries: list[tuple[int, str]] = []
    for opened, members in groups.items():
        labels = " + ".join(sorted({str(m.get("label", "")) for m in members}))
        entries.append((opened, f"- {labels} — paged, open {_age(opened)}"))
    for entry in singles:
        since = int(entry.get("red_since_us") or entry.get("paged_at_us") or now_us)
        entries.append((since, f"- {entry.get('label', '?')} — paged, open {_age(since)}"))
    entries.sort()  # longest-open first

    lines = ["Paged incidents open:"] + [text for _, text in entries[:_DASH_INCIDENT_CAP]]
    if len(entries) > _DASH_INCIDENT_CAP:
        lines.append(f"…and {len(entries) - _DASH_INCIDENT_CAP} more open incidents.")
    return lines


def _dash_status(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Render the 095 board (feature 118) — LOCAL file reads, no device, no re-judging.

    Self-contained like ``_query_health``: resolves the dash state dir per call, loads the
    atomically-written ``board.json``, and renders worst-first within fixed caps. Absence
    and staleness are ANSWERS, never errors (Principle VI): a stale board is disclosed as
    last-known, a missing one as "not running" — old data must never read as current."""

    from obi.core import dash as dash_core
    from obi.core.config import default_dash_state_dir
    from obi.core.dash import _fmt_duration

    settings = ctx.config.dash
    state_dir = (
        settings.state_dir if settings is not None else default_dash_state_dir()
    ).expanduser()
    board = dash_core.load_board(state_dir)
    if board is None:
        if settings is None:
            reason = " (no [dash] section is configured on this host)"
        elif not settings.enabled:
            reason = " ([dash] enabled = false)"
        else:
            reason = ""
        return (
            f"The status dashboard is not running{reason} — no board found in {state_dir}. "
            "The 1-minute `obi dash run` cron writes it, so there is no evaluated overall "
            "status to report. For a manual check, the domain tools (routeros_status, "
            "uisp_status, query_health) still work."
        )

    now_us = int(_utcnow().timestamp() * 1_000_000)
    age_s = max(0.0, (now_us - board.generated_at_us) / 1_000_000)
    age = _fmt_duration(age_s)
    prefix: list[str] = []
    if age_s > _dash_board_stale_after_s():
        prefix.append(
            f"STALE — board generated {age} ago (the cycle runs every minute), so the "
            "dashboard is not updating. Everything below is LAST-KNOWN, not current:"
        )

    signal = str(args.get("signal", "")).strip().lower() or None
    if signal is not None:
        tile = board.tile(signal)
        if tile is None:
            valid = ", ".join(t.signal for t in board.tiles) or ", ".join(dash_core.SIGNALS)
            return f"No signal {signal!r} on the board — valid signals: {valid}."
        header = f"Signal {tile.signal}: {tile.state} — {len(tile.items)} items, board {age} old"
        if tile.data_age_s is not None:
            header += f", tile data {_fmt_duration(tile.data_age_s)} old"
        ordered = _dash_sorted([(tile.signal, item) for item in tile.items])
        lines = [_dash_item_line(s, item, now_us) for s, item in ordered[:_DASH_TILE_ITEM_CAP]]
        if len(ordered) > _DASH_TILE_ITEM_CAP:
            lines.append(f"…and {len(ordered) - _DASH_TILE_ITEM_CAP} more items not shown.")
        return "\n".join(prefix + [header] + lines)

    total_items = sum(len(t.items) for t in board.tiles)
    incidents = _dash_open_incidents(state_dir, now_us)
    # 135: maintenance items are deliberate, not problems — they never block "all green" and
    # are summarised in one line instead of listed (FR-015).
    non_green = _dash_sorted(
        [
            (t.signal, item)
            for t in board.tiles
            for item in t.items
            if item.state != "green" and item.maintenance is None
        ]
    )
    maint_lines = _dash_maintenance_lines(board)
    if board.state == "green" and not non_green and not incidents:
        line = (
            f"Dashboard: all green — {len(board.tiles)} signals, {total_items} items "
            f"evaluated, board {age} old (refreshed every minute)."
        )
        return "\n".join(prefix + [line] + maint_lines + [f"Note: {n}" for n in board.notes])

    lines = prefix + [
        f"Dashboard: {board.state.upper()} — board {age} old (refreshed every minute).",
        "Signals: "
        + " · ".join(
            f"{t.signal} {'maintenance' if t.maintenance is not None else t.state}"
            for t in board.tiles
        ),
    ]
    lines += [_dash_item_line(s, item, now_us) for s, item in non_green[:_DASH_ITEM_CAP]]
    if len(non_green) > _DASH_ITEM_CAP:
        worst = non_green[_DASH_ITEM_CAP:]
        by_signal = ", ".join(sorted({s for s, _ in worst}))
        lines.append(
            f"…and {len(worst)} more not shown (pass signal='<name>' for a full tile: {by_signal})."
        )
    lines += incidents
    lines += maint_lines
    lines += [f"Note: {n}" for n in board.notes]
    return "\n".join(lines)


def _dash_maintenance_lines(board: Any) -> list[str]:
    """The one "In maintenance:" line (135 FR-015) — empty when nothing is in maintenance."""

    count = sum(1 for t in board.tiles for i in t.items if i.maintenance is not None)
    if board.maintenance is not None:
        text = board.maintenance.line()
        if count:
            text += f" ({count} board items, excluded from the verdict)"
        return [f"In maintenance: {text}"]
    if count:
        return [f"In maintenance: {count} board items (excluded from the verdict)."]
    return []


# feature 110: the weather_forecast tool — site weather from the LOCAL snapshot store (hourly
# cron: Open-Meteo forecast + NWS alerts). Raw numbers only, by operator decision — no dust
# verdicts, no risk bands; interpretation stays in the conversation.

_WEATHER_ALERT_CAP = 3  # bounded payload: the radio budget must never truncate the fact
_WEATHER_HEADLINE_CHARS = 100
_WEATHER_ABSENT = "No weather data collected yet — the hourly `obi weather run` cron populates it."
#: All operator-facing times are site-local; the store keeps epoch µs UTC. Feature 137: the
#: zone is the operator's ``[site] timezone`` (default UTC), set per dispatched call.
_SITE_TZ: ContextVar[str] = ContextVar("obi_site_tz", default="UTC")

_COMPASS_POINTS = (
    "N",
    "NNE",
    "NE",
    "ENE",
    "E",
    "ESE",
    "SE",
    "SSE",
    "S",
    "SSW",
    "SW",
    "WSW",
    "W",
    "WNW",
    "NW",
    "NNW",
)


def _playa_tz() -> Any:
    from zoneinfo import ZoneInfo

    return ZoneInfo(_SITE_TZ.get())


def _playa_dt(ts_us: int) -> Any:
    from datetime import datetime

    return datetime.fromtimestamp(ts_us / 1_000_000, tz=_playa_tz())


def _compass(deg: float | None) -> str:
    if deg is None:
        return "?"
    return _COMPASS_POINTS[int((deg % 360) / 22.5 + 0.5) % 16]


def _g(value: float) -> str:
    return f"{value:g}"


def _weather_now_line(current: Any) -> str:
    """The current-conditions sentence; a missing value says "unavailable", never 0 (FR-006)."""

    parts = [
        f"{_g(current.temp_f)}°F" if current.temp_f is not None else "temp unavailable",
        f"{_g(current.humidity_pct)}% RH" if current.humidity_pct is not None else "RH unavailable",
        f"dew {_g(current.dew_point_f)}°F"
        if current.dew_point_f is not None
        else "dew unavailable",
    ]
    if current.wind_mph is not None:
        wind = f"wind {_compass(current.wind_dir_deg)} {_g(current.wind_mph)} mph"
        if current.gust_mph is not None:
            wind += f" gusting {_g(current.gust_mph)} mph"
        parts.append(wind)
    else:
        parts.append("wind unavailable")
    parts.append(
        f"precip {current.precip_in:.2f} in"
        if current.precip_in is not None
        else "precip unavailable"
    )
    return ", ".join(parts) + "."


def _weather_alert_lines(snapshot: Any, alerts: list[Any]) -> list[str]:
    """The three alert states, never conflated (FR-004/FR-006): active / verified none /
    unknown because the source failed at snapshot time."""

    as_of = f"{_playa_dt(snapshot.fetched_us):%H:%M}"
    if not snapshot.alerts_ok:
        return [f"Alerts: unknown as of {as_of} (source failed: {snapshot.alerts_error})."]
    if not alerts:
        return [f"Alerts: none active as of {as_of}."]
    lines = []
    for alert in alerts[:_WEATHER_ALERT_CAP]:
        window = ""
        if alert.onset_us is not None and alert.ends_us is not None:
            window = f" {_playa_dt(alert.onset_us):%a %H:%M}–{_playa_dt(alert.ends_us):%a %H:%M}"
        severity = f" ({alert.severity})" if alert.severity else ""
        headline = ""
        if alert.headline:
            text = alert.headline
            if len(text) > _WEATHER_HEADLINE_CHARS:
                text = text[: _WEATHER_HEADLINE_CHARS - 1] + "…"
            headline = f" — {text}"
        lines.append(f"- ALERT: {alert.event}{severity}{window}{headline}")
    if len(alerts) > _WEATHER_ALERT_CAP:
        lines.append(f"…and {len(alerts) - _WEATHER_ALERT_CAP} more alerts.")
    return lines


def _weather_hourly_lines(rows: list[Any]) -> list[str]:
    lines = ["Next 24h (local · temp · gust@dir · rain%/amount · RH):"]
    for r in rows:
        temp = f"{_g(r.temp_f)}°F" if r.temp_f is not None else "?"
        gust = f"g{_g(r.gust_mph)}" if r.gust_mph is not None else "g?"
        prob = f"{_g(r.precip_prob_pct)}%" if r.precip_prob_pct is not None else "?%"
        precip = f"{r.precip_in:.2f}in" if r.precip_in is not None else "?in"
        rh = f"{_g(r.humidity_pct)}%" if r.humidity_pct is not None else "?%"
        lines.append(
            f"{_playa_dt(r.hour_us):%H:%M} {temp} {gust}{_compass(r.wind_dir_deg)} "
            f"{prob} {precip} {rh}"
        )
    return lines


def _weather_history(conn: Any, at_text: str, now_us: int) -> str:
    from datetime import datetime

    from obi.core import weatherstore

    try:
        asked = datetime.fromisoformat(at_text.strip())
    except ValueError:
        return (
            "History takes a past playa-local time as 'YYYY-MM-DD HH:MM' "
            "(e.g. '2026-08-27 02:00'); omit 'at' for the 24-hour outlook."
        )
    if asked.tzinfo is None:
        asked = asked.replace(tzinfo=_playa_tz())
    at_us = int(asked.timestamp() * 1_000_000)
    if at_us > now_us:
        return (
            f"{asked:%Y-%m-%d %H:%M} is in the future — omit 'at' for the 24-hour "
            "outlook; history covers only recorded time."
        )
    span = weatherstore.coverage(conn)
    if span is None:
        return _WEATHER_ABSENT
    if at_us < span[0]:
        return (
            f"Local weather records begin {_playa_dt(span[0]):%Y-%m-%d %H:%M} (local time) — "
            f"nothing is recorded for {asked:%Y-%m-%d %H:%M}."
        )
    snapshot = weatherstore.nearest_snapshot(conn, at_us)
    if snapshot is None or snapshot.current is None:
        return (
            "No conditions were recorded around then (the fetches in that stretch failed); "
            f"records span {_playa_dt(span[0]):%Y-%m-%d %H:%M} to "
            f"{_playa_dt(span[1]):%Y-%m-%d %H:%M}."
        )
    distance_us = abs(snapshot.fetched_us - at_us)
    direction = "before" if snapshot.fetched_us <= at_us else "after"
    distance = _health_age_text(distance_us, 0)
    return (
        f"Conditions recorded {_playa_dt(snapshot.fetched_us):%Y-%m-%d %H:%M} "
        f"({distance} {direction} the asked {asked:%H:%M}):\n" + _weather_now_line(snapshot.current)
    )


def _weather_forecast_impl(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Answer weather questions from the LOCAL 110 store — never any network I/O.

    Self-contained by design (the query_health/audit_query pattern): opens the weather store
    read-only per call, so every chat surface, the radio, and the watcher answer identically
    with zero per-surface wiring — and keep answering from the last snapshot, with its age,
    when the playa uplink is down (Principle VI)."""

    from obi.core import weatherstore

    if ctx.config.weather is None:  # 137: hidden by `requires`; a direct call still answers
        return "weather is not configured on this host (no [weather] section)."
    db_path = ctx.config.weather.db_path.expanduser()
    if not db_path.exists():
        return _WEATHER_ABSENT

    now_us = int(_utcnow().timestamp() * 1_000_000)
    at_text = str(args.get("at", "")).strip()

    try:
        conn = weatherstore.connect_readonly(db_path)
        try:
            if at_text:
                return _weather_history(conn, at_text, now_us)

            newest = weatherstore.newest_snapshot(conn)
            if newest is None:
                return _WEATHER_ABSENT
            base: weatherstore.Snapshot | None = newest
            if not newest.forecast_ok:
                fallback = weatherstore.newest_snapshot_with_forecast(conn)
                # Serve the last good forecast when it is still inside its own 24 h horizon;
                # older than that it predicts nothing about the next 24 h.
                if fallback is not None and (
                    newest.fetched_us - fallback.fetched_us <= 24 * 3_600 * 1_000_000
                ):
                    base = fallback
                else:
                    base = None
            lines: list[str] = []
            if base is not None and base.current is not None:
                age = _health_age_text(now_us, base.fetched_us)
                stamp = f"{_playa_dt(base.fetched_us):%a %H:%M}"
                lines.append(f"Weather as of {stamp} ({age} ago).")
                lines.append("Now: " + _weather_now_line(base.current))
            else:
                lines.append(
                    f"Forecast unavailable as of {_playa_dt(newest.fetched_us):%a %H:%M} "
                    f"(source failed: {newest.forecast_error}); no usable snapshot in the "
                    "last 24h."
                )
            lines.extend(_weather_alert_lines(newest, weatherstore.alert_rows(conn, newest.id)))
            if base is not None:
                lines.extend(_weather_hourly_lines(weatherstore.hourly_rows(conn, base.id)))
            return "\n".join(lines)
        finally:
            conn.close()
    except weatherstore.StoreError as exc:
        return f"The weather store is unavailable right now ({exc}) — try again shortly."


# feature 112: the gate_traffic tool — event gate travel times + gate status from the LOCAL
# traffic store (5-min cron: the dashboard's Travel Time Log + gate status). Entries are
# served VERBATIM as published — no derived waits, no averages, no trend verdicts (spec
# FR-003/SC-004); interpretation stays in the conversation.

_TRAFFIC_ENTRY_CAP = 5  # newest report + 4 for trend — bounded payload (FR-007)
_TRAFFIC_ENTRY_CHARS = 200  # per-entry clip, announced with an ellipsis, never silent
_TRAFFIC_ADVISORY_CHARS = 160
_TRAFFIC_ABSENT = (
    "No traffic data collected yet — the 5-minute `obi traffic run` cron populates it."
)


def _traffic_clip(text: str, cap: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= cap else flat[:cap] + "…"


def _traffic_stamp(ts_us: int, now_us: int) -> str:
    return f"{_playa_dt(ts_us):%a %H:%M} ({_health_age_text(now_us, ts_us)} ago)"


def _traffic_entry_lines(entries: list[Any], now_us: int) -> list[str]:
    """Newest report prominent, then up to four earlier entries for trend — each text
    verbatim (clipped, visibly) with its playa-local time; an unstamped entry is honest
    about carrying only its capture time ("first seen")."""

    lines: list[str] = []
    newest = entries[0]
    stamp = (
        _traffic_stamp(newest.published_us, now_us)
        if newest.published_us is not None
        else f"first seen {_traffic_stamp(newest.first_seen_us, now_us)}"
    )
    author = f", {newest.author}" if newest.author else ""
    lines.append(
        f"Newest report — {stamp}{author}: {_traffic_clip(newest.text, _TRAFFIC_ENTRY_CHARS)}"
    )
    if len(entries) > 1:
        lines.append("Earlier reports (newest first):")
        for entry in entries[1:]:
            stamp = (
                _traffic_stamp(entry.published_us, now_us)
                if entry.published_us is not None
                else f"first seen {_traffic_stamp(entry.first_seen_us, now_us)}"
            )
            lines.append(f"- {stamp}: {_traffic_clip(entry.text, _TRAFFIC_ENTRY_CHARS)}")
    return lines


def _traffic_status_line(gate: Any, poll: Any, now_us: int) -> str:
    """The published gate status, honestly framed: lanes + advisory verbatim; the
    all-null publication is "not reported" (never defaulted); a failed source at the
    newest poll marks the status unknown-as-of (FR-004/FR-005)."""

    failure = ""
    if poll is not None and poll.status_outcome in ("refused", "failed"):
        detail = f" ({poll.status_error})" if poll.status_error else ""
        failure = f"source {poll.status_outcome}{detail} at the last check"
    if gate is None:
        if failure:
            return f"Gate status: unknown — {failure}."
        return "Gate status: nothing recorded yet."
    if gate.inbound_state is None and gate.outbound_state is None and gate.advisory is None:
        line = (
            "Gate status: the dashboard is not currently reporting gate status "
            f"(as of {_traffic_stamp(gate.captured_us, now_us)})"
        )
    else:
        lanes = (
            f"inbound {gate.inbound_state or 'not reported'}, "
            f"outbound {gate.outbound_state or 'not reported'}"
        )
        advisory = (
            f" — {_traffic_clip(gate.advisory, _TRAFFIC_ADVISORY_CHARS)}" if gate.advisory else ""
        )
        line = f"Gate status (as of {_traffic_stamp(gate.captured_us, now_us)}): {lanes}{advisory}"
    if failure:
        line += f"; currently unknown — {failure}"
    return line + "."


def _gate_traffic_impl(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Answer gate travel-time and gate-status questions from the LOCAL 112 store — never
    any network I/O.

    Self-contained by design (the weather_forecast/query_health pattern): opens the traffic
    store read-only per call, so every chat surface, the radio, and the watcher answer
    identically with zero per-surface wiring — and keep answering from the last stored
    entries, with both the data's age and the last-check age, when the playa uplink is down
    or the dashboard still refuses the pre-event feed (Principle VI)."""

    from obi.core import trafficstore

    if ctx.config.traffic is None:  # 137: hidden by `requires`; a direct call still answers
        return "gate traffic is not configured on this host (no [traffic] section)."
    db_path = ctx.config.traffic.db_path.expanduser()
    if not db_path.exists():
        return _TRAFFIC_ABSENT

    now_us = int(_utcnow().timestamp() * 1_000_000)
    try:
        conn = trafficstore.connect_readonly(db_path)
        try:
            poll = trafficstore.newest_poll(conn)
            if poll is None:
                return _TRAFFIC_ABSENT
            entries = trafficstore.recent_entries(conn, limit=_TRAFFIC_ENTRY_CAP)
            gate = trafficstore.newest_gate_status(conn)

            lines = ["Gate travel time (event dashboard Travel Time Log, as published):"]
            if entries:
                lines.extend(_traffic_entry_lines(entries, now_us))
            elif poll.feed_outcome == "refused":
                lines.append(
                    "The dashboard hasn't published travel times yet — the Travel Time "
                    f"Log is not public (refused: {poll.feed_error or 'refused'})."
                )
            else:
                lines.append("No travel-time entries stored yet.")
            lines.append(_traffic_status_line(gate, poll, now_us))
            feed_note = poll.feed_outcome + (f" ({poll.feed_error})" if poll.feed_error else "")
            status_note = poll.status_outcome + (
                f" ({poll.status_error})" if poll.status_error else ""
            )
            lines.append(
                f"Checked {_health_age_text(now_us, poll.fetched_us)} ago — "
                f"feed: {feed_note}; gate status: {status_note}."
            )
            return "\n".join(lines)
        finally:
            conn.close()
    except trafficstore.StoreError as exc:
        return f"The traffic store is unavailable right now ({exc}) — try again shortly."


def _metrics_history(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Read STORED SNMP history from the local time-series store (feature 090) — no device."""

    from obi.core import logstore
    from obi.core.engine import query_metrics_history

    source = ctx.metrics_history_source
    history_cfg = ctx.config.snmp.history if ctx.config.snmp is not None else None
    if source is None or history_cfg is None:
        return (
            "No metrics history is configured (add an [snmp.history] section and run "
            "`obi metrics ingest` on a schedule to collect it). For a reading right now, use "
            "snmp_metrics."
        )

    device = str(args.get("device", "")).strip()
    if not device:
        return "Which device? Pass a device identity, e.g. 'site-gw-1'."
    interface = str(args.get("interface", "")).strip() or None
    metric = str(args.get("metric", "")).strip() or None
    metrics = (metric,) if metric else _TRAFFIC_METRICS
    metric_label = metric if metric else "traffic"

    now = _utcnow()
    since = logstore.parse_when(args.get("since"), UTC) or (now - timedelta(hours=24))
    until = logstore.parse_when(args.get("until"), UTC) or now
    if until <= since:
        return f"That window is empty ({_fmt_span(since, until)}) — 'until' must be after 'since'."

    def run(target: str) -> MetricsHistoryReport:
        # engine.query_metrics_history writes the single `metrics_history` audit record (with the
        # operator's question as intent and only the query shape) — no second record here.
        return query_metrics_history(
            source,
            audit=ctx.audit,
            db_label=str(history_cfg.db_path),
            device=target,
            metrics=metrics,
            since=since,
            until=until,
            ifname_contains=interface,
            limit=_MAX_SERIES,
            reason=ctx.question or "chat metrics history",
        )

    report = run(device)
    if report.known_devices:
        # The store is authoritative for who HAS history, so operator shorthand ("showers
        # internet") resolves against that list and retries once — one extra local read, only
        # on a miss.
        resolved = _resolve_device(device, report.known_devices)
        if resolved is not None and resolved != device:
            device = resolved
            report = run(device)

    if report.outcome is not SourceOutcome.REACHABLE:
        detail = f" ({report.detail})" if report.detail else ""
        return f"Metrics history is unavailable{detail}."

    if report.summaries:
        return _render_history(
            device,
            summaries=report.summaries,
            since=since,
            until=until,
            metric_label=metric_label,
        )

    # Empty — say WHICH kind of empty. None of these may read as "zero traffic" (FR-011).
    if report.known_devices:
        known = ", ".join(report.known_devices) or "none"
        return (
            f"No stored history for {device!r} — obi collects history for: {known}. "
            "For a device that isn't collected, snmp_metrics reads it live."
        )
    if report.known_ifnames:
        return (
            f"{device} has no interface matching {interface!r}. Its interfaces: "
            + ", ".join(report.known_ifnames)
            + "."
        )
    hint = ""
    if report.store_oldest_us is not None:
        oldest = datetime.fromtimestamp(report.store_oldest_us / 1_000_000, tz=UTC)
        hint = f" The oldest reading obi holds is {oldest:%Y-%m-%d %H:%M} UTC."
    return (
        f"{device}: no readings for {metric_label} in {_fmt_span(since, until)} — that is missing "
        f"data, not zero traffic.{hint}"
    )


# --- feature 108: the external VictoriaMetrics store — the SECOND-TIER remote metrics source. Obi's
# own stores (021 SNMP history, 096 health) stay primary for anything they cover; this store
# holds what obi doesn't collect: the playa backbone PTP radios' RF, the weather stations,
# fleet optics, and the external team's Linux hosts. Every handler here gates on
# ctx.victoria_source,
# consumes the 028 live budget (remote HTTP), and SHORT-CIRCUITS on the first non-REACHABLE
# report — worst-case latency stays ~one timeout, never a per-request pile-up.

_NO_VICTORIA = (
    "the external metrics store is not configured — add a [victoria] section to obi's config."
)

#: RF samples older than this lead the reply with their age (~8 of the store's 15 s scrape
#: intervals) — old values must never pose as current.
_VIC_RF_STALE_S = 120
#: Site fan-out renders at most this many radios (an honest "…and N more" past it).
_VIC_MAX_SITE_RADIOS = 8
#: Range-query resolution: never finer than the store's scrape cadence, aim ~this many points.
_VIC_MIN_STEP_S = 15
_VIC_TARGET_POINTS = 200

#: The RF metric superset across board families (AF60/Wave, Siklu EH, AF11, 5XHD — see
#: specs/108-grafana-tools/research.md R2). Coverage is heterogeneous by board: the renderer
#: shows what exists and never invents the rest.
_VIC_RF_METRICS = (
    "cinr_Dbm",
    "rssi_Dbm",
    "rssi0_Dbm",
    "rssi1_Dbm",
    "txPower_Dbm",
    "rxModulation_Rate",
    "txModulation_Rate",
    "mode_Modulation",
    "rxCapacity_Kbps",
    "txCapacity_Kbps",
    "rxCapacity_Bps",
    "txCapacity_Bps",
    "throughput_Mbps",
    "rxThroughput_Kbps",
    "txThroughput_Kbps",
    "frequency_Mhz",
    "channelWidth_Mhz",
    "linkState_Bool",
    "temp_Celcius",
    "uptime_Seconds",
)

#: The subset worth trending (signal quality + capacity).
_VIC_RF_TREND_METRICS = (
    "cinr_Dbm",
    "rssi_Dbm",
    "rssi0_Dbm",
    "rssi1_Dbm",
    "rxCapacity_Kbps",
    "txCapacity_Kbps",
    "rxCapacity_Bps",
    "txCapacity_Bps",
    "throughput_Mbps",
    "rxThroughput_Kbps",
    "txThroughput_Kbps",
)

#: Hostname convention `<site>-ptp-<peer>-<band>` — the band suffix names the RF band.
_VIC_BAND_BY_SUFFIX = {"60": "60 GHz", "70": "70/80 GHz", "11": "11 GHz", "05": "5 GHz"}


def _vic_fmt_bits(bits: float) -> str:
    """Auto-scaled bits/s from a value ALREADY in bits (the store's capacity units vary)."""

    for label, scale in _BIT_SCALES:
        if abs(bits) >= scale:
            return f"{bits / scale:.3g} {label}"
    return f"{bits:.3g} bit/s"


def _vic_metric_bits(metric: str, value: float) -> float:
    """Normalize a capacity/throughput reading to bits/s (Kbps/Mbps/Bps per board family)."""

    if metric.endswith("_Kbps"):
        return value * 1e3
    if metric.endswith("_Mbps"):
        return value * 1e6
    return value  # *_Bps carries bits/s already (AF11 family)


def _vic_host_header(hostname: str, board: str | None) -> str:
    """``siteA-ptp-siteB-60`` → ``siteA-ptp-siteB-60 (AF60-LR, 60 GHz → siteB)`` (best effort)."""

    parts: list[str] = []
    if board:
        parts.append(board)
    if "-ptp-" in hostname:
        rest = hostname.split("-ptp-", 1)[1]
        peer, _, suffix = rest.rpartition("-")
        band = _VIC_BAND_BY_SUFFIX.get(suffix)
        if band and peer:
            parts.append(f"{band} → {peer}")
    return f"{hostname} ({', '.join(parts)})" if parts else hostname


def _vic_now_parts(latest: dict[str, float], *, verbose: bool) -> list[str]:
    """The current-values fragment for one radio, in canonical order, present metrics only."""

    parts: list[str] = []
    if "cinr_Dbm" in latest:
        parts.append(f"CINR {latest['cinr_Dbm']:.3g} dB")
    if "rssi_Dbm" in latest:
        parts.append(f"RSSI {latest['rssi_Dbm']:.4g} dBm")
    if "rssi0_Dbm" in latest or "rssi1_Dbm" in latest:
        chains = "/".join(f"{latest[m]:.4g}" for m in ("rssi0_Dbm", "rssi1_Dbm") if m in latest)
        parts.append(f"RSSI {chains} dBm")
    if verbose and "txPower_Dbm" in latest:
        parts.append(f"tx pwr {latest['txPower_Dbm']:.3g} dBm")
    if verbose:
        if "rxModulation_Rate" in latest or "txModulation_Rate" in latest:
            rx = latest.get("rxModulation_Rate")
            tx = latest.get("txModulation_Rate")
            both = "/".join(f"{v:g}" for v in (rx, tx) if v is not None)
            parts.append(f"mod {both}")
        elif "mode_Modulation" in latest:
            parts.append(f"mod {latest['mode_Modulation']:g}")
    down = next(
        (m for m in ("rxCapacity_Kbps", "rxCapacity_Bps", "rxThroughput_Kbps") if m in latest),
        None,
    )
    up = next(
        (m for m in ("txCapacity_Kbps", "txCapacity_Bps", "txThroughput_Kbps") if m in latest),
        None,
    )
    if down or up:
        rates = "/".join(_vic_fmt_bits(_vic_metric_bits(m, latest[m])) for m in (down, up) if m)
        parts.append(f"cap {rates}")
    elif "throughput_Mbps" in latest:
        parts.append(f"thru {_vic_fmt_bits(latest['throughput_Mbps'] * 1e6)}")
    if "linkState_Bool" in latest:
        parts.append("link up" if latest["linkState_Bool"] else "LINK DOWN")
    if verbose and "frequency_Mhz" in latest:
        freq = f"{latest['frequency_Mhz']:g} MHz"
        if "channelWidth_Mhz" in latest:
            freq += f" @{latest['channelWidth_Mhz']:g} MHz"
        parts.append(freq)
    if verbose and "temp_Celcius" in latest:
        parts.append(f"{latest['temp_Celcius']:g} °C")
    if verbose and "uptime_Seconds" in latest:
        parts.append(f"up {_fmt_uptime(int(latest['uptime_Seconds']))}")
    return parts


def _vic_trend_parts(trends: dict[str, tuple[float, float, float]]) -> list[str]:
    """min/avg/max fragments for one radio's trended metrics (present metrics only)."""

    parts: list[str] = []
    if "cinr_Dbm" in trends:
        mn, avg, mx = trends["cinr_Dbm"]
        parts.append(f"CINR {mn:.3g}–{mx:.3g} dB (avg {avg:.3g})")
    rssi = next((m for m in ("rssi_Dbm", "rssi0_Dbm") if m in trends), None)
    if rssi:
        mn, _avg, mx = trends[rssi]
        parts.append(f"RSSI {mn:.4g}…{mx:.4g} dBm")
    cap_mins = [
        _vic_metric_bits(m, trends[m][0])
        for m in _VIC_RF_TREND_METRICS
        if m in trends and not m.startswith(("cinr", "rssi"))
    ]
    if cap_mins:
        parts.append(f"cap min {_vic_fmt_bits(min(cap_mins))}")
    return parts


def _vic_group_latest(
    series: list[TsSeries],
) -> dict[str, tuple[dict[str, float], int]]:
    """Instant series → per-hostname (metric → latest value, newest sample ts in µs)."""

    grouped: dict[str, tuple[dict[str, float], int]] = {}
    for one in series:
        hostname = one.labels.get("hostname")
        metric = one.labels.get("__name__")
        if not hostname or not metric or not one.samples:
            continue
        sample = one.samples[-1]
        latest, newest_us = grouped.get(hostname, ({}, 0))
        latest[metric] = sample.value
        newest_us = max(newest_us, int(sample.ts.timestamp() * 1_000_000))
        grouped[hostname] = (latest, newest_us)
    return grouped


def _vic_group_trends(
    series: list[TsSeries],
) -> dict[str, dict[str, tuple[float, float, float]]]:
    """Matrix series → per-hostname (metric → (min, avg, max))."""

    grouped: dict[str, dict[str, tuple[float, float, float]]] = {}
    for one in series:
        hostname = one.labels.get("hostname")
        metric = one.labels.get("__name__")
        if not hostname or not metric or not one.samples:
            continue
        values = [s.value for s in one.samples]
        grouped.setdefault(hostname, {})[metric] = (
            min(values),
            sum(values) / len(values),
            max(values),
        )
    return grouped


def _vic_boards(series: list[TsSeries]) -> dict[str, str]:
    return {
        one.labels["hostname"]: one.labels["board"]
        for one in series
        if "hostname" in one.labels and "board" in one.labels
    }


def _backbone_rf_health(ctx: ToolContext, args: dict[str, Any]) -> str:
    from obi.core import logstore

    if ctx.victoria_source is None or ctx.config.victoria is None:
        return _NO_VICTORIA
    denied = _budget_denied(ctx, "metrics store read")
    if denied is not None:
        return denied
    target = str(args.get("target", "")).strip()
    if not target:
        return "name a backbone radio (e.g. 'siteA-ptp-siteB-60') or a site (e.g. 'siteA')."
    source = ctx.victoria_source
    timeout = ctx.config.victoria.timeout

    roster = source.label_values("hostname", match='{role="ptp-rf"}', timeout=timeout)
    if roster.outcome is not SourceOutcome.REACHABLE:
        return f"{_store_name(ctx)} is unavailable: {roster.detail or roster.outcome}"
    hostnames = sorted(roster.values)
    if not hostnames:
        return (
            f"no backbone radios found in {_store_name(ctx)} — looked for series labeled "
            'role="ptp-rf"; the store may have been re-labeled.'
        )

    # Resolution (FR-002): exact hostname → one radio; a site name → its radios; a unique
    # substring → one radio; anything else refuses with the roster, never a silent guess.
    sites = {h.split("-ptp-", 1)[0] for h in hostnames if "-ptp-" in h}
    lowered = target.lower()
    if lowered in sites:
        radios = [h for h in hostnames if h.split("-ptp-", 1)[0] == lowered]
        host_expr = f'hostname=~"{re.escape(lowered)}-ptp-.+"'
        verbose = False
    else:
        resolved = _resolve_device(target, hostnames)
        if resolved is None:
            return (
                f"{target!r} matches no single backbone radio or site. Radios in the store: "
                + ", ".join(hostnames)
            )
        radios = [resolved]
        host_expr = f'hostname="{resolved}"'
        verbose = True

    now_query = "{" + host_expr + ',__name__=~"' + "|".join(_VIC_RF_METRICS) + '"}'
    current = source.query(now_query, timeout=timeout)
    if current.outcome is not SourceOutcome.REACHABLE:
        return f"{_store_name(ctx)} is unavailable: {current.detail or current.outcome}"
    if current.detail:
        return f"query error from the store: {current.detail}"

    window_arg = str(args.get("window") or ctx.config.victoria.rf_window)
    until = datetime.now(UTC)
    since = logstore.parse_when(window_arg, UTC) or until - timedelta(hours=24)
    span_s = max(60.0, (until - since).total_seconds())
    step_s = max(_VIC_MIN_STEP_S, int(span_s / _VIC_TARGET_POINTS))
    trend_query = "{" + host_expr + ',__name__=~"' + "|".join(_VIC_RF_TREND_METRICS) + '"}'
    trend = source.query_range(trend_query, start=since, end=until, step_s=step_s, timeout=timeout)

    latest_by_host = _vic_group_latest(current.series)
    trends_by_host = (
        _vic_group_trends(trend.series)
        if trend.outcome is SourceOutcome.REACHABLE and not trend.detail
        else {}
    )
    boards = _vic_boards(current.series)
    now_us = int(until.timestamp() * 1_000_000)

    lines: list[str] = []
    if not verbose:
        lines.append(f"{lowered}: {len(radios)} backbone radio(s) — {_store_name(ctx)}")
    for hostname in radios[:_VIC_MAX_SITE_RADIOS]:
        lines.append(_vic_host_header(hostname, boards.get(hostname)))
        grouped = latest_by_host.get(hostname)
        if grouped is None:
            lines.append("  no current samples in the store")
            continue
        latest, newest_us = grouped
        age_s = max(0, (now_us - newest_us) // 1_000_000)
        if age_s > _VIC_RF_STALE_S:
            lines.append(f"  last sample {_health_age_text(now_us, newest_us)} ago:")
        parts = _vic_now_parts(latest, verbose=verbose)
        if parts:
            lines.append("  " + " · ".join(parts))
        trend_parts = _vic_trend_parts(trends_by_host.get(hostname, {}))
        if trend_parts:
            lines.append(f"  {window_arg}: " + " · ".join(trend_parts))
        elif verbose:
            lines.append(f"  {window_arg}: no trend data in the store for this window")
    if len(radios) > _VIC_MAX_SITE_RADIOS:
        lines.append(f"…and {len(radios) - _VIC_MAX_SITE_RADIOS} more radios at this site")
    if trend.outcome is not SourceOutcome.REACHABLE:
        lines.append(f"(trend unavailable: {trend.detail or trend.outcome})")
    return "\n".join(lines)


#: Free-form range queries are capped at this span — the store retains 12 months, but an
#: unbounded window is never needed to answer a chat question and would hammer the host.
_VIC_MAX_RANGE_S = 31 * 86_400
#: Discovery replies list at most this many values.
_VIC_MAX_VALUES = 200
#: Free-form replies render at most this many series (the driver's transport cap is higher —
#: 100 — so curated handlers receive everything their scoped queries match).
_VIC_MAX_RENDER_SERIES = 25

#: Scrape-plumbing labels dropped from rendered series (they identify the collector, not the
#: measurement).
_VIC_NOISE_LABELS = {"instance", "job", "otel_scope_name", "otel_scope_version"}

_VIC_STEP_RE = re.compile(r"^(\d+)\s*([smhdw]?)$")
_VIC_STEP_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86_400, "w": 604_800}


def _vic_parse_step(value: object) -> int | None:
    """``"5m"``/``"30s"``/``"1h"``/bare seconds → seconds; ``None`` when absent/unparseable."""

    if value is None:
        return None
    matched = _VIC_STEP_RE.match(str(value).strip().lower())
    if matched is None:
        return None
    return int(matched[1]) * _VIC_STEP_SECONDS[matched[2]]


def _vic_labels(labels: dict[str, str]) -> str:
    """``cinr_Dbm{hostname=siteA-ptp-siteB-60,site=siteA}`` — name first, plumbing dropped."""

    name = labels.get("__name__", "")
    rest = ",".join(
        f"{k}={v}"
        for k, v in sorted(labels.items())
        if k != "__name__" and k not in _VIC_NOISE_LABELS
    )
    if name and rest:
        return f"{name}{{{rest}}}"
    return name or (f"{{{rest}}}" if rest else "(no labels)")


def _victoria_query(ctx: ToolContext, args: dict[str, Any]) -> str:
    from obi.core import logstore

    if ctx.victoria_source is None or ctx.config.victoria is None:
        return _NO_VICTORIA
    denied = _budget_denied(ctx, "metrics store read")
    if denied is not None:
        return denied
    promql = str(args.get("query", "")).strip()
    if not promql:
        return "pass a PromQL query, e.g. max_over_time(weather_wind_gust_mph[24h])."
    source = ctx.victoria_source
    timeout = ctx.config.victoria.timeout

    since_arg = args.get("since")
    if since_arg is None:
        report = source.query(promql, timeout=timeout)
        if report.outcome is not SourceOutcome.REACHABLE:
            return f"{_store_name(ctx)} is unavailable: {report.detail or report.outcome}"
        if report.detail:
            return f"query error from the store: {report.detail}"
        if not report.series:
            return "no data for that query in the store."
        shown = report.series[:_VIC_MAX_RENDER_SERIES]
        lines = [f"{_vic_labels(s.labels)}: {s.samples[-1].value:.6g}" for s in shown]
        hidden = len(report.series) - len(shown)
        if hidden or report.truncated:
            extra = (
                f"…and {hidden} more matching series" if hidden else "…more matching series exist"
            )
            lines.append(f"{extra} (result truncated — narrow the selector)")
        return "\n".join(lines)

    until = logstore.parse_when(args.get("until"), UTC) or datetime.now(UTC)
    since = logstore.parse_when(since_arg, UTC) or until - timedelta(hours=24)
    span_s = (until - since).total_seconds()
    if span_s <= 0:
        return "the window is empty — 'since' must be before 'until'."
    if span_s > _VIC_MAX_RANGE_S:
        return (
            f"that window spans {span_s / 86_400:.0f} days — ranged queries are capped at "
            "31 days; narrow the range (or ask per-month)."
        )
    step_s = _vic_parse_step(args.get("step")) or int(span_s / _VIC_TARGET_POINTS)
    step_s = max(_VIC_MIN_STEP_S, step_s)
    report = source.query_range(promql, start=since, end=until, step_s=step_s, timeout=timeout)
    if report.outcome is not SourceOutcome.REACHABLE:
        return f"{_store_name(ctx)} is unavailable: {report.detail or report.outcome}"
    if report.detail:
        return f"query error from the store: {report.detail}"
    if not report.series:
        return f"no data for that query in {_fmt_span(since, until)}."
    lines = [f"{_fmt_span(since, until)}, step {step_s}s:"]
    shown_series = report.series[:_VIC_MAX_RENDER_SERIES]
    for one in shown_series:
        values = [s.value for s in one.samples]
        lines.append(
            f"{_vic_labels(one.labels)}: min {min(values):.6g} · "
            f"avg {sum(values) / len(values):.6g} · max {max(values):.6g} · "
            f"last {values[-1]:.6g} ({len(values)} points)"
        )
    hidden = len(report.series) - len(shown_series)
    if hidden or report.truncated:
        extra = f"…and {hidden} more matching series" if hidden else "…more matching series exist"
        lines.append(f"{extra} (result truncated — narrow the selector)")
    return "\n".join(lines)


def _victoria_discover(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.victoria_source is None or ctx.config.victoria is None:
        return _NO_VICTORIA
    denied = _budget_denied(ctx, "metrics store read")
    if denied is not None:
        return denied
    label = str(args.get("label") or "__name__").strip()
    match_arg = args.get("match")
    match = str(match_arg).strip() if match_arg else None
    report = ctx.victoria_source.label_values(
        label, match=match, timeout=ctx.config.victoria.timeout
    )
    if report.outcome is not SourceOutcome.REACHABLE:
        return f"{_store_name(ctx)} is unavailable: {report.detail or report.outcome}"
    if report.detail:
        return f"query error from the store: {report.detail}"
    values = sorted(report.values)
    contains = str(args.get("contains", "")).strip().lower()
    if contains:
        values = [v for v in values if contains in v.lower()]
    if not values:
        scope = f" matching {match!r}" if match else ""
        scope += f" containing {contains!r}" if contains else ""
        return f"no values for label {label!r}{scope} in the store."
    shown = values[:_VIC_MAX_VALUES]
    lines = [f"{len(values)} value(s) for {label}:", ", ".join(shown)]
    if len(values) > len(shown):
        lines.append(f"…and {len(values) - len(shown)} more (narrow with 'contains' or 'match')")
    return "\n".join(lines)


#: Exactly the station metrics the weather renderer consumes — queried by name so unrelated
#: weather_* series (sunrise/sunset astronomy, raw observation timestamps) can never crowd
#: the wind readings out of the transport budget (a live pass caught exactly that).
_VIC_WEATHER_METRICS = (
    "weather_temp_f",
    "weather_humidity_pct",
    "weather_dew_point_f",
    "weather_rain_rate_in_hr",
    "weather_rain_daily_in",
    "weather_bar_sea_level_in",
    "weather_wind_speed_mph",
    "weather_wind_gust_mph",
    "weather_wind_dir_deg",
    "weather_reading_age_seconds",
    "weather_scrape_success",
)

_VIC_COMPASS = (
    "N",
    "NNE",
    "NE",
    "ENE",
    "E",
    "ESE",
    "SE",
    "SSE",
    "S",
    "SSW",
    "SW",
    "WSW",
    "W",
    "WNW",
    "NW",
    "NNW",
)


def _vic_compass(deg: float) -> str:
    return _VIC_COMPASS[int((deg % 360) / 22.5 + 0.5) % 16]


def _weather_observed(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.victoria_source is None or ctx.config.victoria is None:
        return _NO_VICTORIA
    denied = _budget_denied(ctx, "metrics store read")
    if denied is not None:
        return denied
    report = ctx.victoria_source.query(
        '{__name__=~"' + "|".join(_VIC_WEATHER_METRICS) + '"}',
        timeout=ctx.config.victoria.timeout,
    )
    if report.outcome is not SourceOutcome.REACHABLE:
        return f"{_store_name(ctx)} is unavailable: {report.detail or report.outcome}"
    if report.detail:
        return f"query error from the store: {report.detail}"

    # Group by station; the feed publishes a display label alongside the station id.
    stations: dict[str, dict[str, float]] = {}
    names: dict[str, str] = {}
    for one in report.series:
        station = one.labels.get("station")
        metric = one.labels.get("__name__")
        if not station or not metric or not one.samples:
            continue
        stations.setdefault(station, {})[metric] = one.samples[-1].value
        names.setdefault(station, one.labels.get("label", station))
    if not stations:
        return "no weather data in the store."

    stale_s = ctx.config.victoria.weather_stale_s
    lines: list[str] = []
    body: list[str] = []
    wind_line: str | None = None
    # Stable order, wind-capable station first (only Gate Actual measures wind).
    ordered = sorted(
        stations, key=lambda s: (0 if "weather_wind_speed_mph" in stations[s] else 1, s)
    )
    for station in ordered:
        readings = stations[station]
        name = names[station]
        # The feed's own truthfulness signals come first: a failing scrape or an old reading
        # must never let stale numbers pose as current weather.
        if readings.get("weather_scrape_success") == 0.0:
            body.append(f"{name}: weather feed scrape failing — no current reading")
            continue
        age = readings.get("weather_reading_age_seconds")
        if age is not None and age > stale_s:
            lines.append(f"{name}: last reading {_health_age_text(int(age * 1_000_000), 0)} ago —")
        if wind_line is None and "weather_wind_speed_mph" in readings:
            wind = f"wind {readings['weather_wind_speed_mph']:g} mph"
            gust = readings.get("weather_wind_gust_mph")
            if gust is not None:
                wind += f" gusting {gust:g} mph"
            direction = readings.get("weather_wind_dir_deg")
            if direction is not None:
                wind += f" from {_vic_compass(direction)}"
            wind_line = f"{wind} ({name})"
        parts: list[str] = []
        if "weather_temp_f" in readings:
            parts.append(f"{readings['weather_temp_f']:g} °F")
        if "weather_humidity_pct" in readings:
            parts.append(f"humidity {readings['weather_humidity_pct']:g}%")
        if "weather_dew_point_f" in readings:
            parts.append(f"dew point {readings['weather_dew_point_f']:g} °F")
        if "weather_rain_rate_in_hr" in readings:
            rain = f"rain {readings['weather_rain_rate_in_hr']:g} in/hr"
            daily = readings.get("weather_rain_daily_in")
            if daily is not None:
                rain += f" ({daily:g} in today)"
            parts.append(rain)
        if "weather_bar_sea_level_in" in readings:
            parts.append(f"{readings['weather_bar_sea_level_in']:g} inHg")
        if parts:
            body.append(f"{name}: " + " · ".join(parts))
    if wind_line is not None:
        body.insert(0, wind_line)
    return "\n".join(lines + body) if lines or body else "no weather data in the store."


_SEVERITY_CHOICES = ["debug", "info", "warning", "error", "critical"]
_MAX_MSG = 300  # cap a rendered message so one noisy line can never blow up the answer


def _clamp_limit(value: object, logs_cfg: LogsSettings) -> int:
    """Coerce the model's ``limit`` into ``[1, max_limit]``, defaulting when absent/invalid."""

    if not isinstance(value, (int, float, str)):
        return logs_cfg.default_limit
    try:
        n = int(value)
    except (TypeError, ValueError):
        return logs_cfg.default_limit
    if n <= 0:
        return logs_cfg.default_limit
    return min(n, logs_cfg.max_limit)


def _describe_filter(host: str | None, severity: str | None, contains: str | None) -> str:
    parts: list[str] = []
    if host:
        parts.append(f"host~{host!r}")
    if severity:
        parts.append(f"severity={severity}")
    if contains:
        parts.append(f"text~{contains!r}")
    return f" ({', '.join(parts)})" if parts else ""


def _render_log_event(event: LogEvent) -> str:
    """One log line, compact and model-friendly: timestamp, host, topics, message."""

    ts = event.ts.strftime("%Y-%m-%d %H:%M:%S")
    message = event.message if len(event.message) <= _MAX_MSG else event.message[:_MAX_MSG] + "…"
    return f"- {ts} {event.host} {event.topics}: {message}"


def _logs_query(ctx: ToolContext, args: dict[str, Any]) -> str:
    from obi.core import logstore
    from obi.core.engine import query_logs

    source = ctx.log_source
    logs_cfg = ctx.config.logs
    if source is None or logs_cfg is None:
        return (
            "No device logs are configured (add a [logs] section and run `obi logs ingest` to "
            "query syslog from the collectors)."
        )

    tz = logs_cfg.tzinfo()
    host = str(args.get("host", "")).strip() or None
    contains = str(args.get("contains", "")).strip() or None
    severity = str(args.get("severity", "")).strip().lower() or None
    source_filter = str(args.get("source", "")).strip() or None
    since = logstore.parse_when(args.get("since"), tz)
    until = logstore.parse_when(args.get("until"), tz)
    limit = _clamp_limit(args.get("limit"), logs_cfg)

    # engine.query_logs writes the single `logs_query` audit record (with the operator's question
    # as intent and only counts/filters — never the log lines themselves); no second record here.
    report = query_logs(
        source,
        audit=ctx.audit,
        db_label=str(logs_cfg.db_path),
        host=host,
        since=since,
        until=until,
        severity=severity,
        contains=contains,
        source=source_filter,
        limit=limit,
        timeout=logs_cfg.timeout,
        reason=ctx.question or "chat logs query",
    )
    if report.outcome is not SourceOutcome.REACHABLE:
        detail = f" ({report.detail})" if report.detail else ""
        return f"Device logs are unavailable{detail}."
    if not report.events:
        return (
            f"No log lines matched{_describe_filter(host, severity, contains)} "
            f"(the store may simply have none for that filter; `obi logs ingest` refreshes it)."
        )

    counts = report.counts
    sev_summary = ", ".join(
        f"{k}={v}" for k, v in sorted(counts.items()) if k not in ("total", "none")
    )
    head = f"Device logs — {len(report.events)} line(s), newest first"
    if sev_summary:
        head += f" (by severity: {sev_summary})"
    lines = [head + ":"]
    lines.extend(_render_log_event(event) for event in report.events)
    if report.truncated:
        lines.append(
            f"… showing the {len(report.events)} most recent — narrow by host, severity, time "
            f"window, or text to see earlier lines."
        )
    return "\n".join(lines)


def _fmt_rf_summary(rf: object) -> str:
    """A compact RF summary for the chat rendering (e.g. ``up rssi -44 cinr 27 qam128``)."""

    from obi.core.models import RfLink

    if not isinstance(rf, RfLink):
        return "no rf data"
    state = "?" if rf.operational is None else ("up" if rf.operational else "down")
    parts = [f"rf {state}"]
    if rf.rssi_dbm is not None:
        parts.append(f"rssi {rf.rssi_dbm}")
    if rf.cinr_db is not None:
        parts.append(f"cinr {rf.cinr_db}")
    if rf.mode:
        parts.append(rf.mode.replace("adaptive ", ""))
    if rf.tx_power_dbm is not None:
        parts.append(f"txpwr {rf.tx_power_dbm}")
    if rf.air_capacity_mbps is not None:
        parts.append(f"cap {rf.air_capacity_mbps}")
    return " ".join(parts)


def _render_siklu_radio(radio: object) -> str:
    """One Siklu radio as a compact, model-friendly line; unreachable shown as its outcome."""

    from obi.core.models import SikluRadioStatus

    if not isinstance(radio, SikluRadioStatus):
        return "- (invalid radio)"
    name = radio.device.identity or radio.device.address
    if radio.reachability is not Reachability.REACHABLE:
        note = "; ".join(radio.notes) or radio.reachability.value
        return f"- Siklu {name}: {radio.reachability.value} ({note})"
    fw = radio.device.model or "?"
    rf = _fmt_rf_summary(radio.rf)
    return (
        f"- Siklu {name} @ {radio.device.address}: reachable, model={fw}, "
        f"uptime={_fmt_uptime(radio.uptime_seconds)}, temp={_fmt_temp(radio.temperature_celsius)}, "
        f"{rf}"
    )


def _fmt_temp(celsius: float | None) -> str:
    return "?" if celsius is None else f"{celsius:g}C"


def _render_siklu_link(link: object) -> str:
    """One logical link as a compact line: both ends' RF + asymmetry flags (never per-direction)."""

    from obi.core.models import LogicalLink, SikluRadioStatus

    if not isinstance(link, LogicalLink):
        return "- (invalid link)"

    def _end(end: SikluRadioStatus | None) -> str:
        if end is None:
            return "(missing)"
        name = end.device.identity or end.device.address
        if end.reachability is not Reachability.REACHABLE:
            return f"{name}={end.reachability.value}"
        return f"{name}={_fmt_rf_summary(end.rf)}"

    flags = f" [{'; '.join(link.asymmetries)}]" if link.asymmetries else ""
    return f"- {link.name}: {link.state.value} ({_end(link.end_a)} | {_end(link.end_b)}){flags}"


def _siklu_status(ctx: ToolContext, args: dict[str, Any]) -> str:
    from obi.core.engine import siklu_links, siklu_status

    driver = ctx.siklu_driver
    siklu_cfg = ctx.config.siklu
    if driver is None or siklu_cfg is None or not siklu_cfg.hosts:
        return "No Siklu radios are configured ([siklu] hosts)."

    # feature 036: per-device SSH to the Siklu roster — the most expensive read obi makes; bounded
    # by the watcher's per-run live-call budget. No-op for chat/voice/web (ctx.live_budget is None).
    denied = _budget_denied(ctx, "siklu read")
    if denied is not None:
        return denied

    name_contains = str(args.get("name_contains", "")).strip() or None
    want_links = bool(args.get("links", False))

    # engine.siklu_status writes the single `siklu_status` audit record (with the operator's
    # question as intent); we do NOT add a second record here (no double-counting). The Siklu
    # password never
    # appears in that record, this rendering, or logs.
    report = siklu_status(
        driver,
        siklu_cfg,
        audit=ctx.audit,
        name_filter=name_contains or siklu_cfg.name_filter,
        timeout=siklu_cfg.timeout,
        reason=ctx.question or "chat siklu status",
    )
    if not report.radios:
        focus = f" matching '{name_contains}'" if name_contains else ""
        return f"No Siklu radios{focus} in the roster."

    if want_links:
        links = siklu_links(report, overrides=siklu_cfg.pairs)
        if not links:
            return (
                f"{len(report.radios)} Siklu radio(s) configured, but none could be paired into "
                "a logical link (names do not follow the convention and no overrides are set)."
            )
        lines = [f"Siklu point-to-point links ({len(links)}):"]
        lines.extend(_render_siklu_link(link) for link in links)
        return "\n".join(lines)

    lines = [f"Live Siklu health ({len(report.radios)} radio(s)):"]
    lines.extend(_render_siklu_radio(radio) for radio in report.radios)
    return "\n".join(lines)


# --- registry ---------------------------------------------------------------------------------


_MAX_CFG = 6000  # cap a config dump so one big /export can't blow up the answer
_MAX_DIFF = 4000


def _config_query(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Read stored RouterOS config from the local git backup (feature 026). Gates on [configs]."""

    from obi.core.engine import config_diff, config_history, config_show

    if ctx.config_source is None or ctx.config.configs is None:
        return (
            "No config backup is configured (add a [configs] section and run `obi configs ingest` "
            "to capture device configs)."
        )
    device = str(args.get("device", "")).strip()
    if not device:
        return "Which device? Pass a device identity or address."
    mode = str(args.get("mode", "current")).strip().lower()
    reason = ctx.question or "chat config query"

    if mode == "history":
        try:
            limit = max(1, min(int(args.get("limit", 20)), 100))
        except (TypeError, ValueError):
            limit = 20
        hist = config_history(ctx.config, device, limit=limit, audit=ctx.audit, reason=reason)
        if not hist.found:
            return f"No config backup for {device!r} (use inventory_list to find the name)."
        if not hist.entries:
            return f"{device}: backed up, but no recorded change points yet."
        lines = [f"{device} — config change history (newest first):"]
        for e in hist.entries:
            when = e.captured_at.strftime("%Y-%m-%d %H:%M") if e.captured_at else "?"
            lines.append(f"- {e.commit[:12]} {when} (+{e.insertions}/-{e.deletions}) {e.summary}")
        if hist.truncated:
            lines.append(f"… more than {limit} — ask to narrow.")
        return "\n".join(lines)

    if mode == "changes":
        since = str(args.get("since", "")).strip() or "1w"
        chg = config_diff(ctx.config, device, since=since, audit=ctx.audit, reason=reason)
        if not chg.found:
            return f"No config backup for {device!r}."
        if not chg.changed:
            return f"{device}: no config changes since {since}."
        diff = chg.diff
        if len(diff) > _MAX_DIFF:
            diff = diff[:_MAX_DIFF] + "\n… (truncated — narrow the window with 'since')"
        return f"{device} — changes since {since} ({chg.from_ref} → {chg.to_ref}):\n{diff}"

    contains = str(args.get("contains", "")).strip() or None
    show = config_show(ctx.config, device, contains=contains, audit=ctx.audit, reason=reason)
    if not show.found:
        return f"No config backup for {device!r} (use inventory_list to find the name)."
    content = show.content
    if not content.strip():
        return (
            f"{device}: no config lines matched {contains!r}."
            if contains
            else f"{device}: stored config is empty."
        )
    if len(content) > _MAX_CFG:
        content = content[:_MAX_CFG] + "\n… (truncated — use 'contains' to filter to a section)"
    ver = f", RouterOS {show.router_version}" if show.router_version else ""
    flt = f" (filter {contains!r})" if contains else ""
    return f"{device} current config{flt}{ver}:\n{content}"


# --- feature 028: live read-only RouterOS access (query / ping / traceroute) --------------------


# feature 101: bounds for the audit_query tool — the hours ceiling matches the store's default
# retention (90 d), so a wider ask is a mistake worth flagging rather than an empty answer.
_AUDIT_QUERY_MAX_HOURS = 24 * 90
_AUDIT_QUERY_DEFAULT_LIMIT = 20
_AUDIT_QUERY_MAX_LIMIT = 100


def _fmt_window(hours: float) -> str:
    return f"{hours:g}h"


def _audit_query(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Query the 042 audit event store (feature 101): summaries only, newest first.

    Read-only over the local SQLite store via the existing ``query_events`` engine function
    (the same read path as ``obi audit`` and the AUD board); payloads are deliberately not
    rendered — they can be huge and are the drill-down's job, and their summaries were
    already scrubbed at write time.
    """

    from obi.core import auditstore

    try:
        hours = float(args.get("hours", 24))
    except (TypeError, ValueError):
        return "error: hours must be a number"
    if not 0 < hours <= _AUDIT_QUERY_MAX_HOURS:
        return f"error: hours must be > 0 and ≤ {_AUDIT_QUERY_MAX_HOURS} (the retention window)"
    kind = args.get("kind")
    if kind is not None and kind not in auditstore.KINDS:
        return f"error: unknown kind {kind!r} — choose from {', '.join(auditstore.KINDS)}"
    try:
        limit = int(args.get("limit", _AUDIT_QUERY_DEFAULT_LIMIT))
    except (TypeError, ValueError):
        return "error: limit must be an integer"
    if not 1 <= limit <= _AUDIT_QUERY_MAX_LIMIT:
        return f"error: limit must be between 1 and {_AUDIT_QUERY_MAX_LIMIT}"
    source = args.get("source")
    target = args.get("target")
    text = args.get("text")

    db_path = ctx.config.audit.db_path
    if not db_path.exists():
        return (
            f"no audit store yet at {db_path} — audit capture may be disabled, or nothing "
            "has been recorded on this host."
        )

    _audit(ctx, action="audit_query", target="audit-store", outcome=AuditOutcome.OK)
    conn = auditstore.connect_readonly(db_path)
    try:
        events, total = auditstore.query_events(
            conn,
            since=_utcnow() - timedelta(hours=hours),
            kinds=[str(kind)] if kind else None,
            sources=[str(source)] if source else None,
            target=str(target) if target else None,
            text=str(text) if text else None,
            limit=limit,
        )
    finally:
        conn.close()

    window = _fmt_window(hours)
    filters = ", ".join(
        f"{name}={value}"
        for name, value in (("kind", kind), ("source", source), ("target", target), ("text", text))
        if value
    )
    if not events:
        suffix = f" (filters: {filters})" if filters else ""
        return f"no matching events in the last {window}{suffix}"

    # The 013 convention: render in the operator's configured log timezone when there is one.
    tz = ctx.config.logs.tzinfo() if ctx.config.logs is not None else None
    lines: list[str] = []
    for event in events:
        try:
            moment = datetime.fromisoformat(str(event["ts"]))
            stamp = (
                moment.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")
                if tz is not None
                else moment.astimezone().strftime("%Y-%m-%d %H:%M")
            )
        except ValueError:
            stamp = str(event["ts"])
        lines.append(
            f"{stamp}  {event['kind']}  {event['source']}  {event['outcome']}  {event['summary']}"
        )
    lines.append(f"shown {len(events)} of {total} matching events (last {window})")
    return "\n".join(lines)


def _known_secrets_for(ctx: ToolContext) -> tuple[str, ...]:
    """obi's own configured secrets, for the redaction value-backstop (reuses the 026 assembler)."""

    from obi.core.engine import _known_secrets

    return _known_secrets(ctx.config)


def _parse_where(raw: object) -> list[tuple[str, str]]:
    """Parse the model's ``where`` (a list of ``"field=value"``) into equality (field, value)."""

    pairs: list[tuple[str, str]] = []
    if isinstance(raw, list):
        for item in raw:
            text = str(item)
            if "=" in text:
                key, _, value = text.partition("=")
                if key.strip():
                    pairs.append((key.strip(), value.strip()))
    return pairs


def _parse_str_list(raw: object) -> list[str]:
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    return []


def _clamp_int(value: object, *, default: int, lo: int, hi: int) -> int:
    if not isinstance(value, (int, float, str)):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, parsed))


def _resolve_live(
    ctx: ToolContext, device: str, action: str
) -> tuple[Any, MikrotikDriver | None, str]:
    """Shared gate for the live tools: (inventory entry, driver, error). Exactly one of entry/error.

    Returns ``(None, None, message)`` when there is no mikrotik driver, no inventory, or the device
    is unknown (auditing the unknown-device case); otherwise ``(entry, driver, "")``. Does NOT
    consume the live budget — callers do that only once the request is valid and will hit a device.
    """

    driver = ctx.driver
    if not isinstance(driver, MikrotikDriver):
        return None, None, _NO_MIKROTIK
    inventory = _load(ctx)
    if inventory is None:
        return None, None, _NO_INVENTORY
    entry = _find_entry(inventory, device)
    if entry is None:
        _audit(
            ctx, action=action, target=device, outcome=AuditOutcome.ERROR, detail="unknown device"
        )
        return None, None, f"No device named '{device}' is in the inventory."
    return entry, driver, ""


def _budget_denied(ctx: ToolContext, kind: str) -> str | None:
    """Consume one live-budget unit; a skip message when exhausted, else ``None`` (allowed)."""

    if ctx.live_budget is not None and not ctx.live_budget.consume():
        return f"live-call budget for this run reached — skipping this {kind}."
    return None


def _routeros_query(ctx: ToolContext, args: dict[str, Any]) -> str:
    device = str(args.get("device", "")).strip()
    path = str(args.get("path", "")).strip()
    if not device or not path:
        return "error: 'device' and 'path' are required."
    entry, driver, err = _resolve_live(ctx, device, "routeros_query")
    if driver is None:
        return err
    segments = normalize_segments(path)
    try:
        assert_read_path(segments)
    except ReadOnlyPathError as exc:
        _audit(
            ctx,
            action="routeros_query",
            target=device,
            outcome=AuditOutcome.ERROR,
            params={"path": path},
            detail=str(exc),
        )
        return f"Refused: {exc}. routeros_query is read-only — it can only read (print)."
    denied = _budget_denied(ctx, "read")
    if denied is not None:
        return denied
    live = ctx.config.routeros_live
    limit = _clamp_int(args.get("limit"), default=live.max_records, lo=1, hi=live.max_records)
    result = driver.read_path(
        entry.address,
        segments,
        where=_parse_where(args.get("where")),
        fields=_parse_str_list(args.get("fields")),
        timeout=live.timeout_s or ctx.config.timeout,
        max_records=limit,
    )
    menu = "/".join(segments)
    if result.error is not None:
        _audit(
            ctx,
            action="routeros_query",
            target=device,
            outcome=AuditOutcome.ERROR,
            params={"path": menu},
            detail=result.error,
        )
        return f"{entry.identity or entry.address} /{menu}: {result.error}"
    rows = redact_records(result.records, path=menu, extra_secrets=_known_secrets_for(ctx))
    _audit(
        ctx,
        action="routeros_query",
        target=device,
        outcome=AuditOutcome.OK,
        params={"path": menu, "rows": len(rows), "truncated": result.truncated},
    )
    return _render_rows(entry, segments, rows, result.truncated, live.max_output_chars)


def _routeros_ping(ctx: ToolContext, args: dict[str, Any]) -> str:
    device = str(args.get("device", "")).strip()
    destination = str(args.get("destination", "")).strip()
    if not device or not destination:
        return "error: 'device' and 'destination' are required."
    entry, driver, err = _resolve_live(ctx, device, "routeros_ping")
    if driver is None:
        return err
    denied = _budget_denied(ctx, "ping")
    if denied is not None:
        return denied
    live = ctx.config.routeros_live
    count = _clamp_int(args.get("count"), default=live.ping_count, lo=1, hi=live.ping_count_max)
    result = driver.ping(
        entry.address, destination, count=count, timeout=live.timeout_s or ctx.config.timeout
    )
    name = entry.identity or entry.address
    if result.error is not None:
        _audit(
            ctx,
            action="routeros_ping",
            target=device,
            outcome=AuditOutcome.ERROR,
            params={"destination": destination},
            detail=result.error,
        )
        return f"ping {destination} from {name}: {result.error}"
    _audit(
        ctx,
        action="routeros_ping",
        target=device,
        outcome=AuditOutcome.OK,
        params={"destination": destination, "sent": result.sent, "received": result.received},
    )
    return _render_ping(name, destination, result)


def _routeros_traceroute(ctx: ToolContext, args: dict[str, Any]) -> str:
    device = str(args.get("device", "")).strip()
    destination = str(args.get("destination", "")).strip()
    if not device or not destination:
        return "error: 'device' and 'destination' are required."
    entry, driver, err = _resolve_live(ctx, device, "routeros_traceroute")
    if driver is None:
        return err
    denied = _budget_denied(ctx, "traceroute")
    if denied is not None:
        return denied
    live = ctx.config.routeros_live
    result = driver.traceroute(
        entry.address,
        destination,
        max_probes=live.traceroute_max_probes,
        deadline_s=live.traceroute_deadline_s,
        timeout=live.timeout_s or ctx.config.timeout,
    )
    name = entry.identity or entry.address
    if result.error is not None:
        _audit(
            ctx,
            action="routeros_traceroute",
            target=device,
            outcome=AuditOutcome.ERROR,
            params={"destination": destination},
            detail=result.error,
        )
        return f"traceroute {destination} from {name}: {result.error}"
    _audit(
        ctx,
        action="routeros_traceroute",
        target=device,
        outcome=AuditOutcome.OK,
        params={"destination": destination, "hops": len(result.hops), "complete": result.complete},
    )
    return _render_trace(name, destination, result)


def _render_rows(
    entry: InventoryEntry,
    segments: tuple[str, ...],
    rows: list[dict[str, Any]],
    truncated: bool,
    max_chars: int,
) -> str:
    name = entry.identity or entry.address
    menu = "/" + "/".join(segments)
    if not rows:
        return f"{name} {menu}: no matching rows."
    lines = [f"{name} {menu} ({len(rows)} row(s)):"]
    for i, row in enumerate(rows, 1):
        body = ", ".join(f"{key}={value}" for key, value in row.items())
        lines.append(f"  {i}. {body}")
    if truncated:
        lines.append(f"  {len(rows)}+ rows; truncated — narrow with where=/fields=/limit")
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n  … output truncated; narrow with fields="
    return text


def _render_ping(name: str, destination: str, result: Any) -> str:
    head = (
        f"ping {destination} from {name}: sent={result.sent} received={result.received} "
        f"loss={result.loss_pct}%"
    )
    if result.rtt_avg_ms is not None:
        head += (
            f", rtt min/avg/max = {result.rtt_min_ms}/{result.rtt_avg_ms}/{result.rtt_max_ms} ms"
        )
    lines = [head]
    for probe in result.probes:
        if probe.time_ms is not None:
            ttl = f" ttl={probe.ttl}" if probe.ttl is not None else ""
            lines.append(f"  seq {probe.seq}: {probe.time_ms} ms{ttl}")
        else:
            lines.append(f"  seq {probe.seq}: {probe.status}")
    return "\n".join(lines)


def _render_trace(name: str, destination: str, result: Any) -> str:
    lines = [f"traceroute {destination} from {name}:"]
    for hop in result.hops:
        addr = hop.address or "*"
        avg = f"  {hop.avg_ms} ms" if hop.avg_ms is not None else ""
        loss = f"  loss={hop.loss_pct}%" if hop.loss_pct is not None else ""
        lines.append(f"  {hop.hop}. {addr}{avg}{loss}")
    if not result.hops:
        lines.append("  (no hops returned)")
    elif not result.complete:
        if getattr(result, "capped", False):
            lines.append("  … stopped at the probe/time cap (did not reach the destination).")
        else:
            lines.append(
                f"  … did not reach the destination (path ended after {len(result.hops)} hops)."
            )
    return "\n".join(lines)


# --- wled lighting handlers (feature 103) -----------------------------------------------------
# The engine (core/lighting.py) owns every guarantee — allowlist gate, live-stream yield,
# pre-change snapshot, dry-run, audit records with the overwritten state. These handlers are
# the thin surface glue the Layered Architecture principle asks for.


def _wled_lighting(ctx: ToolContext) -> Lighting:
    from obi.core.lighting import Lighting

    assert ctx.wled_source is not None  # handlers gate before calling
    return Lighting(ctx.wled_source.settings, ctx.audit)


def _wled_status(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.wled_source is None:
        return _NO_WLED
    device = args.get("device")
    result = _wled_lighting(ctx).status(str(device) if device else None)
    _audit(
        ctx,
        action="wled_status",
        target=str(device) if device else "wled",
        outcome=AuditOutcome.OK,
        params={},
    )
    return result


def _wled_set(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.wled_source is None:
        return _NO_WLED
    device = args.get("device")

    def _pct(key: str) -> int | None:
        value = args.get(key)
        return int(value) if value is not None else None

    power = args.get("power")
    return _wled_lighting(ctx).set_look(
        str(device) if device else None,
        color=str(args["color"]) if args.get("color") else None,
        color2=str(args["color2"]) if args.get("color2") else None,
        color3=str(args["color3"]) if args.get("color3") else None,
        effect=str(args["effect"]) if args.get("effect") else None,
        palette=str(args["palette"]) if args.get("palette") else None,
        brightness_pct=_pct("brightness_pct"),
        speed_pct=_pct("speed_pct"),
        intensity_pct=_pct("intensity_pct"),
        custom1_pct=_pct("custom1_pct"),
        custom2_pct=_pct("custom2_pct"),
        custom3_pct=_pct("custom3_pct"),
        power=bool(power) if power is not None else None,
        dry_run=bool(args.get("dry_run", False)),
        reason=ctx.question,
        actor=ctx.actor,
    )


def _wled_revert(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.wled_source is None:
        return _NO_WLED
    device = args.get("device")
    return _wled_lighting(ctx).revert(
        str(device) if device else None,
        dry_run=bool(args.get("dry_run", False)),
        reason=ctx.question,
        actor=ctx.actor,
    )


if TYPE_CHECKING:
    from obi.core.lighting import Lighting


# feature 111: the mesh_locate tool — a Meshtastic node's location as a city address, from
# the radio attached over USB. The engine (core/mesh.py) owns every decision: forgiving
# name resolution, passive-first freshness, the durable transmission rate limiter, and the
# survey-cache translation. Read-only classification is deliberate (spec FR-006): the one
# transmitting call is a position QUERY — the ping/traceroute class — and no autonomous
# path reaches it (mesh_locate is NOT in WATCH_TOOL_NAMES; test_watch_toolset pins that).
_NO_MESH = "Mesh lookup isn't configured — add a [meshtastic] section to obi's config."


def _mesh_locate(ctx: ToolContext, args: dict[str, Any]) -> str:
    node = str(args.get("node", "")).strip()
    if not node:
        return "error: 'node' is required."
    if ctx.mesh_source is None:
        return _NO_MESH
    # Consumed up front: even the node-DB read contacts the attached radio (the
    # routeros_ping contract — one budget unit per device-contacting call).
    denied = _budget_denied(ctx, "mesh lookup")
    if denied is not None:
        return denied
    from obi.core.brcaddr import model_loader_for

    locator = MeshLocator(
        ctx.mesh_source.settings, ctx.audit, model_loader=model_loader_for(ctx.config.brc)
    )
    return locator.locate(node, reason=ctx.question or "chat query", actor=ctx.actor)


# feature 127: the playa_locate tool — any published camp or art installation, from the
# locally downloaded city datasets (art + camps). A pure local-file read: contacts no
# device and never the internet (the one egress is the operator's `obi playa fetch`).
def _playa_locate(ctx: ToolContext, args: dict[str, Any]) -> str:
    name = str(args.get("name", "")).strip()
    if not name:
        return "error: 'name' is required."
    from obi.core.config import default_playa_state_path

    # One art.json serves both the mesh landmarks and this lookup: honor a configured
    # [meshtastic] art_path override, else the shared playa default (127 D6).
    art_path = (
        ctx.config.meshtastic.art_path
        if ctx.config.meshtastic is not None
        else default_playa_state_path() / "art.json"
    )
    from obi.core.brcaddr import model_loader_for

    locator = PlayaLocator(
        art_path=art_path,
        camp_path=default_playa_state_path() / "camp.json",
        model_loader=model_loader_for(ctx.config.brc),
    )
    return locator.locate(name)


def _weather_forecast(ctx: ToolContext, args: dict[str, Any]) -> str:
    """``_weather_forecast_impl`` under the ``[site]`` zone, even when called directly (137)."""

    return _with_site_tz(ctx, _weather_forecast_impl, args)


def _gate_traffic(ctx: ToolContext, args: dict[str, Any]) -> str:
    """``_gate_traffic_impl`` under the ``[site]`` zone, even when called directly (137)."""

    return _with_site_tz(ctx, _gate_traffic_impl, args)


def build_tools() -> list[ToolDefinition]:
    """The read-only tools, in a stable order. This is the ENTIRE agent toolset.

    They wrap the Mikrotik core (roster + live reads), the live UISP controller read (006),
    live SNMP metrics (012, ``snmp_metrics``), and ingested device syslog (013, ``logs_query``).
    All are read-only, so read-only is guaranteed by construction (Principle I).

    Not every registered tool reaches every surface: the conversational surfaces take
    :func:`build_chat_tools` (registry minus ``CHAT_EXCLUDED_TOOL_NAMES``), the watcher and
    radio take their own static name lists below, and only the MCP surface serves this list
    verbatim (``mcp/server.py::served_tools``).
    """

    return [
        ToolDefinition(
            name="inventory_list",
            description=(
                "List devices from the persisted inventory roster (cached, not live). Use for "
                "'what devices exist / what is known' questions. Optional filters."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "discovery_state": {
                        "type": "string",
                        "enum": [
                            "identified",
                            "unreachable",
                            "auth_failed",
                            "timed_out",
                            "unsupported",
                            "unidentified",
                            "any",
                        ],
                        "default": "any",
                    },
                    "identity_contains": {
                        "type": "string",
                        "description": "Case-insensitive substring match on identity/address.",
                    },
                    "present_only": {
                        "type": "boolean",
                        "default": True,
                        "description": "Only devices present in the latest crawl.",
                    },
                },
            },
            handler=_inventory_list,
        ),
        ToolDefinition(
            name="inventory_summary",
            description=(
                "Counts and a summary of the inventory (totals by state, present/absent, last "
                "refresh time). Cached, not live."
            ),
            input_schema={"type": "object", "properties": {}},
            handler=_inventory_summary,
        ),
        ToolDefinition(
            name="routeros_status",
            description=(
                "Fetch LIVE health (reachability, uptime, CPU load, free memory) for a named "
                "device or for all identified devices. Use for 'what is happening now / is X up'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Device address or identity, or 'all' for the roster.",
                    }
                },
                "required": ["target"],
            },
            handler=_routeros_status,
        ),
        ToolDefinition(
            name="routeros_neighbors",
            description=(
                "Read a device's neighbor-discovery table (adjacent devices) live. Use for "
                "topology / 'what is near X' questions."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "Device address or identity."}
                },
                "required": ["target"],
            },
            handler=_routeros_neighbors,
        ),
        ToolDefinition(
            name="uisp_status",
            description=(
                "Fetch LIVE health for UISP-managed devices from the UISP controller (one read "
                "covers the whole fleet). Use for any question about UISP devices, and for the "
                "UISP half of whole-fleet questions ('what's down?', 'summarize the network'). "
                "UISP is read live — there is no cached UISP roster and no UISP "
                "neighbor/topology. Optional name filter."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name_contains": {
                        "type": "string",
                        "description": (
                            "Case-insensitive substring filter on UISP device name. Omit for the "
                            "whole fleet; pass a device name to focus on one device or a large "
                            "fleet."
                        ),
                    }
                },
            },
            handler=_uisp_status,
        ),
        ToolDefinition(
            name="uisp_links",
            description=(
                "Fetch LIVE wireless link/backhaul RF health from the UISP controller: per-link "
                "state, signal, per-end link scores and radio signals, frequency, distance, and "
                "theoretical capacity (two reads cover every link). Use for questions about a "
                "backhaul or point-to-point/point-to-multipoint link ('how's the siteA "
                "backhaul?'), weak or down links, or link quality — NOT device CPU/uptime "
                "(uisp_status). Read-only. Optional name filter matches either endpoint."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name_contains": {
                        "type": "string",
                        "description": (
                            "Case-insensitive substring matched against either endpoint device "
                            "name. Omit for all wireless links."
                        ),
                    }
                },
            },
            handler=_uisp_links,
        ),
        # unifi_status (feature 011) DISABLED registry-wide — operator decision 2026-08-26:
        # playa wifi is another team's system, so obi answers no live-UniFi questions on ANY
        # surface for now (the posture that already kept it off radio/watch, widened to the
        # whole registry). The `_unifi_status` handler, engine read, `obi unifi` CLI, UNIFI
        # board, and UniFi log ingest all remain; to re-enable, re-add the ToolDefinition here
        # (schema in git history) — the per-surface lists below never carried it.
        ToolDefinition(
            name="snmp_metrics",
            description=(
                "Read LIVE SNMP metrics for devices obi already knows (Mikrotik roster or "
                "NetBox-documented hosts). Use for interface traffic/counters, per-port "
                "operational status, and SNMP-reported CPU/memory/uptime. These are live readings "
                "(NOT the NetBox documented record). For RouterOS-native Mikrotik health prefer "
                "routeros_status; use this for SNMP-specific or non-Mikrotik device metrics. "
                "Choose the 'system' profile (name/uptime/CPU/memory) or 'interfaces' profile "
                "(per-interface status and in/out traffic counters)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Device address or identity, or 'all' for the known fleet.",
                    },
                    "profile": {
                        "type": "string",
                        "enum": ["system", "interfaces"],
                        "default": "system",
                        "description": "Metric group: 'system' health or 'interfaces' traffic.",
                    },
                    "source": {
                        "type": "string",
                        "enum": ["mikrotik", "netbox", "all"],
                        "default": "all",
                        "description": "Which roster supplies targets.",
                    },
                },
                "required": ["target"],
            },
            handler=_snmp_metrics,
        ),
        ToolDefinition(
            name="metrics_history",
            description=(
                "Read obi's STORED metric history for a device and report it as RATES over a "
                "time window — how much traffic an interface actually carried, in bits per "
                "second, with the average and the peak. Use this for any 'how much / how busy / "
                "how fast / utilization / over the last hour|day|week' question about traffic, "
                "and for the trend of interface errors, CPU, memory or temperature. Reads a "
                "LOCAL store — it contacts no device, works when the network is down, and is "
                "cheap. Prefer it over snmp_metrics for anything about throughput: snmp_metrics "
                "returns cumulative counters from a single moment, which cannot express a rate. "
                "Use snmp_metrics instead for present-moment port state, or for a device obi "
                "collects no history for. Omit 'metric' to get both traffic directions at once; "
                "omit 'interface' to see which interfaces are busiest."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "device": {
                        "type": "string",
                        "description": "Device identity, e.g. 'site-gw-1'.",
                    },
                    "interface": {
                        "type": "string",
                        "description": (
                            "Optional interface, matched as a substring of its name (e.g. "
                            "'uplink' finds 'sfp-sfpplus1-uplink'). Omit to rank every "
                            "interface busiest-first."
                        ),
                    },
                    "metric": {
                        "type": "string",
                        "enum": [
                            "ifHCInOctets",
                            "ifHCOutOctets",
                            "ifInErrors",
                            "ifOutErrors",
                            "ifInDiscards",
                            "ifOutDiscards",
                            "cpuLoad",
                            "cpuTemperature",
                            "boardTemperature",
                            "memUsedKb",
                            "memFreeKb",
                            "memTotalKb",
                        ],
                        "description": (
                            "Optional single metric. Omit for traffic (both octet directions "
                            "together) — the usual choice."
                        ),
                    },
                    "since": {
                        "type": "string",
                        "description": (
                            "Start of the window: a lookback ('2h', '7d') or an ISO date/time. "
                            "Defaults to 24 hours ago."
                        ),
                    },
                    "until": {
                        "type": "string",
                        "description": "End of the window (lookback or ISO); defaults to now.",
                    },
                },
                "required": ["device"],
                "additionalProperties": False,
            },
            handler=_metrics_history,
        ),
        ToolDefinition(
            name="query_health",
            description=(
                "Report the fleet's CURRENT hardware-health state from obi's local health "
                "store: temperatures, PSU/fan status, SFP optics power, CPU/memory pressure, "
                "and the collector VMs' disk usage — with breach verdicts from the SAME "
                "thresholds the health watcher alerts on. Use this for 'how hot is X', 'any "
                "PSU/fan/optics problems', 'is anything unhealthy', 'how full are the "
                "collectors' disks'. Omit 'device' for a worst-first fleet summary; name a "
                "device for its full readings. Reads a LOCAL store — contacts no device, "
                "works when the network is down, cheap. For TRENDS over time ('is it getting "
                "hotter'), use metrics_history with a health:* metric instead; for "
                "present-moment interface/port state, use snmp_metrics."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "device": {
                        "type": "string",
                        "description": (
                            "Optional device identity (e.g. 'site-gw-1' or 'collector-1'); "
                            "shorthand resolves against devices with health data. Omit for "
                            "the fleet summary."
                        ),
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
            handler=_query_health,
        ),
        ToolDefinition(
            name="dash_status",
            description=(
                "The network's CURRENT overall status from obi's status dashboard — the "
                "evaluated board the operator trusts, refreshed every minute: fleet "
                "reachability (ICMP), router-to-router netwatch, reference pings, internet "
                "uplinks, and data-pipeline freshness, rolled up green/yellow/red with "
                "per-item detail and how long each problem has stood. This is the FIRST "
                "tool for any overall health/status question — 'what is the current "
                "status', 'is everything OK', 'how's the network', 'what's down right "
                "now': one call answers it. Reads a LOCAL file — contacts no device, works "
                "when the network is down, cheap. Pass 'signal' to list one tile's items. "
                "Answers state the board's age and say plainly when the board is stale or "
                "the dashboard is not running. For drill-downs on a SPECIFIC device or "
                "question, follow up with the domain tools (routeros_status/uisp_status "
                "live state, query_health hardware, logs_query history, metrics_history "
                "traffic)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "signal": {
                        "type": "string",
                        "description": (
                            "Optional: one signal to drill into — fleet, netwatch, ping, "
                            "internet, or pipeline. Lists ALL of that tile's items (green "
                            "included). Omit for the overall summary."
                        ),
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
            handler=_dash_status,
        ),
        ToolDefinition(
            name="weather_forecast",
            requires=("weather",),
            description=(
                "Weather for {weather_place} from obi's LOCAL weather store (hourly "
                "snapshots of the Open-Meteo forecast plus official NWS alerts for the "
                "configured zone). Omit 'at' for: current conditions, active NWS alerts "
                "(High Wind / Dust Storm Warnings etc.), and the raw hour-by-hour numbers for "
                "the next 24 hours — temperature, humidity, dew point, precipitation "
                "amount+probability, wind speed/direction/gusts. Pass 'at' (a past site-local "
                "time) for what conditions were recorded around that time. Raw numbers with "
                "units and data age — no interpretation. Reads a LOCAL store: contacts "
                "nothing, works with the uplink down (the data age tells you how stale), "
                "cheap. Covers the configured location only. For measured station "
                "observations from {store}, weather_observed is the second-tier complement."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "at": {
                        "type": "string",
                        "description": (
                            "Optional PAST time for a historical lookup, playa-local, "
                            "'YYYY-MM-DD HH:MM' (e.g. '2026-08-27 02:00'). Omit for current "
                            "conditions + alerts + the 24-hour outlook."
                        ),
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
            handler=_weather_forecast,
        ),
        ToolDefinition(
            name="gate_traffic",
            requires=("traffic",),
            description=(
                "Event gate travel times and gate status from obi's LOCAL traffic store "
                "(5-minute snapshots of the event dashboard's Travel Time Log — the "
                "published reports like 'Gate: 90 min' — plus the dashboard's "
                "ingress/egress gate status). Use for 'how long is the gate wait', 'how slow "
                "is gate', 'what's the wait time for gate', 'is the gate open', 'is ingress "
                "paused'. Serves the newest published report with its time and age, recent "
                "earlier reports for trend, and the lanes' open/closed state + advisory — "
                "all VERBATIM as published, with the last-check age; it never computes or "
                "predicts a wait. Reads a LOCAL store: contacts nothing, works with the "
                "uplink down (the ages tell you how stale), cheap. Before the event the "
                "dashboard publishes nothing — the tool says so honestly."
            ),
            input_schema={
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
            handler=_gate_traffic,
        ),
        ToolDefinition(
            name="backbone_rf_health",
            requires=("victoria",),
            description=(
                "RF health of the BACKBONE point-to-point radios from {store} (a "
                "second-tier source: for devices obi collects itself prefer obi's own "
                "metrics_history/snmp_metrics/query_health; use this for what obi doesn't "
                "collect). Reports a radio's current signal quality (CINR/RSSI), modulation, "
                "TX power, capacity and link state, with a min–max trend over a window. Use "
                "for 'how's the siteA–siteB 60 GHz link', 'RF at a site', or to cross-check "
                "obi's own view. Name one radio (e.g. 'siteA-ptp-siteB-60') or a site name "
                "for every backbone radio there. Contacts the store over HTTP."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": (
                            "Backbone radio hostname ('siteA-ptp-siteB-60'), a site name "
                            "('bear'), or a unique substring. Ambiguity returns the "
                            "candidate list."
                        ),
                    },
                    "window": {
                        "type": "string",
                        "description": (
                            "Trend window: a lookback ('2h', '7d') or ISO date/time. "
                            "Defaults to the configured rf_window (normally 24h)."
                        ),
                    },
                },
                "required": ["target"],
                "additionalProperties": False,
            },
            handler=_backbone_rf_health,
        ),
        ToolDefinition(
            name="victoria_query",
            requires=("victoria",),
            description=(
                "Free-form PromQL against {store} (second-tier: for devices obi collects "
                "itself prefer obi's own metrics_history/snmp_metrics/query_health). The "
                "store holds what obi doesn't collect: backbone radio RF, the weather "
                "stations (weather_*), fleet optics (iface_optical_*), and Linux hosts "
                "(system_*), at 15 s resolution with 12-month retention. Omit 'since' for an "
                "instant query (current values); pass 'since' for a ranged query summarized "
                "per series as min/avg/max/last (span capped at 31 days). Unsure of metric or "
                "label names? Call victoria_discover first — grounded names beat guessed "
                "ones. Contacts the store over HTTP."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "PromQL, e.g. max_over_time(weather_wind_gust_mph[24h]) or "
                            "iface_optical_rx_power_Dbm < -10."
                        ),
                    },
                    "since": {
                        "type": "string",
                        "description": (
                            "Start of a RANGE query: a lookback ('2h', '7d') or ISO "
                            "date/time. Omit for an instant query."
                        ),
                    },
                    "until": {
                        "type": "string",
                        "description": "End of the range (lookback or ISO); defaults to now.",
                    },
                    "step": {
                        "type": "string",
                        "description": (
                            "Range resolution like '5m' or '30s'. Default auto (~200 "
                            "points); floor 15s."
                        ),
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            handler=_victoria_query,
        ),
        ToolDefinition(
            name="victoria_discover",
            requires=("victoria",),
            description=(
                "Discover what {store} holds: enumerate metric names (the "
                "default) or the values of any label (hostname, site, board, station, "
                "iface), optionally scoped by a series matcher and a substring filter. Use "
                "this BEFORE composing a victoria_query when unsure of exact names — "
                "the store's vocabulary is custom (cinr_Dbm, weather_wind_gust_mph, "
                "iface_optical_rx_power_Dbm, …). Cheap, read-only, capped at 200 values."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "description": (
                            "Label to enumerate; default '__name__' (= metric names). "
                            "E.g. 'hostname', 'site', 'board', 'station'."
                        ),
                    },
                    "match": {
                        "type": "string",
                        "description": (
                            'Optional series matcher to scope, e.g. {role="ptp-rf"} or '
                            "weather_temp_f."
                        ),
                    },
                    "contains": {
                        "type": "string",
                        "description": "Optional case-insensitive substring filter on the values.",
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
            handler=_victoria_discover,
        ),
        ToolDefinition(
            name="weather_observed",
            requires=("victoria",),
            description=(
                "Current weather from {store}'s station feeds (not every station reports "
                "wind): temperature, wind speed/gusts/direction, humidity, dew point, rain, "
                "pressure, with the reading's age. This is the OBSERVED record ('what is/was "
                "the wind actually doing') — wind and gust numbers matter for tower climbs; "
                "for the FORECAST, the next 24 hours, or official NWS alerts use "
                "weather_forecast instead. Takes no arguments. Contacts the store over HTTP."
            ),
            input_schema={
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
            handler=_weather_observed,
        ),
        ToolDefinition(
            name="logs_query",
            description=(
                "Search ingested DEVICE SYSLOG (RouterOS log lines the syslog-ng collectors "
                "captured, mirrored and indexed locally). Use for 'what did X log', 'show me "
                "errors/DHCP/OSPF events', 'what happened around <time>' questions. This is "
                "HISTORICAL log text, not live health and not the inventory: for 'is X up right "
                "now' use routeros_status/uisp_status. Returns the most recent matching lines "
                "(newest first), bounded — narrow with the filters. All filters optional; with "
                "none, returns the most recent lines across all hosts."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "host": {
                        "type": "string",
                        "description": (
                            "Case-insensitive substring of the device/host name (e.g. 'site-gw-1' "
                            "or 'chestnut' for all chestnut devices)."
                        ),
                    },
                    "severity": {
                        "type": "string",
                        "enum": _SEVERITY_CHOICES,
                        "description": "Only lines at this RouterOS severity.",
                    },
                    "contains": {
                        "type": "string",
                        "description": "Full-text phrase to match within the log message.",
                    },
                    "source": {
                        "type": "string",
                        "description": (
                            "Filter by log source family: 'syslog' = device syslog only (RouterOS "
                            "log lines from every collector host, no controller events), 'unifi' = "
                            "UniFi controller events, 'uisp' = UISP controller events. A syslog "
                            "collector hostname/substring also works — use it to isolate UniFi "
                            "events for a site whose name also matches RouterOS hosts (e.g. Oak)."
                        ),
                    },
                    "since": {
                        "type": "string",
                        "description": (
                            "Start of the time window: a relative lookback ('1h', '30m', '2d', "
                            "'1w') or an ISO date/time ('2026-06-03' or '2026-06-03T10:00')."
                        ),
                    },
                    "until": {
                        "type": "string",
                        "description": "End of the time window (same formats as 'since').",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max lines to return (newest first); bounded by the store.",
                    },
                },
            },
            handler=_logs_query,
        ),
        ToolDefinition(
            name="siklu_status",
            description=(
                "Read-only: report Siklu EtherHaul point-to-point radios from the configured "
                "roster — identity, system health (uptime, temperature), RF link health (up/down, "
                "RSSI, CINR, modulation, TX power, capacity), and Ethernet-port state. Set "
                "links=true to view each hop as one logical link (both ends, asymmetry flags). "
                "Optional name_contains narrows the radios. Use this for any question about Siklu "
                "radios/links, and for the Siklu part of whole-fleet questions ('what's down?')."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name_contains": {
                        "type": "string",
                        "description": "case-insensitive substring filter on radio name",
                    },
                    "links": {
                        "type": "boolean",
                        "description": "group the two ends of each hop into a logical link",
                    },
                },
                "additionalProperties": False,
            },
            handler=_siklu_status,
        ),
        ToolDefinition(
            name="config_query",
            description=(
                "Read stored RouterOS device CONFIGURATION from obi's local git backup — the "
                "current config and its change history (redacted: no secrets). Use for 'show X's "
                "config / firewall / NAT rules', 'what changed on X's config', 'when did X's "
                "config last change'. This is device CONFIG (versioned /export), NOT live health "
                "(routeros_status) and NOT syslog (logs_query). Reads the backup; no device "
                "contact."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "device": {
                        "type": "string",
                        "description": "Device identity or address, e.g. 'core-gw-1'.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["current", "history", "changes"],
                        "default": "current",
                        "description": (
                            "'current' = latest stored config; 'history' = list of change points; "
                            "'changes' = the diff over a time window."
                        ),
                    },
                    "contains": {
                        "type": "string",
                        "description": (
                            "With mode=current, return only config lines matching this text (e.g. "
                            "'firewall', 'nat', 'ospf'). Keeps replies small."
                        ),
                    },
                    "since": {
                        "type": "string",
                        "description": (
                            "With mode=changes/history, the lookback window: '1w', '30d', or an "
                            "ISO date. Defaults to '1w' for changes."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": "With mode=history, max change points (newest first).",
                    },
                },
                "required": ["device"],
                "additionalProperties": False,
            },
            handler=_config_query,
        ),
        # feature 101: obi's own history. A local store read that contacts nothing — like
        # metrics_history/config_query it is NOT in BUDGETED_LIVE_TOOL_NAMES. Operator decision
        # 2026-08-26 (feature 105): the trail is operator-only disclosure, and the operator
        # reads it from their own Claude — so this tool is MCP-ONLY. It is excluded from the
        # conversational surfaces via CHAT_EXCLUDED_TOOL_NAMES, and deliberately NOT in
        # WATCH_TOOL_NAMES or RADIO_OPERATOR_TOOL_NAMES (the watcher has no business reading
        # its own trail mid-investigation; RF carries no authentication).
        ToolDefinition(
            name="audit_query",
            description=(
                "Query obi's own AUDIT TRAIL — what obi did and when: every LLM call "
                "(llm_call), tool call (tool_call), device command (device_command), human "
                "message in/out (human_message), delivered report/alert (report), and engine "
                "action (action), recorded from every surface (chat, slack, voice, watch, web, "
                "radio, mcp, cli:*). Returns one-line summaries newest-first plus a total "
                "count; full payloads stay in `obi audit show` / the AUD board. Use for 'what "
                "did obi do overnight', 'when did the watcher last alert about X', 'what "
                "touched device X'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "hours": {
                        "type": "number",
                        "description": "Look-back window in hours (default 24, max 2160).",
                    },
                    "kind": {
                        "type": "string",
                        "enum": [
                            "llm_call",
                            "tool_call",
                            "device_command",
                            "human_message",
                            "report",
                            "action",
                        ],
                        "description": "One event kind; omit for all.",
                    },
                    "source": {
                        "type": "string",
                        "description": (
                            "Surface tag filter, e.g. chat, slack, watch, web, radio, mcp, "
                            "cli:status."
                        ),
                    },
                    "target": {
                        "type": "string",
                        "description": "Device/host filter, as recorded on events.",
                    },
                    "text": {
                        "type": "string",
                        "description": "Full-text search over event summaries and targets.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max events returned (default 20, max 100).",
                    },
                },
                "additionalProperties": False,
            },
            handler=_audit_query,
        ),
        ToolDefinition(
            name="routeros_query",
            description=(
                "Read LIVE data from a RouterOS device: issues a read (print) of any RouterOS menu "
                "path (e.g. 'interface', 'ip/route', 'ip/firewall/filter', "
                "'interface/wireless/registration-table') with optional server-side filtering and "
                "field selection. READ-ONLY — it can only read, never change the device; "
                "state-changing paths (set/add/remove/reboot/run/fetch/...) are refused. Use this "
                "for anything the other tools don't already cover."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "device": {
                        "type": "string",
                        "description": "Device identity or address (must be in the inventory).",
                    },
                    "path": {
                        "type": "string",
                        "description": (
                            "RouterOS menu path, e.g. 'ip/route' or '/interface'. A trailing "
                            "'/print' is optional."
                        ),
                    },
                    "where": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional equality filters, each 'field=value' (e.g. 'running=true', "
                            "'disabled=false'); ANDed, applied on the device."
                        ),
                    },
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional field names to return (e.g. ['name','running','comment']). "
                            "Fewer fields = shorter reply."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max rows to return (clamped to the configured maximum).",
                    },
                },
                "required": ["device", "path"],
                "additionalProperties": False,
            },
            handler=_routeros_query,
        ),
        ToolDefinition(
            name="routeros_ping",
            description=(
                "Run a bounded ICMP ping FROM a RouterOS device to a destination and report "
                "per-probe results plus a sent/received/loss and RTT summary. Read-only diagnostic."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "device": {
                        "type": "string",
                        "description": "Source device (identity or address, in the inventory).",
                    },
                    "destination": {
                        "type": "string",
                        "description": "Target host/IP to ping from that device.",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of probes (default 4; clamped to the max).",
                    },
                },
                "required": ["device", "destination"],
                "additionalProperties": False,
            },
            handler=_routeros_ping,
        ),
        ToolDefinition(
            name="routeros_traceroute",
            description=(
                "Trace the network path FROM a RouterOS device to a destination and report the "
                "ordered hops (address + latency). Read-only diagnostic; bounded."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "device": {
                        "type": "string",
                        "description": "Source device (identity or address, in the inventory).",
                    },
                    "destination": {
                        "type": "string",
                        "description": "Target host/IP to trace toward.",
                    },
                },
                "required": ["device", "destination"],
                "additionalProperties": False,
            },
            handler=_routeros_traceroute,
        ),
        # feature 103: the read-only half of the lighting feature lives in the registry, so it
        # reaches every surface (MCP by derivation, radio by its pinned list) with zero surface
        # work. The WRITE tools deliberately do NOT live here — see build_operator_tools().
        ToolDefinition(
            name="wled_status",
            requires=("wled",),
            description=(
                "Current state of a configured LED lighting device{lighting}: power, "
                "brightness, color/effect, palette, effect speed/intensity, whether a live "
                "show is in control, power draw, uptime, wifi signal, and whether a revert "
                "snapshot is held. Read-only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "device": {
                        "type": "string",
                        "description": (
                            "Configured lighting device name; optional when exactly one is "
                            "configured."
                        ),
                    },
                },
                "additionalProperties": False,
            },
            handler=_wled_status,
        ),
        # feature 111 (passive since 125): mesh-node location. The lookup itself never
        # transmits — the mesh daemon keeps tracked positions fresh on its own schedule.
        ToolDefinition(
            name="mesh_locate",
            requires=("meshtastic",),
            description=(
                "Where is a tracked Meshtastic mesh node{mesh_example}? Mesh nodes are the "
                "only things carrying a tracker: camps and art installations are NOT mesh "
                "nodes, so never call this for them (use playa_locate and answer from it "
                "alone). Resolves the node by name against the radio attached to obi's "
                "machine and answers with its last known position — a city address (like "
                "'6:15 & A' or '6:15 and 2,670 feet from the Man') when the city plan is "
                "configured, else coordinates — referencing a nearby art installation when "
                "the downloaded art dataset covers it; stale positions carry their honest "
                "age. Answers instantly from continuously refreshed data (the mesh daemon "
                "requests fresh positions automatically); the lookup itself never "
                "transmits. Read-only; changes nothing on any device."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "node": {
                        "type": "string",
                        "description": (
                            "Mesh node to locate: short name, long name or part of one "
                            "(e.g. 'tracker-1'), node id (e.g. '!1234abcd'), or an "
                            "operator-configured "
                            "alias like 'art car'. Case-insensitive — pass the caller's "
                            "words as given."
                        ),
                    },
                },
                "required": ["node"],
                "additionalProperties": False,
            },
            handler=_mesh_locate,
        ),
        # feature 127: any published camp or art installation → its location, from the
        # locally downloaded city datasets. Contacts nothing at answer time.
        ToolDefinition(
            name="playa_locate",
            requires=("brc",),
            description=(
                "Where is an event camp or art installation? Resolves the name "
                "against the locally downloaded city datasets (this year's placed "
                "camps and art) and answers with its location — a camp's published "
                "address (like 'G & 9:15', plus a look-for landmark when one is "
                "published) or an art piece's city address. Forgiving name "
                "matching; when a few places tie, the answer already lists each one's "
                "location — relay them all, never ask the caller to pick first. Reads "
                "local data only — never the internet. Read-only; changes "
                "nothing anywhere. Camps and art are placed, not tracked: this answer "
                "is complete by itself — do not also call mesh_locate for them, and "
                "never tell the caller a camp or art piece 'has no tracker' (only "
                "tracked mesh nodes have one; for those use mesh_locate)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "Camp or art installation to find, as the caller said it "
                            "('Camp Soft Landing', 'the temple', 'Electric "
                            "Dandelion'). Case-insensitive; partial names are fine."
                        ),
                    },
                },
                "required": ["name"],
                "additionalProperties": False,
            },
            handler=_playa_locate,
        ),
    ]


# feature 103: the operator toolset — the registry plus obi's ONLY mutating tools. Consumed
# verbatim by the MCP server (2.0.0) and, via the pinned tier tuples below, the radio (2.0.0,
# any caller); the conversational runtime consumes it through build_chat_tools (minus the
# 105 chat exclusions). The web tool route and the watcher keep consuming
# build_tools()/their pinned subsets and stay write-free BY CONSTRUCTION
# (test_registry_readonly.py).


def _wled_write_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="wled_set",
            requires=("wled",),
            description=(
                "CHANGE a configured LED lighting device{lighting}: any mix of color(s), "
                "COLOR PALETTE (the color scheme an effect draws from — a name from the "
                "device's palette catalog, e.g. Icefire), a pattern/effect from the device's "
                "effect catalog, effect speed/intensity percent, brightness percent, or power "
                "on/off. A request for a 'palette' means the palette parameter, never effect. "
                "The state being overwritten is captured first and restorable via "
                "wled_revert. Set dry_run=true to preview without changing anything. Refuses "
                "while a live show is driving the device."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "device": {
                        "type": "string",
                        "description": (
                            "Configured lighting device name; optional when exactly one is "
                            "configured."
                        ),
                    },
                    "color": {
                        "type": "string",
                        "description": "#RRGGBB hex or a common color name (e.g. purple).",
                    },
                    "color2": {
                        "type": "string",
                        "description": (
                            "Secondary/background color slot: #RRGGBB hex or a common color "
                            "name. Unset slots keep their current colors."
                        ),
                    },
                    "color3": {
                        "type": "string",
                        "description": "Tertiary color slot: #RRGGBB hex or a common color name.",
                    },
                    "effect": {
                        "type": "string",
                        "description": "Pattern name from the device's effect catalog.",
                    },
                    "palette": {
                        "type": "string",
                        "description": (
                            "Color palette name from the device's palette catalog (e.g. "
                            "Icefire, Sunset). Near-miss names resolve; a miss suggests the "
                            "closest catalog names."
                        ),
                    },
                    "brightness_pct": {"type": "integer", "minimum": 0, "maximum": 100},
                    "speed_pct": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100,
                        "description": "Effect speed as a percent.",
                    },
                    "intensity_pct": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100,
                        "description": "Effect intensity as a percent.",
                    },
                    "custom1_pct": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100,
                        "description": "Advanced: effect-specific slider 1 (meaning varies).",
                    },
                    "custom2_pct": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100,
                        "description": "Advanced: effect-specific slider 2 (meaning varies).",
                    },
                    "custom3_pct": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100,
                        "description": "Advanced: effect-specific slider 3 (meaning varies).",
                    },
                    "power": {"type": "boolean"},
                    "dry_run": {
                        "type": "boolean",
                        "description": "Preview exactly what would change; writes nothing.",
                    },
                },
                "additionalProperties": False,
            },
            handler=_wled_set,
            risk_class=LOW_RISK,
            mutating=True,
        ),
        ToolDefinition(
            name="wled_revert",
            requires=("wled",),
            description=(
                "Restore a lighting device to the look captured before obi's most recent "
                "change ('<device> revert' / 'put it back'). The look being overwritten is "
                "captured in turn, so revert is itself revertible. dry_run previews."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "device": {
                        "type": "string",
                        "description": (
                            "Configured lighting device name; optional when exactly one is "
                            "configured."
                        ),
                    },
                    "dry_run": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
            handler=_wled_revert,
            risk_class=LOW_RISK,
            mutating=True,
        ),
    ]


# feature 124: the outbound-call originator tool. Operator surfaces ONLY — the MCP
# surface (operator-run clients, chat's trust class) via the derived operator toolset,
# beside the `obi radio call` CLI. Excluded from every conversational surface (the 105
# CHAT_EXCLUDED mechanism below), from the radio caller tiers (pinned tuples), from the
# watcher, and from the web tool route. The tool ENQUEUES; the daemon's deterministic
# call machine does everything on-air — hail, go-ahead wait, delivery — through the one
# transmit gate. The model composes no part of what airs: the message is the operator's,
# fixed and echoed back at request time (contract O3).


def _radio_call(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Queue an outbound radio call, or poll one — self-contained over radio.db."""

    import time as _time
    from datetime import UTC

    from obi.core import radiostore
    from obi.radio.outbound import with_over_sign_off

    radio = ctx.config.radio
    if radio is None:
        return "error: the radio surface is not configured ([radio] missing)"
    tz = ctx.config.logs.tzinfo() if ctx.config.logs is not None else UTC

    request_id = args.get("request_id")
    if request_id is not None:
        try:
            conn = radiostore.connect_readonly(radio.db_path)
        except radiostore.StoreError as exc:
            return f"error: cannot open the radio store: {exc}"
        try:
            row = radiostore.get_call_request(conn, int(request_id))
        finally:
            conn.close()
        if row is None:
            return f"error: no outbound call request #{request_id}"
        _audit(ctx, action="radio_call_status", target=row.callee, outcome=AuditOutcome.OK)
        status = [
            f"outbound call #{row.id} to {row.callee}: {row.outcome or row.state}",
            f"on-air message: {row.message!r}",
            f"hail windows elapsed: {row.attempts}",
        ]
        if row.detail:
            status.append(f"detail: {row.detail}")
        if row.state not in ("done", "expired"):
            status.append("still in progress — poll again with the same request_id.")
        return "\n".join(status)

    callee = str(args.get("callee", "")).strip()
    message = str(args.get("message", "")).strip()
    if not callee:
        return "error: callee is required (who should obi hail?)"
    if not message:
        return "error: message is required (what should obi deliver?)"
    if len(message) > radio.outbound_max_chars:
        return (
            f"error: message too long for one transmission ({len(message)} chars > "
            f"{radio.outbound_max_chars}). obi never truncates a message on the air — "
            "shorten it."
        )

    from obi.core import service as svc

    try:
        if not svc.service_state(svc.spec_for("radio")).loaded:
            return (
                "error: the radio station is not running — nobody would be hailed. "
                "Start it with `obi radio start`."
            )
    except Exception:  # noqa: BLE001 — an advisory check; the TTL is the hard gate
        pass

    on_air = with_over_sign_off(message)
    try:
        conn = radiostore.connect(radio.db_path)
    except radiostore.StoreError as exc:
        return f"error: cannot open the radio store: {exc}"
    try:
        new_id = radiostore.enqueue_call_request(
            conn,
            created_us=_time.time_ns() // 1000,
            callee=callee,
            message=on_air,
            ttl_s=radio.outbound_ttl_s,
            tz=tz,
            origin="mcp",
        )
    finally:
        conn.close()
    _audit(ctx, action="radio_call", target=callee, outcome=AuditOutcome.OK)
    return (
        f"queued outbound call #{new_id} to {callee}.\n"
        f'hail: "{callee}, {callee}, obi" (up to {1 + radio.outbound_max_rehails} hails, '
        f"{radio.outbound_ack_window_s}s window each, TTL {radio.outbound_ttl_s}s)\n"
        f"on-air message after their go-ahead: {on_air!r}\n"
        f"The message never airs without a go-ahead. Poll with request_id={new_id} for "
        "the outcome; it is also audited and Slack-noted."
    )


def _radio_call_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="radio_call",
            description=(
                "CALL a person on the radio and deliver a short message, by channel "
                "protocol: obi hails '<Name>, <Name>, obi', waits for their go-ahead "
                "('go' / 'go for <name>' / 'go for obi'), and only then transmits the "
                "message — exactly as given here, never composed or embellished. Up to "
                "2 re-hails, then the failure is reported instead of the message airing "
                "blind. Use ONLY when the operator explicitly asks to call/reach someone "
                "on the radio. Pass callee+message to place a call; pass request_id "
                "alone to check one."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "callee": {
                        "type": "string",
                        "description": "Who to hail, by name, as it should be spoken.",
                    },
                    "message": {
                        "type": "string",
                        "description": (
                            "The message to deliver after the go-ahead — the operator's "
                            "words; transmitted verbatim with a closing 'over'."
                        ),
                    },
                    "request_id": {
                        "type": "integer",
                        "description": "Poll an existing outbound call instead of placing one.",
                    },
                },
                "additionalProperties": False,
            },
            handler=_radio_call,
            risk_class=LOW_RISK,
            mutating=True,
        ),
    ]


def _store_name(ctx: ToolContext) -> str:
    """How answers name the external metrics store (``[victoria] display_name``, feature 137)."""

    victoria = getattr(ctx.config, "victoria", None)
    return getattr(victoria, "display_name", None) or "the external metrics store"


def render_descriptions(tools: list[ToolDefinition], config: Any) -> list[ToolDefinition]:
    """Fill the deployment placeholders in tool descriptions from ``config`` (feature 137).

    ``{store}``, ``{lighting}``, ``{weather_place}`` and ``{mesh_example}`` are plain-text tokens
    (str.replace, never format — descriptions hold literal braces); with no config each renders
    in its generic form, so no description ever names a real deployment from source."""

    from obi.core.prompt import _lighting_devices

    victoria = getattr(config, "victoria", None) if config is not None else None
    store = getattr(victoria, "display_name", None) or "the external metrics store"
    weather = getattr(config, "weather", None) if config is not None else None
    place = getattr(weather, "place_name", None) or "the configured location"
    devices, mapping = _lighting_devices(config)
    lighting = f" ({devices}" + (f"; {mapping.strip().rstrip('.')}" if mapping else "") + ")"
    mesh = getattr(config, "meshtastic", None) if config is not None else None
    names = sorted({*(getattr(mesh, "display_names", {}) or {}).values()}) if mesh else []
    mesh_example = f" (e.g. {names[0]!r})" if names else ""
    out = []
    for tool in tools:
        text = (
            tool.description.replace("{store}", store)
            .replace("{lighting}", lighting)
            .replace("{weather_place}", place)
            .replace("{mesh_example}", mesh_example)
        )
        out.append(tool if text == tool.description else replace(tool, description=text))
    return out


def available_tools(tools: list[ToolDefinition], config: Any) -> list[ToolDefinition]:
    """``tools`` minus those whose required config sections are unset (feature 137, FR-005).

    A pure, explicit, code-visible narrowing (constitution III's MCP clause): it can only
    REMOVE tools, so no surface is ever widened by config."""

    return [t for t in tools if all(getattr(config, s, None) is not None for s in t.requires)]


def _maybe_filter(tools: list[ToolDefinition], config: Any) -> list[ToolDefinition]:
    kept = tools if config is None else available_tools(tools, config)
    return render_descriptions(kept, config)


def build_operator_tools(config: Any = None) -> list[ToolDefinition]:
    """The registry + obi's mutating tools: the decorative-lighting writes (constitution
    1.15.0) and the outbound-call originator (124, constitution 2.2.0).

    This is the ONLY builder that returns mutating tools; its consumers are the chat
    runtime (minus the 105/124 chat exclusions), the MCP server, and the radio tiers
    (constitution 2.0.0; the radio tiers pin name tuples that exclude the 124 tool). A
    new read-only tool added to ``build_tools()`` appears here automatically; a new WRITE
    tool must be added explicitly and survive ``validate_operator_tools`` + the operator
    contract test.
    """

    return _maybe_filter([*build_tools(), *_wled_write_tools(), *_radio_call_tools()], config)


#: The mutating extras build_operator_tools may add beyond the registry — the closed,
#: code-visible list validate_operator_tools enforces (103 boundary, extended by 124).
OPERATOR_WRITE_TOOL_NAMES = ("wled_set", "wled_revert", "radio_call")


def validate_operator_tools() -> None:
    """Assert the 103 write-boundary invariants; raises on violation (the 048 standing-guard
    shape). Called by the contract test and cheap enough to call at service wiring time."""

    registry = build_tools()
    for tool in registry:
        if tool.mutating or tool.risk_class != READ_ONLY:
            raise ValueError(f"registry tool {tool.name!r} is not read-only")
    registry_names = {tool.name for tool in registry}
    extras = [tool for tool in build_operator_tools() if tool.name not in registry_names]
    if {tool.name for tool in extras} != set(OPERATOR_WRITE_TOOL_NAMES):
        raise ValueError(
            "operator toolset must extend the registry by exactly the declared write "
            "tools, got: " + ", ".join(sorted(tool.name for tool in extras))
        )
    for tool in extras:
        if not tool.mutating or tool.risk_class != LOW_RISK:
            raise ValueError(f"write tool {tool.name!r} must be mutating with risk 'low'")


# feature 105 (operator decision 2026-08-26): tools registered for the operator's own Claude
# (the MCP surface) but NOT offered to the conversational surfaces — chat CLI, Slack, voice,
# web CHAT/TALK, XR — which all build their runtime through build_chat_tools(). The audit
# trail summarizes everything obi does; that is operator-only disclosure, read via
# Claude Desktop/Code, not something obi recites back to whoever is on a chat surface.
# feature 124 (operator decision 2026-09-02): radio_call joins the exclusions — outbound
# calls originate from the CLI and MCP only; no chat surface (and no radio caller — the
# tiers pin their own tuples) can put a hail on the air.
CHAT_EXCLUDED_TOOL_NAMES = ("audit_query", "radio_call")


def build_chat_tools(config: Any = None) -> list[ToolDefinition]:
    """The conversational surfaces' toolset: the OPERATOR toolset minus the chat exclusions.

    Every conversational surface shares one runtime (``chat/session.py::build_runtime``), so
    this one seam covers chat, Slack, voice, web, and XR; test_chat_tools_readonly pins it.
    Composition after 103+105 merged: the registry plus the wled write tools (2.0.0), minus
    ``CHAT_EXCLUDED_TOOL_NAMES`` (audit_query stays MCP-only disclosure).
    """

    return [
        tool for tool in build_operator_tools(config) if tool.name not in CHAT_EXCLUDED_TOOL_NAMES
    ]


# feature 014: the read-only subset the autonomous watcher exposes to its bounded investigation
# loop. Every name here is a read-only tool, so the watcher's investigation is read-only by
# construction (FR-006); test_watch_toolset asserts it.
# feature 028: the three live RouterOS tools are read-only too (query only ever issues `print`;
# ping/traceroute are read-only diagnostics), so the watcher can ground findings in live state —
# bounded per run by [watch].investigation_live_call_budget (LiveCallBudget on the ToolContext).
# feature 036: the watcher also reaches the per-vendor read-only tools a human has in chat —
# uisp_status (006, UISP device health), siklu_status (020, Siklu radio/link RF), and config_query
# (026, stored RouterOS config from the local git backup) — so it can ground multi-vendor findings.
# The live device/controller-contacting ones are budgeted (see BUDGETED_LIVE_TOOL_NAMES); the
# corresponding sources are wired into the watcher's ToolContext (core/watch.py::_judge).
WATCH_TOOL_NAMES = (
    "logs_query",
    "snmp_metrics",
    # feature 090: the STORED time-series history, so a finding can be quantified ("traffic on
    # that link fell from 300 Mbit/s to nothing at 14:05") instead of asserted. A local read that
    # contacts no device, so it is deliberately NOT in BUDGETED_LIVE_TOOL_NAMES below and stays
    # available even at budget 0.
    "metrics_history",
    # feature 100: current hardware-health state from the same store — also a local read
    # that contacts no device, so unbudgeted and available even at budget 0.
    "query_health",
    # feature 118: the 095 board — lets an investigation open with the operator's own
    # rollup ("the dash already shows fleet red since 14:02"). A local read, unbudgeted.
    "dash_status",
    # feature 110: site weather from the local snapshot store — a contacts-nothing read, so
    # unbudgeted; lets an investigation note "gusting 45 mph at the time" beside a link finding.
    "weather_forecast",
    # feature 112: gate travel times from the local traffic store — the same contacts-nothing
    # class, so unbudgeted. No watch pass exists for traffic; this is investigation context only.
    "gate_traffic",
    "routeros_status",
    "routeros_neighbors",
    "inventory_list",
    "routeros_query",
    "routeros_ping",
    "routeros_traceroute",
    # feature 034/036: live UISP device health + wireless-link/backhaul RF health (read-only GETs),
    # so the watcher can ground UISP findings. Both are budgeted (feature 036 — see below).
    "uisp_links",
    "uisp_status",
    # feature 036: live Siklu EtherHaul radio/link RF health over SSH — budgeted (SSH is the most
    # expensive read); stored RouterOS config from the local git backup — a local read, unbudgeted.
    "siklu_status",
    "config_query",
    # feature 108: the external VictoriaMetrics store — REMOTE HTTP reads (unlike
    # metrics_history/query_health above), so all budgeted below.
    "backbone_rf_health",
    "victoria_query",
    "victoria_discover",
    "weather_observed",
)

# feature 028/036: the watch tools whose handler contacts a live device or controller and therefore
# consumes one unit of the per-run live-call budget. Single source of truth for both "consumes the
# budget" (the per-handler _budget_denied guard) and "stripped when the budget is 0" (the watcher's
# budget-0 strip in core/watch.py::_judge — at budget 0 ctx.live_budget is None, so the per-call
# guard cannot fire and the strip is the real enforcement). config_query is deliberately excluded:
# it reads only obi's local git backup and contacts no device.
BUDGETED_LIVE_TOOL_NAMES = (
    "routeros_query",
    "routeros_ping",
    "routeros_traceroute",
    "uisp_status",
    "uisp_links",
    "siklu_status",
    # feature 108: the victoria-store tools reach a remote HTTP host (core-vms-1) — each call
    # consumes one live-budget unit on the watcher, exactly like the controller reads above.
    "backbone_rf_health",
    "victoria_query",
    "victoria_discover",
    "weather_observed",
    # feature 111: even the passive path reads the attached radio over serial, and the
    # refresh path can transmit on the mesh — a device-contacting call like the rest.
    # (Budget-0 stripping matters only where a budget exists; the watcher never sees this
    # tool at all — it is deliberately NOT in WATCH_TOOL_NAMES.)
    "mesh_locate",
)


def build_watch_tools(config: Any = None) -> list[ToolDefinition]:
    """The read-only tool subset for the watcher's investigation loop (feature 014)."""

    return _maybe_filter([tool for tool in build_tools() if tool.name in WATCH_TOOL_NAMES], config)


# feature 088: the radio surface's toolset (spec FR-014/015/016). RF carries no
# authentication, so this is STATIC code — never config, never runtime-widened.
#
# OPERATOR DECISION 2026-08-20: the two tiers are COLLAPSED. Every caller on channel now
# gets the same set, whether or not their stated handle matched [radio] operator_handles.
# The tier machinery is untouched (calls.py still resolves safe/operator per call), so
# re-gating is one edit — replace the RADIO_SAFE_TOOL_NAMES alias below with a narrower
# literal tuple and nothing else changes.
#
# Constitution 2.0.0 (operator decision 2026-08-26, consequence stated plainly): the
# decorative-lighting write tools ARE on this surface, for ANY caller — FM carries no
# authentication, so anyone in RF range can set or revert the portal lights. That class is
# low-risk by construction, one revert away from undone, and audited per claimed handle
# (actor="radio:<caller>"). NETWORK devices remain untouchable from RF: every other tool
# here is read-only, and validate_radio_tools pins the mutating set to exactly the wled
# writes. Budgeted live tools stay budgeted via a per-call LiveCallBudget.
#
# unifi_status was excluded from the radio ENTIRELY (not merely gated) from the start:
# playa wifi is another team's system — the same standing posture that keeps `unifi` in
# [watch] ignore_collectors. Since feature 105 that exclusion is registry-wide.
RADIO_OPERATOR_TOOL_NAMES = (
    "inventory_list",
    "inventory_summary",
    "routeros_status",
    "routeros_neighbors",
    "uisp_status",
    "uisp_links",
    "snmp_metrics",
    # feature 090: a local store read that contacts nothing — the property this surface
    # most wants, since it cannot touch a device however the caller identifies themselves.
    "metrics_history",
    # feature 100: hardware-health state — the same contacts-nothing property; short,
    # bounded payloads by construction so on-air answers fit the reply budget.
    "query_health",
    # feature 118: the 095 board — the ideal radio tool ("obi, how's the network"): a
    # contacts-nothing local read with caps by construction, so the one-breath answer
    # carries the overall state without ever needing a device.
    "dash_status",
    # feature 110: site weather (forecast/current/alerts/history) — a local snapshot read,
    # contacts nothing, bounded payload; "obi, what's the wind tonight?" is a natural call.
    "weather_forecast",
    # feature 112: gate travel times + gate status — the same contacts-nothing local-read
    # class, bounded by construction; "obi, how long is the gate line?" is a natural call
    # from anyone coordinating an arrival or a supply run.
    "gate_traffic",
    "logs_query",
    "siklu_status",
    "config_query",
    "routeros_query",
    "routeros_ping",
    "routeros_traceroute",
    # feature 108: the external store — read-only remote HTTP, safe for any caller; the curated
    # payloads are bounded by construction (site cap + compact blocks), and the free-form
    # tools are series-capped. weather_observed is the headline radio ask (wind gates climbs).
    "backbone_rf_health",
    "victoria_query",
    "victoria_discover",
    "weather_observed",
    # feature 103: current portal-lighting state (read-only) — and, by constitution 2.0.0
    # (operator decision, any caller), the lighting WRITE tools. The ONLY mutating entries;
    # validate_radio_tools pins that.
    "wled_status",
    "wled_set",
    "wled_revert",
    # feature 111: mesh-node location — read-only, bounded payload, and its one possible
    # transmission is globally rate-limited, so an unauthenticated caller cannot make obi
    # flood the mesh however often they ask.
    "mesh_locate",
    # feature 127: camp/art location from the local city datasets — the ideal radio tool
    # class again (contacts nothing, bounded answer): "obi, where's Camp Soft Landing?"
    "playa_locate",
)

#: Collapsed into the operator tier (operator decision 2026-08-20 — see above). To re-gate,
#: make this an explicit, narrower literal tuple; ``build_radio_tools`` needs no change.
RADIO_SAFE_TOOL_NAMES = RADIO_OPERATOR_TOOL_NAMES


def validate_radio_tools() -> None:
    """Assert the radio-tier invariants; raises on violation (feature 048's standing-guard
    shape). Called by the contract test and cheap enough to call at service wiring time."""

    by_name = {tool.name: tool for tool in build_operator_tools()}
    # Subset, not STRICT subset: the tiers are collapsed by operator decision (2026-08-20), so
    # equality is the expected state. The invariant that carries the safety weight is the
    # mutating-set pin below — NETWORK devices stay untouchable from RF however the tiers
    # are drawn; only the decorative-lighting writes (constitution 2.0.0) may mutate.
    if not set(RADIO_SAFE_TOOL_NAMES) <= set(RADIO_OPERATOR_TOOL_NAMES):
        raise ValueError("RADIO_SAFE_TOOL_NAMES must not exceed the operator tier")
    for name in (*RADIO_SAFE_TOOL_NAMES, *RADIO_OPERATOR_TOOL_NAMES):
        tool = by_name.get(name)
        if tool is None:
            raise ValueError(f"radio tier has {name!r}, which is not a registered obi tool")
        if tool.mutating and name not in ("wled_set", "wled_revert"):
            raise ValueError(f"radio tier has {name!r}, which mutates and is not a wled write")
        if not tool.mutating and tool.risk_class != "read_only":
            raise ValueError(f"radio tier has {name!r} with a non-read-only risk class")


def build_radio_tools(tier: str, config: Any = None) -> list[ToolDefinition]:
    """The tool subset for one radio call's tier (``safe`` | ``operator``).

    Drawn from the OPERATOR toolset (2.0.0) so the tier tuples can name the lighting
    write tools; the tuples remain the static gate on what actually reaches the air.
    """

    names = RADIO_OPERATOR_TOOL_NAMES if tier == "operator" else RADIO_SAFE_TOOL_NAMES
    # feature 137: config can only NARROW a tier (an unconfigured feature's tools drop out);
    # the static tuples above remain the gate on what may ever reach the air.
    return [tool for tool in build_operator_tools(config) if tool.name in names]


def _with_site_tz(ctx: ToolContext, handler: ToolHandler, args: dict[str, Any]) -> str:
    """Run one handler with the operator's ``[site] timezone`` as the local zone (137)."""

    site = getattr(ctx.config, "site", None)
    token = _SITE_TZ.set(getattr(site, "timezone", None) or "UTC")
    try:
        return handler(ctx, args)
    finally:
        _SITE_TZ.reset(token)


def build_dispatch(
    ctx: ToolContext, tools: list[ToolDefinition]
) -> dict[str, Callable[[dict[str, Any]], str]]:
    """Map tool name → a callable the loop invokes; handlers never raise to the loop."""

    def make(tool: ToolDefinition) -> Callable[[dict[str, Any]], str]:
        def run(args: dict[str, Any]) -> str:
            # feature 042: every model-initiated tool call lands in the audit event store —
            # name, arguments, full result, duration, error-ness — via the ambient scope (a
            # no-op when capture is off). The recorder bounds/redacts the payload itself.
            start = time.monotonic()
            try:
                result = _with_site_tz(ctx, tool.handler, args)
            except Exception as exc:  # noqa: BLE001 - a tool failure must never crash the loop
                _audit(
                    ctx,
                    action=f"{tool.name}",
                    target="error",
                    outcome=AuditOutcome.ERROR,
                    detail=str(exc),
                )
                message = f"error running {tool.name}: {exc}"
                record_event(
                    "tool_call",
                    summary=f"tool {tool.name} raised",
                    payload={
                        "tool": tool.name,
                        "arguments": args,
                        "result": message,
                        "is_error": True,
                    },
                    duration_ms=(time.monotonic() - start) * 1000.0,
                    outcome="error",
                    error=str(exc),
                )
                return message
            is_error = result.startswith("error")
            record_event(
                "tool_call",
                summary=f"tool {tool.name}" + (" (returned error)" if is_error else ""),
                payload={
                    "tool": tool.name,
                    "arguments": args,
                    "result": result,
                    "is_error": is_error,
                },
                duration_ms=(time.monotonic() - start) * 1000.0,
            )
            return result

        return run

    return {tool.name: make(tool) for tool in tools}

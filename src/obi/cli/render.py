"""CLI presentation helpers (feature 066): the fmt/render layer, extracted from main.py.

Pure Rich rendering of core report objects (Constitution Principle III) — one cohesive home for the
per-vendor table/formatter helpers the top-level `status` command and the vendor sub-apps both use.
No business logic, no device or config access; every value here was computed by the core.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from obi.core.models import (
    DeviceDrift,
    DriftClass,
    DriftReport,
    InventoryReport,
    LogicalLink,
    NetboxReport,
    Reachability,
    SikluReport,
    SourceOutcome,
    UispLinkEnd,
    UispLinksReport,
    UispReport,
    UnifiReport,
)

if TYPE_CHECKING:
    from obi.core.dash import Board
    from obi.core.icmpstore import DeviceState
    from obi.core.inventory import Inventory

# The null-value placeholder for CLI tables ("no data" reads cleaner than a blank cell).
_DASH = "-"


def _fmt_uptime(seconds: int | None) -> str:
    if seconds is None:
        return _DASH
    units = (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60), ("s", 1))
    parts: list[str] = []
    remaining = seconds
    for label, size in units:
        if remaining >= size:
            value, remaining = divmod(remaining, size)
            parts.append(f"{value}{label}")
        if len(parts) == 2:  # keep it compact: two most-significant units
            break
    return "".join(parts) if parts else "0s"


def _fmt_bytes(num: int | None) -> str:
    if num is None:
        return _DASH
    size = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def _fmt_pct(value: float | None) -> str:
    return _DASH if value is None else f"{value:g}%"


def _render_status_table(inventory: Inventory, report: InventoryReport, console: Console) -> None:
    from obi.core.models import DiscoveryState, Reachability

    live = {status.device.address: status for status in report.statuses}
    table = Table(title="Mikrotik inventory & status", title_justify="left")
    for column in (
        "Identity",
        "Address",
        "Model",
        "RouterOS",
        "Uptime",
        "CPU%",
        "Free Mem",
        "Reachability",
        "State",
    ):
        table.add_column(column, overflow="fold")

    for entry in inventory.entries:
        status = live.get(entry.address)
        state = entry.discovery_state.value + ("" if entry.present_in_latest else " (absent)")
        table.add_row(
            entry.identity or _DASH,
            entry.address,
            entry.model or _DASH,
            entry.version or _DASH,
            _fmt_uptime(status.uptime_seconds if status else None),
            _fmt_pct(status.cpu_load_percent if status else None),
            _fmt_bytes(status.free_memory_bytes if status else None),
            status.reachability.value if status else _DASH,
            state,
        )

    console.print(table)

    entries = inventory.entries
    if not entries:
        console.print("Inventory is empty — run `obi routeros discover` to populate it.")
        return
    parts: list[str] = []
    for reachability in Reachability:
        count = sum(1 for s in report.statuses if s.reachability is reachability)
        if count:
            parts.append(f"{reachability.value}={count}")
    unsupported = sum(1 for e in entries if e.discovery_state is DiscoveryState.UNSUPPORTED)
    absent = sum(1 for e in entries if not e.present_in_latest)
    if unsupported:
        parts.append(f"unsupported={unsupported}")
    if absent:
        parts.append(f"absent={absent}")
    console.print(f"{len(entries)} device(s): " + ", ".join(parts))


def _render_inventory(inventory: Inventory, console: Console) -> None:
    from obi.core.models import DiscoveryState

    table = Table(title="Discovered inventory", title_justify="left")
    for column in ("Identity", "Address", "Model", "State", "Provenance", "Last Seen"):
        table.add_column(column, overflow="fold")
    for entry in inventory.entries:
        state = entry.discovery_state.value + ("" if entry.present_in_latest else " (absent)")
        table.add_row(
            entry.identity or _DASH,
            entry.address,
            entry.model or _DASH,
            state,
            entry.provenance,
            entry.last_seen.strftime("%Y-%m-%d %H:%M"),
        )
    console.print(table)

    entries = inventory.entries
    if not entries:
        console.print("No devices discovered within the configured scope.")
        return
    new = sum(1 for e in entries if e.present_in_latest and e.first_seen == e.last_seen)
    absent = sum(1 for e in entries if not e.present_in_latest)
    parts = [f"found={len(entries)}", f"new={new}", f"absent={absent}"]
    for state in DiscoveryState:
        count = sum(1 for e in entries if e.discovery_state is state)
        if count:
            parts.append(f"{state.value}={count}")
    console.print("inventory: " + ", ".join(parts))


def _render_uisp_report(report: UispReport, console: Console) -> None:
    """Render the live UISP report (feature 003) as a Rich table + summary line."""

    title = f"UISP inventory & status ({report.controller})"
    if report.name_filter:
        title += f" — filter: {report.name_filter!r}"
    table = Table(title=title, title_justify="left")
    for column in (
        "Vendor",
        "Identity",
        "Address",
        "Model",
        "Firmware",
        "Uptime",
        "CPU%",
        "Free Mem",
        "Reachability",
    ):
        table.add_column(column, overflow="fold")

    for status in report.statuses:
        device = status.device
        table.add_row(
            "uisp",
            device.identity or _DASH,
            device.address,
            device.model or _DASH,
            device.version or _DASH,
            _fmt_uptime(status.uptime_seconds),
            _fmt_pct(status.cpu_load_percent),
            _fmt_bytes(status.free_memory_bytes),
            status.reachability.value,
        )

    console.print(table)

    if not report.statuses:
        if report.outcome is SourceOutcome.REACHABLE:
            console.print(
                "No UISP devices matched the filter."
                if report.name_filter
                else "The controller reported no devices."
            )
        else:
            detail = f": {report.detail}" if report.detail else ""
            console.print(f"UISP controller {report.outcome.value}{detail}")
        return

    counts = report.counts
    parts = [f"{r.value}={counts[r.value]}" for r in Reachability if counts.get(r.value)]
    console.print(f"{counts['total']} UISP device(s): " + ", ".join(parts))


def _fmt_signal(dbm: float | None) -> str:
    return _DASH if dbm is None else f"{dbm:g} dBm"


def _fmt_distance(meters: float | None) -> str:
    if meters is None:
        return _DASH
    return f"{meters / 1000:.1f} km" if meters >= 1000 else f"{meters:g} m"


def _fmt_mhz(mhz: float | None) -> str:
    return _DASH if mhz is None else f"{mhz:g} MHz"


def _fmt_link_capacity(end_a: UispLinkEnd, end_b: UispLinkEnd) -> str:
    """The link's theoretical capacity (down/up Mbps) from the first end that reports one."""

    for end in (end_a, end_b):
        radio = end.radio
        if radio and (radio.capacity_down_bps or radio.capacity_up_bps):
            down = _DASH if radio.capacity_down_bps is None else str(
                round(radio.capacity_down_bps / 1_000_000)
            )
            up = _DASH if radio.capacity_up_bps is None else str(
                round(radio.capacity_up_bps / 1_000_000)
            )
            return f"{down}/{up} Mbps"
    return _DASH


# An end whose controller link score fell below this renders as quality-degraded (FR-004).
_DEGRADED_SCORE = 0.5


def _uisp_end_cell(end: UispLinkEnd) -> str:
    """One link end: ``name: 0.82 (−65 dBm)`` — score styled when degraded, ``-`` when absent."""

    name = end.name or "?"
    radio = end.radio
    if radio is None:
        return f"{name}: {_DASH}"
    score = _DASH if radio.score is None else f"{radio.score:.2f}"
    if radio.score is not None and radio.score < _DEGRADED_SCORE:
        score = f"[yellow]{score}[/yellow]"
    return f"{name}: {score} ({_fmt_signal(radio.signal_dbm)})"


def _render_uisp_links(report: UispLinksReport, console: Console) -> None:
    """Render the wireless link-health view (feature 034) as a Rich table + summary line."""

    title = f"UISP wireless links ({report.controller})"
    if report.name_filter:
        title += f" — filter: {report.name_filter!r}"
    table = Table(title=title, title_justify="left")
    for column in (
        "Link",
        "State",
        "Signal",
        "SSID",
        "Freq",
        "Distance",
        "End A",
        "End B",
        "Capacity",
    ):
        table.add_column(column, overflow="fold")

    for link in report.links:
        state = link.state or _DASH
        if not link.is_active:
            state = f"[red]{state}[/red]"
        table.add_row(
            link.label,
            state,
            _fmt_signal(link.signal_dbm),
            link.ssid or _DASH,
            _fmt_mhz(link.frequency_mhz),
            _fmt_distance(link.distance_m),
            _uisp_end_cell(link.end_a),
            _uisp_end_cell(link.end_b),
            _fmt_link_capacity(link.end_a, link.end_b),
        )

    console.print(table)

    if not report.links:
        if report.outcome is SourceOutcome.REACHABLE:
            console.print(
                "No wireless links matched the filter."
                if report.name_filter
                else "The controller reported no wireless links."
            )
        else:
            detail = f": {report.detail}" if report.detail else ""
            console.print(f"UISP controller {report.outcome.value}{detail}")
        return

    counts = report.counts
    console.print(
        f"{counts['total']} wireless link(s): active={counts['active']}, down={counts['down']}"
    )


def _render_unifi_report(report: UnifiReport, console: Console) -> None:
    """Render the live UniFi report (feature 011) as a Rich table + summary line."""

    title = f"UniFi inventory & status ({report.console})"
    if report.name_filter:
        title += f" — filter: {report.name_filter!r}"
    table = Table(title=title, title_justify="left")
    for column in (
        "Vendor",
        "Identity",
        "Address",
        "Model",
        "Firmware",
        "Uptime",
        "CPU%",
        "Free Mem",
        "Reachability",
    ):
        table.add_column(column, overflow="fold")

    for status in report.statuses:
        device = status.device
        table.add_row(
            "unifi",
            device.identity or _DASH,
            device.address,
            device.model or _DASH,
            device.version or _DASH,
            _fmt_uptime(status.uptime_seconds),
            _fmt_pct(status.cpu_load_percent),
            _fmt_bytes(status.free_memory_bytes),
            status.reachability.value,
        )

    console.print(table)

    if not report.statuses:
        if report.outcome is SourceOutcome.REACHABLE:
            console.print(
                "No UniFi devices matched the filter."
                if report.name_filter
                else "The console reported no devices."
            )
        else:
            detail = f": {report.detail}" if report.detail else ""
            console.print(f"UniFi console {report.outcome.value}{detail}")
        return

    counts = report.counts
    parts = [f"{r.value}={counts[r.value]}" for r in Reachability if counts.get(r.value)]
    console.print(f"{counts['total']} UniFi device(s): " + ", ".join(parts))


def _render_netbox_report(report: NetboxReport, console: Console) -> None:
    """Render the documented NetBox report (feature 007) as a Rich table + summary line."""

    title = f"NetBox documented inventory ({report.source})"
    if report.name_filter:
        title += f" — filter: {report.name_filter!r}"
    table = Table(title=title, title_justify="left")
    for column in (
        "Name",
        "Site",
        "Role",
        "Model",
        "Manufacturer",
        "Status",
        "Primary IP",
        "Serial",
    ):
        table.add_column(column, overflow="fold")

    for device in report.devices:
        table.add_row(
            device.name or _DASH,
            device.site or _DASH,
            device.role or _DASH,
            device.model or _DASH,
            device.manufacturer or _DASH,
            device.status or _DASH,
            device.primary_ip or _DASH,
            device.serial or _DASH,
        )

    console.print(table)

    if not report.devices:
        if report.outcome is SourceOutcome.REACHABLE:
            console.print(
                "No NetBox devices matched the filter."
                if report.name_filter
                else "NetBox documents no devices."
            )
        else:
            detail = f": {report.detail}" if report.detail else ""
            console.print(f"NetBox {report.outcome.value}{detail}")
        return

    counts = report.counts
    parts = [f"{k}={v}" for k, v in counts.items() if k != "total"]
    console.print(f"{counts['total']} documented device(s): " + ", ".join(sorted(parts)))


# Drift-class render order + heading; matched (the all-good bucket) shown last.
_DRIFT_GROUPS: tuple[tuple[DriftClass, str], ...] = (
    (DriftClass.UNDOCUMENTED, "ADD — live but undocumented"),
    (
        DriftClass.RETIRE_CANDIDATE,
        "RETIRE CANDIDATES — documented, in-scope, not live  [review only; never auto-retires]",
    ),
    (DriftClass.OUTSIDE_SCOPE, "NOT EVALUATED — outside discovery scope"),
    (DriftClass.UN_CORRELATABLE, "NOT EVALUATED — no documented primary IP"),
    (DriftClass.MATCHED, "MATCHED — documented and live"),
)


def _drift_annotation(drift: DeviceDrift) -> str:
    bits: list[str] = []
    if drift.drift_class is DriftClass.RETIRE_CANDIDATE:
        bits.append(
            f"last seen {drift.last_seen.date().isoformat()}"
            if drift.last_seen
            else "never seen in roster"
        )
    bits.extend(drift.notes)
    return "; ".join(bits) or _DASH


def _render_drift_report(report: DriftReport, console: Console) -> None:
    """Render the read-only NetBox drift report (feature 017) grouped by drift class."""

    title = f"NetBox drift — RouterOS/MikroTik ({report.source})"
    if report.name_filter:
        title += f" — filter: {report.name_filter!r}"
    age = report.roster_generated_at.isoformat() if report.roster_generated_at else _DASH
    console.print(f"[bold]{title}[/bold]")
    console.print(
        f"reality roster: {age}   source: {report.source_outcome.value}   "
        "[dim][READ-ONLY — applies nothing][/dim]"
    )
    if report.detail:
        console.print(f"[yellow]{report.detail}[/yellow]")

    for drift_class, heading in _DRIFT_GROUPS:
        items = [d for d in report.drift if d.drift_class is drift_class]
        if not items:
            continue
        table = Table(title=f"{heading} ({len(items)})", title_justify="left")
        for column in ("Name", "Address", "Model", "Site", "Last seen / notes"):
            table.add_column(column, overflow="fold")
        for drift in items:
            table.add_row(
                drift.name or _DASH,
                ", ".join(drift.addresses) if drift.addresses else _DASH,
                drift.model or _DASH,
                drift.site or _DASH,
                _drift_annotation(drift),
            )
        console.print(table)

    counts = report.counts
    if counts["total"] == 0:
        console.print("No drift to report.")
        return
    parts = [f"{k}={v}" for k, v in counts.items() if k != "total"]
    console.print(f"{counts['total']} device(s): " + ", ".join(sorted(parts)))


def _fmt_temp(celsius: float | None) -> str:
    return _DASH if celsius is None else f"{celsius:g}C"


def _fmt_rf_summary(report_rf: object) -> str:
    """A compact RF summary for the per-radio table cell (e.g. ``up rssi -44 cinr 27 qam128``)."""

    from obi.core.models import RfLink

    if not isinstance(report_rf, RfLink):
        return _DASH
    rf = report_rf
    state = "?" if rf.operational is None else ("up" if rf.operational else "down")
    parts = [state]
    if rf.rssi_dbm is not None:
        parts.append(f"rssi {rf.rssi_dbm}")
    if rf.cinr_db is not None:
        parts.append(f"cinr {rf.cinr_db}")
    if rf.mode:
        parts.append(rf.mode.replace("adaptive ", ""))
    if rf.air_capacity_mbps is not None:
        parts.append(f"cap {rf.air_capacity_mbps}")
    return " ".join(parts)


def _render_siklu_report(report: SikluReport, console: Console) -> None:
    """Render the live Siklu report (feature 020) as a per-radio Rich table + summary line."""

    table = Table(title="Siklu EtherHaul radios", title_justify="left")
    for column in (
        "Vendor",
        "Identity",
        "Address",
        "Model",
        "Firmware",
        "Serial",
        "Uptime",
        "Temp",
        "RF link",
        "Reachability",
    ):
        table.add_column(column, overflow="fold")

    for radio in report.radios:
        device = radio.device
        reachable = radio.reachability is Reachability.REACHABLE
        rf_cell = _fmt_rf_summary(radio.rf) if reachable else _DASH
        table.add_row(
            "siklu",
            device.identity or _DASH,
            device.address,
            device.model or _DASH,
            device.version or _DASH,
            device.serial or _DASH,
            _fmt_uptime(radio.uptime_seconds),
            _fmt_temp(radio.temperature_celsius),
            rf_cell,
            radio.reachability.value,
        )

    console.print(table)

    if not report.radios:
        console.print("No Siklu radios configured (\\[siklu] hosts).")
        return

    counts = report.counts
    parts = [f"{r.value}={counts[r.value]}" for r in Reachability if counts.get(r.value)]
    console.print(f"{counts['total']} Siklu radio(s): " + ", ".join(parts))


def _render_siklu_links(links: list[LogicalLink], console: Console) -> None:
    """Render the logical point-to-point link view (feature 020, ``obi siklu links``)."""

    table = Table(title="Siklu point-to-point links", title_justify="left")
    for column in ("Link", "State", "Paired by", "End A (RF)", "End B (RF)", "Asymmetry"):
        table.add_column(column, overflow="fold")

    def _end_cell(end: object) -> str:
        from obi.core.models import SikluRadioStatus

        if not isinstance(end, SikluRadioStatus):
            return _DASH
        name = end.device.identity or end.device.address
        if end.reachability is not Reachability.REACHABLE:
            return f"{name}: {end.reachability.value}"
        return f"{name}: {_fmt_rf_summary(end.rf)}"

    for link in links:
        table.add_row(
            link.name,
            link.state.value,
            link.paired_by,
            _end_cell(link.end_a),
            _end_cell(link.end_b),
            "; ".join(link.asymmetries) if link.asymmetries else _DASH,
        )

    console.print(table)
    if not links:
        console.print("No Siklu links to report.")
        return
    console.print(f"{len(links)} link(s).")


def _render_icmp_status(states: list[DeviceState], now_us: int, console: Console) -> None:
    """Render the ICMP reachability table (feature 072, ``obi icmp status``), down-first."""

    if not states:
        console.print("No devices in the ICMP store yet (run `obi icmp run`).")
        return

    table = Table(title="ICMP reachability", title_justify="left")
    for column in ("Device", "Address", "State", "Last seen", "For"):
        table.add_column(column, overflow="fold")

    # Down first, then by device; a stable, operator-useful ordering.
    for state in sorted(states, key=lambda s: (s.up, s.device)):
        if state.up:
            state_cell = "[green]up[/green]"
            since_s = max(0, (now_us - state.last_change_us) // 1_000_000)
            duration = _fmt_uptime(int(since_s))
        else:
            state_cell = "[red]down[/red]"
            down_s = max(0, (now_us - state.last_change_us) // 1_000_000)
            duration = _fmt_uptime(int(down_s))
        if state.last_seen_us is None:
            last_seen = "never"
        else:
            ago_s = max(0, (now_us - state.last_seen_us) // 1_000_000)
            last_seen = f"{_fmt_uptime(int(ago_s))} ago"
        table.add_row(state.device, state.address, state_cell, last_seen, duration)

    console.print(table)
    down = sum(1 for s in states if not s.up)
    console.print(f"{len(states)} device(s): {len(states) - down} up, {down} down.")


_DASH_STATE_CELL = {
    "green": "[green]green[/green]",
    "stale": "[dim]stale[/dim]",
    "yellow": "[yellow]yellow[/yellow]",
    "red": "[red]red[/red]",
}

_DASH_ATTENTION_CAP = 6


def _render_dash_board(board: Board, now_us: int, console: Console) -> None:
    """Render the status board (feature 095, ``obi dash show``): rollup, tiles, attention items.

    The terminal twin of the DASH widget — same worst-of vocabulary, same honesty rule: the
    header names the board's age so a stale file never reads as live truth.
    """

    age_s = max(0, (now_us - board.generated_at_us) // 1_000_000)
    rollup = _DASH_STATE_CELL.get(board.state, board.state)
    console.print(f"Status: {rollup}  (evaluated {_fmt_uptime(int(age_s))} ago)")
    # 135: the standing maintenance banner — no expiry, so it must never be out of sight.
    if board.maintenance is not None:
        console.print(f"[magenta]MAINT:[/magenta] {escape(board.maintenance.line())}")

    table = Table(title="Status dashboard", title_justify="left")
    for column in ("Tile", "State", "Items", "Attention"):
        table.add_column(column, overflow="fold")
    for tile in board.tiles:
        # Maintenance items are deliberate: neither attention nor ok — a dim group of their own.
        maint = [item for item in tile.items if item.maintenance is not None]
        attention = [
            item for item in tile.items if item.state != "green" and item.maintenance is None
        ]
        shown = [f"{item.label} — {item.detail}" for item in attention[:_DASH_ATTENTION_CAP]]
        if len(attention) > _DASH_ATTENTION_CAP:
            shown.append(f"…and {len(attention) - _DASH_ATTENTION_CAP} more")
        if maint:
            labels = ", ".join(item.label for item in maint[:_DASH_ATTENTION_CAP])
            extra = len(maint) - _DASH_ATTENTION_CAP
            more = f" +{extra}" if extra > 0 else ""
            shown.append(f"[dim]maintenance ({len(maint)}): {escape(labels)}{more}[/dim]")
        ok = sum(1 for item in tile.items if item.state == "green" and item.maintenance is None)
        state_cell = (
            "[dim]maintenance[/dim]"
            if tile.maintenance is not None
            else _DASH_STATE_CELL.get(tile.state, tile.state)
        )
        table.add_row(
            tile.signal,
            state_cell,
            f"{ok}/{len(tile.items) - len(maint)} ok",
            "\n".join(shown),
        )
    console.print(table)
    for note in board.notes:
        console.print(f"[dim]note:[/dim] {note}")

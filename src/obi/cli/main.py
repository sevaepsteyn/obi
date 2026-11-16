"""Thin Typer CLI adapter over the core engine (Constitution Principle III).

Parses arguments, loads config, calls ``core.engine``, and renders output. No business
logic lives here — it is purely a presentation/adapter layer.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import time as _time
from contextlib import AbstractContextManager
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from obi.cli._shared import _audit_capture
from obi.cli.loaders import (
    _audit_db_or_exit,
    _load_audit_config,
    _load_configs_config,
    _load_health_config,
    _load_logs_config,
    _load_metrics_config,
    _load_radio_config,
    _load_uisp_ingest_config,
    _load_unifi_internal_config,
    _load_web_config,
)
from obi.cli.render import (
    _render_dash_board,
    _render_drift_report,
    _render_icmp_status,
    _render_inventory,
    _render_netbox_report,
    _render_siklu_links,
    _render_siklu_report,
    _render_status_table,
    _render_uisp_links,
    _render_uisp_report,
    _render_unifi_report,
)
from obi.core.config import (
    Config,
    ConfigError,
    default_config_path,
    load_config,
    perms_warning,
)
from obi.core.drivers.mikrotik import MikrotikDriver
from obi.core.logging import setup_logging
from obi.core.models import (
    LinkHealth,
    LogReport,
    MetricReport,
    MetricSample,
    SnmpOutcome,
    SourceOutcome,
)

if TYPE_CHECKING:
    from datetime import datetime, tzinfo

    from obi.core.audit import AuditTrail
    from obi.core.config import Config
    from obi.core.lighting import Lighting
    from obi.core.metricstore import Series
    from obi.core.radiostore import Transmission as RadioTransmission
    from obi.core.service import CollectorHealth, LaunchdState, ServiceSpec
    from obi.core.watch import WatchFinding, WatchRun
    from obi.voice.web.turns import VoiceWebApp

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help=(
        "obi — read-only Mikrotik (RouterOS), UISP, UniFi, and Siklu device inventory and "
        "health status, plus NetBox documented inventory."
    ),
)

_DASH = "-"


@app.callback()
def _main() -> None:
    """obi — read-only Mikrotik device inventory and health status.

    A no-op callback so the ``status`` command keeps its name (``obi status``) rather than
    collapsing into a single root command.
    """


routeros_app = typer.Typer(
    add_completion=False,
    help=(
        "Admin commands for the RouterOS (Mikrotik) fleet. `obi routeros discover` crawls the "
        "network and writes the persisted roster — admin/CLI-only by design: the chat/Slack/"
        "watch agents have no tool that can trigger it."
    ),
)
app.add_typer(routeros_app, name="routeros")


@routeros_app.command("discover")
def discover(
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit the inventory as JSON instead of a table.")
    ] = False,
    scope: Annotated[
        Path | None,
        typer.Option("--scope", help="Path to the config/scope file (default: XDG config)."),
    ] = None,
    timeout: Annotated[
        float | None, typer.Option("--timeout", help="Per-device probe timeout in seconds.")
    ] = None,
    concurrency: Annotated[
        int | None, typer.Option("--concurrency", help="Max concurrent probes.")
    ] = None,
    budget: Annotated[
        float | None, typer.Option("--budget", help="Total crawl wall-clock budget in seconds.")
    ] = None,
) -> None:
    """Crawl the network from the configured seeds and write the persisted inventory."""

    setup_logging()
    logger = logging.getLogger("obi")
    err = Console(stderr=True)
    config_path = scope or default_config_path()

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        err.print(f"[red]config error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc

    warning = perms_warning(config_path)
    if warning:
        logger.warning("insecure config permissions", extra={"detail": warning})
        err.print(f"[yellow]warning:[/yellow] {warning}")

    if timeout is not None:
        config.timeout = timeout
    if concurrency is not None:
        config.concurrency = concurrency
    if budget is not None:
        config.budget = budget

    # `obi routeros discover` is Mikrotik-only: the persisted roster is built by the topology
    # crawl. UISP devices are queried live by `obi status` and are never persisted (003, FR-017).
    if config.routeros is None or config.scope is None:
        err.print(
            "[yellow]note:[/yellow] `obi routeros discover` is Mikrotik-only and no \\[routeros] "
            "section is configured. UISP devices are queried live via `obi status` (not "
            "persisted)."
        )
        raise typer.Exit(code=0)

    from obi.core import inventory as inv
    from obi.core.audit import AuditTrail
    from obi.core.crawl import crawl

    driver = MikrotikDriver(config.routeros)
    audit = AuditTrail(config.audit_path)

    with _audit_capture(config, "cli:discover"):
        fresh = crawl(
            config.scope,
            driver,
            audit=audit,
            timeout=config.timeout,
            concurrency=config.concurrency,
            budget=config.budget,
            resolve_link_local_via_ospf=config.resolve_link_local_via_ospf,
        )
    merged = inv.reconcile(inv.load(config.inventory_path), fresh)
    inv.save(merged, config.inventory_path)

    if json_output:
        from obi.core.serialize import inventory_to_json

        sys.stdout.write(inventory_to_json(merged) + "\n")
    else:
        _render_inventory(merged, Console())


@routeros_app.command("status")
def routeros_status(
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON instead of a table.")
    ] = False,
    scope: Annotated[
        Path | None,
        typer.Option("--scope", help="Path to the config/scope file (default: XDG config)."),
    ] = None,
    timeout: Annotated[
        float | None, typer.Option("--timeout", help="Per-device timeout in seconds.")
    ] = None,
    concurrency: Annotated[
        int | None, typer.Option("--concurrency", help="Max concurrent device queries.")
    ] = None,
) -> None:
    """Report live health for the RouterOS (Mikrotik) roster only.

    The same Mikrotik section ``obi status`` shows, without the other vendors. Devices come from
    the persisted inventory (run ``obi routeros discover`` first).
    """

    setup_logging()
    logger = logging.getLogger("obi")
    err = Console(stderr=True)
    config_path = scope or default_config_path()

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        err.print(f"[red]config error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc

    warning = perms_warning(config_path)
    if warning:
        logger.warning("insecure config permissions", extra={"detail": warning})
        err.print(f"[yellow]warning:[/yellow] {warning}")

    if config.routeros is None:
        err.print(
            f"[red]routeros not configured:[/red] add a \\[routeros] section to {config_path}."
        )
        raise typer.Exit(code=2)

    if timeout is not None:
        config.timeout = timeout
    if concurrency is not None:
        config.concurrency = concurrency

    from obi.core.audit import AuditTrail  # local import keeps module import graph shallow

    audit = AuditTrail(config.audit_path)
    with _audit_capture(config, "cli:routeros-status"):
        _mikrotik_status_section(config, audit, Console(), err, json_output=json_output)


# Each vendor's status section is a helper shared by `obi status` (all vendors) and that
# vendor's own `status` subcommand (`obi routeros status`, `obi uisp status`, `obi unifi status`),
# so the scoped views render byte-for-byte the same as their section in the combined report.
def _mikrotik_status_section(
    config: Config, audit: AuditTrail, console: Console, err: Console, *, json_output: bool
) -> None:
    from obi.core import inventory as inv
    from obi.core.engine import status_from_inventory
    from obi.core.serialize import status_report_to_json

    routeros_cfg = config.routeros
    if routeros_cfg is None:  # callers gate on this; the check narrows the type
        return
    loaded = inv.load(config.inventory_path)
    if loaded is None:
        err.print(
            "[red]no inventory found:[/red] run `obi routeros discover` first to build one "
            f"(expected at {config.inventory_path})."
        )
        raise typer.Exit(code=2)

    report = status_from_inventory(
        loaded,
        MikrotikDriver(routeros_cfg),
        audit=audit,
        timeout=config.timeout,
        concurrency=config.concurrency,
    )
    if json_output:
        sys.stdout.write(status_report_to_json(loaded, report) + "\n")
    else:
        _render_status_table(loaded, report, console)


def _uisp_status_section(
    config: Config,
    audit: AuditTrail,
    console: Console,
    err: Console,
    *,
    json_output: bool,
    name_filter_override: str | None,
) -> None:
    from obi.core.drivers.uisp import UispController
    from obi.core.engine import uisp_status
    from obi.core.serialize import uisp_report_to_json

    uisp_cfg = config.uisp
    if uisp_cfg is None:  # callers gate on this; the check narrows the type
        return
    name_filter = name_filter_override if name_filter_override is not None else uisp_cfg.name_filter
    report = uisp_status(
        UispController(uisp_cfg),
        audit=audit,
        controller=uisp_cfg.base_url,
        name_filter=name_filter,
        ignore_names=uisp_cfg.ignore_names,  # feature 035: UISP-scoped exclude
        timeout=uisp_cfg.timeout,
    )
    # A controller failure is reported (not raised); obi still exits cleanly (FR-011/SC-005).
    if report.outcome is not SourceOutcome.REACHABLE:
        detail = f" — {report.detail}" if report.detail else ""
        err.print(f"[yellow]uisp:[/yellow] controller {report.outcome.value}{detail}")
    if json_output:
        sys.stdout.write(uisp_report_to_json(report) + "\n")
    else:
        _render_uisp_report(report, console)


def _unifi_status_section(
    config: Config,
    audit: AuditTrail,
    console: Console,
    err: Console,
    *,
    json_output: bool,
    name_filter_override: str | None,
) -> None:
    from obi.core.drivers.unifi_network import UnifiNetworkController
    from obi.core.engine import unifi_status as unifi_status_engine
    from obi.core.serialize import unifi_report_to_json

    unifi_cfg = config.unifi_network
    if unifi_cfg is None:  # callers gate on this; the check narrows the type
        return
    name_filter = (
        name_filter_override if name_filter_override is not None else unifi_cfg.name_filter
    )
    report = unifi_status_engine(
        UnifiNetworkController(unifi_cfg),
        audit=audit,
        console=unifi_cfg.base_url,
        action="unifi_status",
        target=f"unifi-network:{unifi_cfg.base_url}",
        name_filter=name_filter,
        timeout=unifi_cfg.timeout,
    )
    # A console failure is reported (not raised); obi still exits cleanly (FR-003/FR-009).
    if report.outcome is not SourceOutcome.REACHABLE:
        detail = f" — {report.detail}" if report.detail else ""
        err.print(f"[yellow]unifi:[/yellow] console {report.outcome.value}{detail}")
    elif report.detail:  # partial result — one console surface was unavailable (FR-007)
        err.print(f"[yellow]unifi:[/yellow] partial result — {report.detail}")
    if json_output:
        sys.stdout.write(unifi_report_to_json(report) + "\n")
    else:
        _render_unifi_report(report, console)


def _siklu_status_section(
    config: Config,
    audit: AuditTrail,
    console: Console,
    *,
    json_output: bool,
    name_filter_override: str | None,
    timeout_override: float | None,
) -> None:
    from obi.core.drivers.siklu import SikluDriver
    from obi.core.engine import siklu_status
    from obi.core.serialize import siklu_report_to_json

    siklu_cfg = config.siklu
    if siklu_cfg is None:  # callers gate on this; the check narrows the type
        return
    name_filter = (
        name_filter_override if name_filter_override is not None else siklu_cfg.name_filter
    )
    report = siklu_status(
        SikluDriver(siklu_cfg),
        siklu_cfg,
        audit=audit,
        name_filter=name_filter,
        timeout=timeout_override if timeout_override is not None else siklu_cfg.timeout,
    )
    if json_output:
        sys.stdout.write(siklu_report_to_json(report) + "\n")
    else:
        _render_siklu_report(report, console)


@app.command()
def status(
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON instead of a table.")
    ] = False,
    scope: Annotated[
        Path | None,
        typer.Option("--scope", help="Path to the config/scope file (default: XDG config)."),
    ] = None,
    timeout: Annotated[
        float | None, typer.Option("--timeout", help="Per-device timeout in seconds.")
    ] = None,
    concurrency: Annotated[
        int | None, typer.Option("--concurrency", help="Max concurrent device queries.")
    ] = None,
    filter_: Annotated[
        str | None,
        typer.Option(
            "--filter",
            help="Only report UISP devices whose name contains this (case-insensitive).",
        ),
    ] = None,
) -> None:
    """Report each device's live health.

    Mikrotik devices come from the persisted inventory (run ``obi routeros discover`` first); UISP
    devices are queried live from the controller. When both vendors are configured, the
    Mikrotik report is shown first, then a separate UISP section (they are not merged).
    """

    setup_logging()
    logger = logging.getLogger("obi")
    err = Console(stderr=True)
    config_path = scope or default_config_path()

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        err.print(f"[red]config error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc

    warning = perms_warning(config_path)
    if warning:
        logger.warning("insecure config permissions", extra={"detail": warning})
        err.print(f"[yellow]warning:[/yellow] {warning}")

    if timeout is not None:
        config.timeout = timeout
    if concurrency is not None:
        config.concurrency = concurrency

    from obi.core.audit import AuditTrail  # local import keeps module import graph shallow

    audit = AuditTrail(config.audit_path)
    console = Console()

    with _audit_capture(config, "cli:status"):
        # Mikrotik (roster-sourced) report — unchanged from feature 002 (FR-007).
        if config.routeros is not None:
            _mikrotik_status_section(config, audit, console, err, json_output=json_output)

        # UISP (live) report — feature 003. Queried fresh; never persisted (FR-017).
        if config.uisp is not None:
            _uisp_status_section(
                config, audit, console, err, json_output=json_output, name_filter_override=filter_
            )

        # UniFi Network (live) report — feature 011. Queried fresh per site; never persisted. (The
        # account-level Site Manager surface has its own `obi unifi sm` command.)
        if config.unifi_network is not None:
            _unifi_status_section(
                config, audit, console, err, json_output=json_output, name_filter_override=filter_
            )

        # Siklu EtherHaul (live) section — feature 020. Queried fresh over SSH per radio; never
        # persisted. Its own section (vendors render independently — no model-level merge), so
        # existing vendors' output is byte-for-byte unchanged (FR-018).
        if config.siklu is not None:
            _siklu_status_section(
                config,
                audit,
                console,
                json_output=json_output,
                name_filter_override=filter_,
                timeout_override=timeout,
            )


netbox_app = typer.Typer(
    add_completion=False,
    help=(
        "Commands for NetBox, the documented source of truth (DCIM). `obi netbox inventory` "
        "reads the documented devices (intended state, not live health); `obi netbox drift` "
        "reconciles that documentation against obi's discovered RouterOS roster."
    ),
)
app.add_typer(netbox_app, name="netbox")


@netbox_app.command("inventory")
def netbox_inventory_command(
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON instead of a table.")
    ] = False,
    scope: Annotated[
        Path | None,
        typer.Option("--scope", help="Path to the config/scope file (default: XDG config)."),
    ] = None,
    timeout: Annotated[
        float | None, typer.Option("--timeout", help="NetBox request timeout in seconds.")
    ] = None,
    filter_: Annotated[
        str | None,
        typer.Option(
            "--filter",
            help="Only report devices whose name contains this (case-insensitive).",
        ),
    ] = None,
) -> None:
    """Report NetBox's documented device inventory (read-only, source of truth).

    NetBox documents the network's intended state (sites, devices, IPs); this is documentation,
    not live health. For live device status use ``obi status``.
    """

    setup_logging()
    logger = logging.getLogger("obi")
    err = Console(stderr=True)
    config_path = scope or default_config_path()

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        err.print(f"[red]config error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc

    warning = perms_warning(config_path)
    if warning:
        logger.warning("insecure config permissions", extra={"detail": warning})
        err.print(f"[yellow]warning:[/yellow] {warning}")

    if config.netbox is None:
        err.print(
            "[red]netbox not configured:[/red] add a \\[netbox] section (base_url + token) to "
            f"{config_path} — see README.md § NetBox."
        )
        raise typer.Exit(code=2)

    if timeout is not None:
        config.netbox.timeout = timeout

    from obi.core.audit import AuditTrail  # local import keeps module import graph shallow
    from obi.core.drivers.netbox import NetboxSource
    from obi.core.engine import netbox_inventory
    from obi.core.serialize import netbox_report_to_json

    audit = AuditTrail(config.audit_path)
    console = Console()

    name_filter = filter_ if filter_ is not None else config.netbox.name_filter
    with _audit_capture(config, "cli:netbox-inventory"):
        report = netbox_inventory(
            NetboxSource(config.netbox),
            audit=audit,
            source_host=config.netbox.base_url,
            name_filter=name_filter,
            timeout=config.netbox.timeout,
        )
    # A source failure is reported (not raised); obi still exits cleanly (FR-011/SC-005).
    if report.outcome is not SourceOutcome.REACHABLE:
        detail = f" — {report.detail}" if report.detail else ""
        err.print(f"[yellow]netbox:[/yellow] {report.outcome.value}{detail}")
    if json_output:
        sys.stdout.write(netbox_report_to_json(report) + "\n")
    else:
        _render_netbox_report(report, console)


@netbox_app.command("drift")
def netbox_drift(
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON instead of a table.")
    ] = False,
    scope: Annotated[
        Path | None,
        typer.Option("--scope", help="Path to the config/scope file (default: XDG config)."),
    ] = None,
    timeout: Annotated[
        float | None, typer.Option("--timeout", help="NetBox request timeout in seconds.")
    ] = None,
    filter_: Annotated[
        str | None,
        typer.Option(
            "--filter",
            help="Only reconcile devices whose name contains this (case-insensitive).",
        ),
    ] = None,
) -> None:
    """Reconcile NetBox's documented RouterOS inventory against obi's live discovery roster.

    READ-ONLY presence drift: which live MikroTik devices are missing from NetBox (to add) and which
    documented ones are no longer live (retire candidates, for your review). obi writes nothing —
    applying changes is a separate, not-yet-built operation. Uses the saved roster, so run
    ``obi routeros discover`` first for fresh reality.
    """

    setup_logging()
    logger = logging.getLogger("obi")
    err = Console(stderr=True)
    config_path = scope or default_config_path()

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        err.print(f"[red]config error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc

    warning = perms_warning(config_path)
    if warning:
        logger.warning("insecure config permissions", extra={"detail": warning})
        err.print(f"[yellow]warning:[/yellow] {warning}")

    if config.netbox is None:
        err.print(
            "[red]netbox not configured:[/red] add a \\[netbox] section (base_url + token) to "
            f"{config_path} — see README.md § NetBox."
        )
        raise typer.Exit(code=2)

    if timeout is not None:
        config.netbox.timeout = timeout

    from obi.core.audit import AuditTrail
    from obi.core.drivers.netbox import NetboxSource
    from obi.core.engine import netbox_reconcile
    from obi.core.serialize import drift_report_to_json

    audit = AuditTrail(config.audit_path)
    console = Console()

    name_filter = filter_ if filter_ is not None else config.netbox.name_filter
    with _audit_capture(config, "cli:netbox-drift"):
        report = netbox_reconcile(
            NetboxSource(config.netbox),
            audit=audit,
            source_host=config.netbox.base_url,
            inventory_path=config.inventory_path,
            name_filter=name_filter,
            timeout=config.netbox.timeout,
        )
    # A source failure / missing reality is reported (not raised); obi still exits cleanly (FR-011).
    if report.source_outcome is not SourceOutcome.REACHABLE:
        detail = f" — {report.detail}" if report.detail else ""
        err.print(f"[yellow]netbox:[/yellow] {report.source_outcome.value}{detail}")
    if json_output:
        sys.stdout.write(drift_report_to_json(report) + "\n")
    else:
        _render_drift_report(report, console)


uisp_app = typer.Typer(
    add_completion=False,
    help=(
        "Commands for the UISP (Ubiquiti) controller. `obi uisp status` reports live device "
        "health from the controller — the same UISP section `obi status` shows. Controller "
        "event/outage logs live under `obi uisp logs`."
    ),
)
app.add_typer(uisp_app, name="uisp")


@uisp_app.command("status")
def uisp_vendor_status(
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON instead of a table.")
    ] = False,
    scope: Annotated[
        Path | None,
        typer.Option("--scope", help="Path to the config/scope file (default: XDG config)."),
    ] = None,
    timeout: Annotated[
        float | None, typer.Option("--timeout", help="UISP request timeout in seconds.")
    ] = None,
    filter_: Annotated[
        str | None,
        typer.Option(
            "--filter",
            help="Only report UISP devices whose name contains this (case-insensitive).",
        ),
    ] = None,
) -> None:
    """Report live health for UISP-managed devices only.

    The same UISP section ``obi status`` shows, without the other vendors. Queried fresh from
    the controller; never persisted.
    """

    setup_logging()
    logger = logging.getLogger("obi")
    err = Console(stderr=True)
    config_path = scope or default_config_path()

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        err.print(f"[red]config error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc

    warning = perms_warning(config_path)
    if warning:
        logger.warning("insecure config permissions", extra={"detail": warning})
        err.print(f"[yellow]warning:[/yellow] {warning}")

    if config.uisp is None:
        err.print(
            "[red]uisp not configured:[/red] add a \\[uisp] section (base_url + token) to "
            f"{config_path}."
        )
        raise typer.Exit(code=2)

    if timeout is not None:
        config.uisp.timeout = timeout

    from obi.core.audit import AuditTrail  # local import keeps module import graph shallow

    audit = AuditTrail(config.audit_path)
    with _audit_capture(config, "cli:uisp-status"):
        _uisp_status_section(
            config, audit, Console(), err, json_output=json_output, name_filter_override=filter_
        )


@uisp_app.command("links")
def uisp_vendor_links(
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON instead of a table.")
    ] = False,
    scope: Annotated[
        Path | None,
        typer.Option("--scope", help="Path to the config/scope file (default: XDG config)."),
    ] = None,
    timeout: Annotated[
        float | None, typer.Option("--timeout", help="UISP request timeout in seconds.")
    ] = None,
    filter_: Annotated[
        str | None,
        typer.Option(
            "--filter",
            help="Only links where either endpoint name contains this (case-insensitive).",
        ),
    ] = None,
) -> None:
    """Report live RF health for every wireless link the controller knows (feature 034).

    One row per wireless data-link: endpoints, state, signal, SSID, frequency, distance, and
    each end's radio quality (link score, per-radio signal) plus theoretical capacity — joined
    from the controller's device list. Read-only; queried fresh; never persisted.
    """

    setup_logging()
    logger = logging.getLogger("obi")
    err = Console(stderr=True)
    config_path = scope or default_config_path()

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        err.print(f"[red]config error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc

    warning = perms_warning(config_path)
    if warning:
        logger.warning("insecure config permissions", extra={"detail": warning})
        err.print(f"[yellow]warning:[/yellow] {warning}")

    if config.uisp is None:
        err.print(
            "[red]uisp not configured:[/red] add a \\[uisp] section (base_url + token) to "
            f"{config_path}."
        )
        raise typer.Exit(code=2)

    from obi.core.audit import AuditTrail  # local import keeps module import graph shallow
    from obi.core.drivers.uisp import UispController
    from obi.core.engine import uisp_links
    from obi.core.serialize import uisp_links_report_to_json

    uisp_cfg = config.uisp
    with _audit_capture(config, "cli:uisp-links"):
        report = uisp_links(
            UispController(uisp_cfg),
            audit=AuditTrail(config.audit_path),
            controller=uisp_cfg.base_url,
            name_filter=filter_ if filter_ is not None else uisp_cfg.name_filter,
            ignore_names=uisp_cfg.ignore_names,  # feature 035: UISP-scoped exclude
            timeout=timeout if timeout is not None else uisp_cfg.timeout,
            reason="operator ran `obi uisp links`",
        )
    if report.outcome is not SourceOutcome.REACHABLE:
        detail = f" ({report.detail})" if report.detail else ""
        err.print(f"[yellow]uisp:[/yellow] controller {report.outcome.value}{detail}")
    if json_output:
        sys.stdout.write(uisp_links_report_to_json(report) + "\n")
    else:
        _render_uisp_links(report, Console())


unifi_app = typer.Typer(
    add_completion=False,
    help=(
        "Commands for the UniFi (Ubiquiti) console. `obi unifi status` reports live device "
        "health from the per-console Network Integration API — the same UniFi section "
        "`obi status` shows; `obi unifi sm` reads the account-level Site Manager API. "
        "(Controller system logs live under `obi unifi logs`.)"
    ),
)
app.add_typer(unifi_app, name="unifi")


@unifi_app.command("status")
def unifi_vendor_status(
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON instead of a table.")
    ] = False,
    scope: Annotated[
        Path | None,
        typer.Option("--scope", help="Path to the config/scope file (default: XDG config)."),
    ] = None,
    timeout: Annotated[
        float | None, typer.Option("--timeout", help="UniFi request timeout in seconds.")
    ] = None,
    filter_: Annotated[
        str | None,
        typer.Option(
            "--filter",
            help="Only report UniFi devices whose name contains this (case-insensitive).",
        ),
    ] = None,
) -> None:
    """Report live health for UniFi devices only (feature 011).

    The same UniFi section ``obi status`` shows, without the other vendors. Reads the configured
    [unifi-network] console live — the official integration API (per the selected ``sites``)
    plus the undocumented controller API for richer health, merged per device.
    """

    setup_logging()
    logger = logging.getLogger("obi")
    err = Console(stderr=True)
    config_path = scope or default_config_path()

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        err.print(f"[red]config error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc

    warning = perms_warning(config_path)
    if warning:
        logger.warning("insecure config permissions", extra={"detail": warning})
        err.print(f"[yellow]warning:[/yellow] {warning}")

    if config.unifi_network is None:
        err.print(
            "[red]unifi not configured:[/red] add a \\[unifi-network] section (base_url + "
            f"api_key and/or token) to {config_path} — see README.md § UniFi controller events."
        )
        raise typer.Exit(code=2)

    if timeout is not None:
        config.unifi_network.timeout = timeout

    from obi.core.audit import AuditTrail  # local import keeps module import graph shallow

    audit = AuditTrail(config.audit_path)
    with _audit_capture(config, "cli:unifi-status"):
        _unifi_status_section(
            config, audit, Console(), err, json_output=json_output, name_filter_override=filter_
        )


siklu_app = typer.Typer(
    add_completion=False,
    help=(
        "Commands for the Siklu EtherHaul point-to-point radios. `obi siklu status` reads each "
        "radio live over SSH — the same Siklu section `obi status` shows; `obi siklu links` "
        "pairs the two ends of each hop into one logical link."
    ),
)
app.add_typer(siklu_app, name="siklu")


def _siklu_report_command(
    *,
    json_output: bool,
    links: bool,
    scope: Path | None,
    timeout: float | None,
    filter_: str | None,
) -> None:
    """Shared body of `obi siklu status` and `obi siklu links` (feature 020)."""

    setup_logging()
    logger = logging.getLogger("obi")
    err = Console(stderr=True)
    config_path = scope or default_config_path()

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        err.print(f"[red]config error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc

    warning = perms_warning(config_path)
    if warning:
        logger.warning("insecure config permissions", extra={"detail": warning})
        err.print(f"[yellow]warning:[/yellow] {warning}")

    from obi.core.audit import AuditTrail  # local import keeps module import graph shallow
    from obi.core.drivers.siklu import SikluDriver
    from obi.core.engine import siklu_links, siklu_status
    from obi.core.serialize import siklu_report_to_json

    audit = AuditTrail(config.audit_path)
    console = Console()

    if config.siklu is None:
        # Aligned with the other vendor-scoped commands (2026-07-07, supersedes 020's FR-010
        # exit-0): an explicitly requested vendor view on an unconfigured vendor is an error.
        err.print(
            "[red]siklu not configured:[/red] add a \\[siklu] section with a non-empty 'hosts' "
            f"list (and username/password) to {config_path}."
        )
        raise typer.Exit(code=2)

    siklu_cfg = config.siklu
    name_filter = filter_ if filter_ is not None else siklu_cfg.name_filter
    with _audit_capture(config, "cli:siklu"):
        report = siklu_status(
            SikluDriver(siklu_cfg),
            siklu_cfg,
            audit=audit,
            name_filter=name_filter,
            timeout=timeout if timeout is not None else siklu_cfg.timeout,
        )
    if links:
        report.links = siklu_links(report, overrides=siklu_cfg.pairs)

    if json_output:
        sys.stdout.write(siklu_report_to_json(report) + "\n")
    elif links:
        _render_siklu_links(report.links, console)
    else:
        _render_siklu_report(report, console)


@siklu_app.command("status")
def siklu_vendor_status(
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON instead of a table.")
    ] = False,
    scope: Annotated[
        Path | None,
        typer.Option("--scope", help="Path to the config/scope file (default: XDG config)."),
    ] = None,
    timeout: Annotated[
        float | None, typer.Option("--timeout", help="Per-radio SSH timeout in seconds.")
    ] = None,
    filter_: Annotated[
        str | None,
        typer.Option(
            "--filter",
            help="Only report Siklu radios whose name contains this (case-insensitive).",
        ),
    ] = None,
) -> None:
    """Report Siklu EtherHaul radios from the configured [siklu] roster (feature 020).

    Reads each radio live over SSH (read-only ``show`` commands only) — identity, system health
    (uptime, temperature), RF link health (up/down, RSSI, CINR, modulation, TX power, capacity),
    and per-Ethernet-port detail. Siklu radios also appear in ``obi status`` and ``obi chat``.
    """

    _siklu_report_command(
        json_output=json_output, links=False, scope=scope, timeout=timeout, filter_=filter_
    )


@siklu_app.command("links")
def siklu_vendor_links(
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON instead of a table.")
    ] = False,
    scope: Annotated[
        Path | None,
        typer.Option("--scope", help="Path to the config/scope file (default: XDG config)."),
    ] = None,
    timeout: Annotated[
        float | None, typer.Option("--timeout", help="Per-radio SSH timeout in seconds.")
    ] = None,
    filter_: Annotated[
        str | None,
        typer.Option(
            "--filter",
            help="Only report Siklu radios whose name contains this (case-insensitive).",
        ),
    ] = None,
) -> None:
    """Show each Siklu point-to-point hop as one logical link (both ends, asymmetry flags).

    Reads the same live radio data as ``obi siklu status``, then pairs the two ends of each hop
    (by the BM naming convention or explicit [siklu] pair overrides) and flags hard disagreement
    between them.
    """

    _siklu_report_command(
        json_output=json_output, links=True, scope=scope, timeout=timeout, filter_=filter_
    )


@unifi_app.command("sm")
def unifi_sm(
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON instead of a table.")
    ] = False,
    scope: Annotated[
        Path | None,
        typer.Option("--scope", help="Path to the config/scope file (default: XDG config)."),
    ] = None,
    timeout: Annotated[
        float | None, typer.Option("--timeout", help="UniFi request timeout in seconds.")
    ] = None,
    filter_: Annotated[
        str | None,
        typer.Option(
            "--filter",
            help="Only report UniFi devices whose name contains this (case-insensitive).",
        ),
    ] = None,
) -> None:
    """Report UniFi devices from Ubiquiti's account-level Site Manager API (feature 011).

    Reads the configured [unifi-sm] account live (``api.ui.com/v1/devices``) — the whole account's
    inventory + reachability across all sites/consoles in one call (no per-device cpu/memory). This
    surface is command-only; it does not appear in ``obi status`` or ``obi chat``.
    """

    setup_logging()
    logger = logging.getLogger("obi")
    err = Console(stderr=True)
    config_path = scope or default_config_path()

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        err.print(f"[red]config error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc

    warning = perms_warning(config_path)
    if warning:
        logger.warning("insecure config permissions", extra={"detail": warning})
        err.print(f"[yellow]warning:[/yellow] {warning}")

    if config.unifi_sm is None:
        err.print(
            "[red]unifi-sm not configured:[/red] add a \\[unifi-sm] section (api_key) to "
            f"{config_path} — see README.md § UniFi controller events."
        )
        raise typer.Exit(code=2)

    if timeout is not None:
        config.unifi_sm.timeout = timeout

    from obi.core.audit import AuditTrail  # local import keeps module import graph shallow
    from obi.core.drivers.unifi_sm import UnifiSiteManagerController
    from obi.core.engine import unifi_status as unifi_status_engine
    from obi.core.serialize import unifi_report_to_json

    cfg = config.unifi_sm
    audit = AuditTrail(config.audit_path)
    console = Console()

    name_filter = filter_ if filter_ is not None else cfg.name_filter
    with _audit_capture(config, "cli:unifi-sm"):
        report = unifi_status_engine(
            UnifiSiteManagerController(cfg),
            audit=audit,
            console=cfg.base_url,
            action="unifi_sitemanager",
            target=f"unifi-sm:{cfg.base_url}",
            name_filter=name_filter,
            timeout=cfg.timeout,
        )
    if report.outcome is not SourceOutcome.REACHABLE:
        detail = f" — {report.detail}" if report.detail else ""
        err.print(f"[yellow]unifi-sm:[/yellow] {report.outcome.value}{detail}")
    if json_output:
        sys.stdout.write(unifi_report_to_json(report) + "\n")
    else:
        _render_unifi_report(report, console)


def _fmt_sample(sample: MetricSample) -> str:
    if sample.value is None:
        return f"{sample.name}=unavailable"
    suffix = f" ({sample.note})" if sample.note else ""
    return f"{sample.name}={sample.value}{suffix}"


def _render_snmp_reports(reports: list[MetricReport], profile_name: str, console: Console) -> None:
    """Render an SNMP sweep (feature 012): one block per device, scalars then per-row table data."""

    from obi.core.drivers.snmp_profiles import get_profile

    label_column = get_profile(profile_name).table_index_label
    lines = [f"SNMP metrics (profile={profile_name}; {len(reports)} device(s)):"]
    for report in reports:
        name = report.target.identity or report.target.address
        head = f"- {name} @ {report.target.address} [snmp/{report.target.source}]: "
        if report.outcome is not SnmpOutcome.REACHABLE:
            note = "; ".join(report.notes)
            lines.append(head + report.outcome.value + (f" ({note})" if note else ""))
            continue
        lines.append(head + "reachable")
        scalars = [s for s in report.samples if s.index is None]
        if scalars:
            lines.append("    " + ", ".join(_fmt_sample(s) for s in scalars))
        rows: dict[tuple[str, str | None], list[MetricSample]] = {}
        for sample in report.samples:
            if sample.index is not None:
                rows.setdefault((sample.index, sample.label), []).append(sample)
        for (index, label), samples in rows.items():
            body = ", ".join(_fmt_sample(s) for s in samples if s.name != label_column)
            lines.append(f"    {label or index}: {body}")
    # The [snmp/<source>] labels and device-supplied strings (sysName/ifName/sysDescr) are plain
    # data, not Rich markup — render with markup off so brackets print literally (and an untrusted
    # device string can never inject styling). An address in two sources is deduped to one line
    # labeled with both (e.g. [snmp/mikrotik-roster+netbox]).
    console.print("\n".join(lines), markup=False, highlight=False)


@app.command()
def chat(
    question: Annotated[
        str | None,
        typer.Argument(
            help="Ask one question and exit. Omit for an interactive session.",
        ),
    ] = None,
    scope: Annotated[
        Path | None,
        typer.Option("--scope", help="Path to the config/scope file (default: XDG config)."),
    ] = None,
    model: Annotated[
        str | None, typer.Option("--model", help="Override the chat model for this run.")
    ] = None,
    max_tokens: Annotated[
        int | None, typer.Option("--max-tokens", help="Override the response token cap.")
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="One-shot only: emit {question, answer, tools_used} as JSON."),
    ] = False,
) -> None:
    """Ask obi about the network in plain language (read-only): interactive, or one-shot."""

    setup_logging()
    logger = logging.getLogger("obi")
    err = Console(stderr=True)
    config_path = scope or default_config_path()

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        err.print(f"[red]config error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc

    warning = perms_warning(config_path)
    if warning:
        logger.warning("insecure config permissions", extra={"detail": warning})
        err.print(f"[yellow]warning:[/yellow] {warning}")

    if model is not None:
        config.chat.model = model
    if max_tokens is not None:
        config.chat.max_tokens = max_tokens

    from obi.chat.session import build_runtime, print_answer, run_interactive, run_once
    from obi.core import inventory as inv
    from obi.core.llm import ChatUnavailable

    try:
        runtime = build_runtime(config)
    except ChatUnavailable as exc:
        err.print(f"[red]chat unavailable:[/red] {exc}")
        raise typer.Exit(code=3) from exc

    if runtime.context_warning:
        logger.warning("chat context not loaded", extra={"detail": runtime.context_warning})
        err.print(f"[yellow]warning:[/yellow] {runtime.context_warning}")

    if inv.load(config.inventory_path) is None:
        err.print(
            "[red]no inventory found:[/red] run `obi routeros discover` first so obi has "
            f"something to talk about (expected at {config.inventory_path})."
        )
        raise typer.Exit(code=4)

    if question is None:
        with _audit_capture(config, "chat"):
            code = run_interactive(runtime, Console(), err)
        raise typer.Exit(code=code)

    try:
        with _audit_capture(config, "chat"):
            result = run_once(runtime, question)
    except ChatUnavailable as exc:
        err.print(f"[red]chat unavailable:[/red] {exc}")
        raise typer.Exit(code=3) from exc

    if json_output:
        import json as json_lib

        payload = {"question": question, "answer": result.answer, "tools_used": result.tools_used}
        sys.stdout.write(json_lib.dumps(payload) + "\n")
    else:
        print_answer(Console(), result.answer)  # 082: render markdown, don't parse [brackets]


icmp_app = typer.Typer(
    add_completion=False,
    help=(
        "ICMP reachability monitoring (feature 072). `obi icmp run` pings every RouterOS device "
        "and Siklu radio from this host and records up/down state (the */5 cron entrypoint); "
        "`obi icmp status` shows the current reachability. Down/up alerting is the [watch.icmp] "
        "pass in `obi watch`."
    ),
)
app.add_typer(icmp_app, name="icmp")


@icmp_app.command("run")
def icmp_run(
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit the cycle summary as JSON instead of text.")
    ] = False,
    scope: Annotated[
        Path | None,
        typer.Option("--scope", help="Path to the config/scope file (default: XDG config)."),
    ] = None,
) -> None:
    """Ping every monitored device once and update the reachability store (the */5 cron entrypoint).

    Read-only: sends ICMP echoes from this host to each ``present_in_latest`` inventory device plus
    the [siklu] radios; records only current up/down state. Down/up alerts are raised separately by
    the [watch.icmp] pass in ``obi watch``.
    """

    setup_logging()
    logger = logging.getLogger("obi")
    err = Console(stderr=True)
    config_path = scope or default_config_path()

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        err.print(f"[red]config error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc

    warning = perms_warning(config_path)
    if warning:
        logger.warning("insecure config permissions", extra={"detail": warning})
        err.print(f"[yellow]warning:[/yellow] {warning}")

    from obi.core import icmp as icmp_mod
    from obi.core import icmpstore
    from obi.core.config import IcmpSettings
    from obi.core.inventory import load as load_inventory
    from obi.core.models import _utcnow

    settings = config.icmp or IcmpSettings()
    siklu_hosts = list(config.siklu.hosts) if config.siklu is not None else []
    inventory = load_inventory(config.inventory_path)
    # 095: the snapshot-derived UISP backhaul radios join the roster HERE too, so a manual
    # `obi icmp run` and the dash cycle resolve the same target set and never prune each other.
    uisp_targets: list[icmp_mod.Target] = []
    if settings.include_uisp_ptp:
        ignore_names = list(config.uisp.ignore_names) if config.uisp is not None else []
        uisp_targets, _maintenance, uisp_note = icmp_mod.load_uisp_ptp_targets(
            settings, ignore_names=ignore_names
        )
        if uisp_note is not None:
            logger.info("icmp uisp roster", extra={"detail": uisp_note})
    targets = icmp_mod.resolve_targets(
        inventory,
        siklu_hosts,
        ignore=settings.ignore_devices,
        include_siklu=settings.include_siklu,
        extra_hosts=settings.extra_hosts,
        uisp_targets=uisp_targets,
    )
    if not targets:
        err.print(
            "[yellow]no ICMP targets:[/yellow] no discovered inventory at "
            f"{config.inventory_path} and no [siklu] hosts — run `obi routeros discover` first."
        )
        raise typer.Exit(code=0)

    now_us = int(_utcnow().timestamp() * 1_000_000)
    with _audit_capture(config, "cli:icmp-run"):
        conn = icmpstore.connect(settings.db_path)
        try:
            summary = icmp_mod.run_collect(settings, targets, conn=conn, now_us=now_us)
        finally:
            conn.close()

    logger.info(
        "icmp cycle complete",
        extra={
            "probed": summary.probed,
            "up": summary.up,
            "down": summary.down,
            "new_down": summary.new_down,
            "recovered": summary.recovered,
        },
    )
    if json_output:
        import json as json_lib

        sys.stdout.write(
            json_lib.dumps(
                {
                    "probed": summary.probed,
                    "up": summary.up,
                    "down": summary.down,
                    "new_down": summary.new_down,
                    "recovered": summary.recovered,
                    "pruned": summary.pruned,
                }
            )
            + "\n"
        )
    else:
        Console().print(
            f"ICMP: probed {summary.probed} — {summary.up} up, {summary.down} down "
            f"({summary.new_down} newly down, {summary.recovered} recovered)"
        )


@icmp_app.command("status")
def icmp_status(
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON instead of a table.")
    ] = False,
    scope: Annotated[
        Path | None,
        typer.Option("--scope", help="Path to the config/scope file (default: XDG config)."),
    ] = None,
) -> None:
    """Show each monitored device's current reachability from the feature-072 state store."""

    setup_logging()
    err = Console(stderr=True)
    config_path = scope or default_config_path()

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        err.print(f"[red]config error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc

    from obi.core import icmpstore
    from obi.core.config import IcmpSettings
    from obi.core.models import _utcnow

    db_path = (config.icmp or IcmpSettings()).db_path.expanduser()
    if not db_path.exists():
        err.print(
            "[yellow]no ICMP data yet:[/yellow] the collector has not run — schedule "
            "`obi icmp run` (*/5 cron) or run it once."
        )
        raise typer.Exit(code=0)

    try:
        conn = icmpstore.connect_readonly(db_path)
        try:
            states = icmpstore.load_states(conn)
        finally:
            conn.close()
    except icmpstore.StoreError as exc:
        err.print(f"[red]icmp store error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc

    now_us = int(_utcnow().timestamp() * 1_000_000)
    if json_output:
        import json as json_lib

        sys.stdout.write(
            json_lib.dumps(
                [
                    {
                        "device": s.device,
                        "address": s.address,
                        "up": s.up,
                        "last_seen_us": s.last_seen_us,
                        "last_change_us": s.last_change_us,
                    }
                    for s in states
                ]
            )
            + "\n"
        )
    else:
        _render_icmp_status(states, now_us, Console())


weather_app = typer.Typer(
    add_completion=False,
    help=(
        "Site weather snapshots (feature 110). `obi weather run` fetches current "
        "conditions + the 24-hour hourly forecast (Open-Meteo) and the active NWS alerts for "
        "the configured zone into a local store (the hourly cron entrypoint); `obi weather status` "
        "shows snapshot freshness and coverage. The `weather_forecast` chat tool answers from "
        "the store only, so a fetch failure just ages the data — it never breaks answers."
    ),
)
app.add_typer(weather_app, name="weather")


@weather_app.command("run")
def weather_run(
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit the cycle summary as JSON instead of text.")
    ] = False,
    scope: Annotated[
        Path | None,
        typer.Option("--scope", help="Path to the config/scope file (default: XDG config)."),
    ] = None,
) -> None:
    """Snapshot the site weather once into the local store (the hourly cron entrypoint).

    Fetches the two public sources independently; a source failure is a RECORDED outcome on
    the snapshot (visible in `obi weather status` and served honestly by the chat tool),
    not a process failure — the exit code stays 0 for any recorded cycle.
    """

    setup_logging()
    logger = logging.getLogger("obi")
    err = Console(stderr=True)
    config_path = scope or default_config_path()

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        err.print(f"[red]config error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc

    warning = perms_warning(config_path)
    if warning:
        logger.warning("insecure config permissions", extra={"detail": warning})
        err.print(f"[yellow]warning:[/yellow] {warning}")

    if config.weather is None:
        err.print(
            "[red]weather not configured:[/red] add a \\[weather] section with latitude and "
            "longitude (feature 137: there is no built-in location)."
        )
        raise typer.Exit(code=2)
    from obi.core import weather as weather_mod
    from obi.core import weatherstore
    from obi.core.models import _utcnow

    settings = config.weather
    now_us = int(_utcnow().timestamp() * 1_000_000)
    with _audit_capture(config, "cli:weather-run"):
        conn = weatherstore.connect(settings.db_path.expanduser())
        try:
            summary = weather_mod.run_cycle(
                settings, conn, now_us=now_us, tz_name=config.site.timezone
            )
        finally:
            conn.close()

    logger.info(
        "weather cycle complete",
        extra={
            "forecast_ok": summary.forecast_ok,
            "forecast_error": summary.forecast_error,
            "alerts_ok": summary.alerts_ok,
            "alerts_error": summary.alerts_error,
            "hourly_rows": summary.hourly_rows,
            "active_alerts": summary.active_alerts,
            "pruned": summary.pruned,
        },
    )
    if json_output:
        import json as json_lib

        sys.stdout.write(
            json_lib.dumps(
                {
                    "forecast_ok": summary.forecast_ok,
                    "forecast_error": summary.forecast_error,
                    "alerts_ok": summary.alerts_ok,
                    "alerts_error": summary.alerts_error,
                    "hourly_rows": summary.hourly_rows,
                    "active_alerts": summary.active_alerts,
                    "pruned": summary.pruned,
                }
            )
            + "\n"
        )
    else:
        forecast = "ok" if summary.forecast_ok else f"FAILED ({summary.forecast_error})"
        alerts = (
            f"ok ({summary.active_alerts} active)"
            if summary.alerts_ok
            else f"FAILED ({summary.alerts_error})"
        )
        Console().print(
            f"Weather: forecast {forecast} — {summary.hourly_rows} hourly rows; alerts {alerts}"
        )


@weather_app.command("status")
def weather_status(
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON instead of text.")
    ] = False,
    scope: Annotated[
        Path | None,
        typer.Option("--scope", help="Path to the config/scope file (default: XDG config)."),
    ] = None,
) -> None:
    """Show the newest snapshot's age, per-source outcome, and the store's history coverage."""

    setup_logging()
    err = Console(stderr=True)
    config_path = scope or default_config_path()

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        err.print(f"[red]config error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc

    if config.weather is None:
        err.print(
            "[red]weather not configured:[/red] add a \\[weather] section with latitude and "
            "longitude (feature 137: there is no built-in location)."
        )
        raise typer.Exit(code=2)
    from obi.core import weatherstore
    from obi.core.models import _utcnow

    db_path = config.weather.db_path.expanduser()
    if not db_path.exists():
        err.print(
            "[yellow]no weather data yet:[/yellow] the collector has not run — schedule "
            "`obi weather run` (hourly cron) or run it once."
        )
        raise typer.Exit(code=0)

    try:
        conn = weatherstore.connect_readonly(db_path)
        try:
            snap = weatherstore.newest_snapshot(conn)
            span = weatherstore.coverage(conn)
        finally:
            conn.close()
    except weatherstore.StoreError as exc:
        err.print(f"[red]weather store error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc

    now_us = int(_utcnow().timestamp() * 1_000_000)
    if json_output:
        import json as json_lib

        sys.stdout.write(
            json_lib.dumps(
                {
                    "db_path": str(db_path),
                    "newest_fetched_us": snap.fetched_us if snap else None,
                    "age_s": (now_us - snap.fetched_us) // 1_000_000 if snap else None,
                    "forecast_ok": snap.forecast_ok if snap else None,
                    "forecast_error": snap.forecast_error if snap else None,
                    "alerts_ok": snap.alerts_ok if snap else None,
                    "alerts_error": snap.alerts_error if snap else None,
                    "coverage_first_us": span[0] if span else None,
                    "coverage_last_us": span[1] if span else None,
                }
            )
            + "\n"
        )
    elif snap is None:
        Console().print("Weather store exists but holds no snapshots yet.")
    else:
        age_s = max(0, (now_us - snap.fetched_us) // 1_000_000)
        age = f"{age_s // 3600}h {age_s % 3600 // 60}m" if age_s >= 3600 else f"{age_s // 60}m"
        forecast = "ok" if snap.forecast_ok else f"FAILED ({snap.forecast_error})"
        alerts = "ok" if snap.alerts_ok else f"FAILED ({snap.alerts_error})"
        days = ((span[1] - span[0]) // 86_400_000_000) if span else 0
        Console().print(
            f"Weather: newest snapshot {age} ago — forecast {forecast}, alerts {alerts}; "
            f"history spans {days}d ({db_path})"
        )


traffic_app = typer.Typer(
    add_completion=False,
    help=(
        "Event gate traffic (feature 112). `obi traffic run` polls the event dashboard's Travel "
        "Time Log and gate status into a local store (the 5-minute cron entrypoint) — the "
        "pre-event 403 is a recorded outcome, not an error; `obi traffic status` shows the "
        "newest entry, gate status, and poll outcomes. The `gate_traffic` chat tool answers "
        "from the store only, so a fetch failure just ages the data — it never breaks answers."
    ),
)
app.add_typer(traffic_app, name="traffic")


@traffic_app.command("run")
def traffic_run(
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit the cycle summary as JSON instead of text.")
    ] = False,
    scope: Annotated[
        Path | None,
        typer.Option("--scope", help="Path to the config/scope file (default: XDG config)."),
    ] = None,
) -> None:
    """Poll the dashboard once into the local store (the 5-minute cron entrypoint).

    Fetches the two public endpoints independently; a refusal (the pre-event 403) or failure
    is a RECORDED outcome on the poll (visible in `obi traffic status` and served honestly by
    the chat tool), not a process failure — the exit code stays 0 for any recorded cycle.
    """

    setup_logging()
    logger = logging.getLogger("obi")
    err = Console(stderr=True)
    config_path = scope or default_config_path()

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        err.print(f"[red]config error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc

    warning = perms_warning(config_path)
    if warning:
        logger.warning("insecure config permissions", extra={"detail": warning})
        err.print(f"[yellow]warning:[/yellow] {warning}")

    if config.traffic is None:
        err.print(
            "[red]traffic not configured:[/red] add a \\[traffic] section with base_url "
            "(feature 137: there is no built-in dashboard)."
        )
        raise typer.Exit(code=2)
    from obi.core import traffic as traffic_mod
    from obi.core import trafficstore
    from obi.core.models import _utcnow

    settings = config.traffic
    now_us = int(_utcnow().timestamp() * 1_000_000)
    with _audit_capture(config, "cli:traffic-run"):
        conn = trafficstore.connect(settings.db_path.expanduser())
        try:
            summary = traffic_mod.run_cycle(
                settings, conn, now_us=now_us, timezone=config.site.timezone
            )
        finally:
            conn.close()

    logger.info(
        "traffic cycle complete",
        extra={
            "feed_outcome": summary.feed_outcome,
            "feed_error": summary.feed_error,
            "status_outcome": summary.status_outcome,
            "status_error": summary.status_error,
            "new_entries": summary.new_entries,
            "pruned": summary.pruned,
        },
    )
    if json_output:
        import json as json_lib

        sys.stdout.write(
            json_lib.dumps(
                {
                    "feed_outcome": summary.feed_outcome,
                    "feed_error": summary.feed_error,
                    "status_outcome": summary.status_outcome,
                    "status_error": summary.status_error,
                    "new_entries": summary.new_entries,
                    "pruned": summary.pruned,
                }
            )
            + "\n"
        )
    else:
        feed = summary.feed_outcome + (f" ({summary.feed_error})" if summary.feed_error else "")
        status = summary.status_outcome + (
            f" ({summary.status_error})" if summary.status_error else ""
        )
        Console().print(
            f"Traffic: feed {feed} — {summary.new_entries} new entries; gate status {status}"
        )


@traffic_app.command("status")
def traffic_status(
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON instead of text.")
    ] = False,
    scope: Annotated[
        Path | None,
        typer.Option("--scope", help="Path to the config/scope file (default: XDG config)."),
    ] = None,
) -> None:
    """Show the newest travel-time entry, gate status, last poll outcomes, and coverage."""

    setup_logging()
    err = Console(stderr=True)
    config_path = scope or default_config_path()

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        err.print(f"[red]config error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc

    if config.traffic is None:
        err.print(
            "[red]traffic not configured:[/red] add a \\[traffic] section with base_url "
            "(feature 137: there is no built-in dashboard)."
        )
        raise typer.Exit(code=2)
    from obi.core import trafficstore
    from obi.core.models import _utcnow

    db_path = config.traffic.db_path.expanduser()
    if not db_path.exists():
        err.print(
            "[yellow]no traffic data yet:[/yellow] the collector has not run — schedule "
            "`obi traffic run` (*/5 cron) or run it once."
        )
        raise typer.Exit(code=0)

    try:
        conn = trafficstore.connect_readonly(db_path)
        try:
            poll = trafficstore.newest_poll(conn)
            entries = trafficstore.recent_entries(conn, limit=1)
            count = trafficstore.entry_count(conn)
            gate = trafficstore.newest_gate_status(conn)
            span = trafficstore.coverage(conn)
        finally:
            conn.close()
    except trafficstore.StoreError as exc:
        err.print(f"[red]traffic store error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc

    now_us = int(_utcnow().timestamp() * 1_000_000)

    def _age(us: int) -> str:
        age_s = max(0, (now_us - us) // 1_000_000)
        return f"{age_s // 3600}h {age_s % 3600 // 60}m" if age_s >= 3600 else f"{age_s // 60}m"

    if json_output:
        import json as json_lib

        newest = entries[0] if entries else None
        sys.stdout.write(
            json_lib.dumps(
                {
                    "db_path": str(db_path),
                    "last_poll_us": poll.fetched_us if poll else None,
                    "feed_outcome": poll.feed_outcome if poll else None,
                    "feed_error": poll.feed_error if poll else None,
                    "status_outcome": poll.status_outcome if poll else None,
                    "status_error": poll.status_error if poll else None,
                    "entry_count": count,
                    "newest_entry_text": newest.text if newest else None,
                    "newest_entry_us": (
                        (newest.published_us or newest.first_seen_us) if newest else None
                    ),
                    "gate_inbound": gate.inbound_state if gate else None,
                    "gate_outbound": gate.outbound_state if gate else None,
                    "gate_advisory": gate.advisory if gate else None,
                    "gate_captured_us": gate.captured_us if gate else None,
                    "coverage_first_us": span[0] if span else None,
                    "coverage_last_us": span[1] if span else None,
                }
            )
            + "\n"
        )
    elif poll is None:
        Console().print("Traffic store exists but holds no polls yet.")
    else:
        feed = poll.feed_outcome + (f" ({poll.feed_error})" if poll.feed_error else "")
        status = poll.status_outcome + (f" ({poll.status_error})" if poll.status_error else "")
        console = Console()
        console.print(
            f"Traffic: last poll {_age(poll.fetched_us)} ago — feed {feed}, "
            f"gate status {status}; {count} entries ({db_path})"
        )
        if entries:
            newest = entries[0]
            console.print(
                f"Newest entry ({_age(newest.published_us or newest.first_seen_us)} ago): "
                f"{newest.text}"
            )
        if gate is not None:
            lanes = (
                f"inbound {gate.inbound_state or 'not reported'}, "
                f"outbound {gate.outbound_state or 'not reported'}"
            )
            advisory = f" — {gate.advisory}" if gate.advisory else ""
            console.print(f"Gate ({_age(gate.captured_us)} ago): {lanes}{advisory}")


dash_app = typer.Typer(
    add_completion=False,
    help=(
        "Status dashboard (feature 095). `obi dash run` is the 1-minute cycle: the ICMP sweep, "
        "a netwatch read per router, the reference pings, and the store rollup — writing "
        "board.json for the DASH web board and paging red transitions (opt-in [dash.pager]) "
        "over the watch Slack DM path, plus real PlayaPage pages when [paging.alerts] is "
        "enabled (feature 102). `obi dash show` renders the board in the terminal."
    ),
)
app.add_typer(dash_app, name="dash")


mesh_app = typer.Typer(
    add_completion=False,
    help=(
        "Meshtastic mesh nodes (features 111/123/125/126). `locate` answers where a mesh "
        "node is — as a city address when [brc] is configured, naming a nearby art piece when "
        "the art dataset is downloaded (`obi playa fetch`) — instantly and passively "
        "from the mesh daemon's continuously refreshed data (it sends the rate-limited "
        "position requests automatically; a lookup never transmits). "
        "`daemon`/`status`/`standby`/`resume` manage the "
        "daemon. Requires a \\[meshtastic] section."
    ),
)
app.add_typer(mesh_app, name="mesh")


@mesh_app.command("locate")
def mesh_locate_command(
    node: Annotated[
        str, typer.Argument(help="Node to find: short name, long name, or !id.")
    ],
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to config.toml.")
    ] = None,
) -> None:
    """Where is a mesh node? Prints the same answer the mesh_locate chat tool gives."""

    setup_logging()
    err = Console(stderr=True)
    path = config_path or default_config_path()
    try:
        config = load_config(path)
    except ConfigError as exc:
        err.print(f"[red]config error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc
    warning = perms_warning(path)
    if warning:
        err.print(f"[yellow]{escape(warning)}[/yellow]")
    if config.meshtastic is None:
        err.print(
            f"[red]meshtastic not configured:[/red] add a \\[meshtastic] section to {path} "
            "(an empty section is a valid all-defaults opt-in)."
        )
        raise typer.Exit(code=2)
    from obi.core.audit import AuditTrail
    from obi.core.mesh import MeshLocator

    # A position request may transmit on the mesh, so the run is captured (source cli:mesh).
    with _audit_capture(config, "cli:mesh"):
        from obi.core.brcaddr import model_loader_for

        locator = MeshLocator(
            config.meshtastic,
            AuditTrail(config.audit_path),
            model_loader=model_loader_for(config.brc),
        )
        Console().print(escape(locator.locate(node, reason="cli: mesh locate", actor=None)))


@mesh_app.command("record")
def mesh_record_command(
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to config.toml.")
    ] = None,
) -> None:
    """One passive sweep (feature 117): read the radio's node table once and record every
    report_only node's fix into the position history. Never transmits. Quiet skips (radio
    absent/busy) exit 0 so the cron log stays calm; a store failure exits 1."""

    setup_logging()
    err = Console(stderr=True)
    path = config_path or default_config_path()
    try:
        config = load_config(path)
    except ConfigError as exc:
        err.print(f"[red]config error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc
    warning = perms_warning(path)
    if warning:
        err.print(f"[yellow]{escape(warning)}[/yellow]")
    if config.meshtastic is None:
        err.print(
            f"[red]meshtastic not configured:[/red] add a \\[meshtastic] section to {path} "
            "(an empty section is a valid all-defaults opt-in)."
        )
        raise typer.Exit(code=2)
    from obi.core.audit import AuditTrail
    from obi.core.mesh import MeshLocator

    # Pure read (the sweep cannot transmit); captured so the serial reads land in audit.
    with _audit_capture(config, "cli:mesh"):
        from obi.core.brcaddr import model_loader_for

        outcome = MeshLocator(
            config.meshtastic,
            AuditTrail(config.audit_path),
            model_loader=model_loader_for(config.brc),
        ).record_pass()
    if outcome.error is not None:
        err.print(f"[red]position store failed:[/red] {escape(outcome.error)}")
        raise typer.Exit(code=1)
    if outcome.skipped is not None:
        Console().print(f"skipped: {escape(outcome.skipped)}")
        return
    if outcome.recorded:
        Console().print(
            f"recorded {outcome.recorded} new fix(es); {outcome.tracked} tracked node(s) seen"
        )
    else:
        Console().print(f"no new fixes ({outcome.tracked} tracked node(s) seen)")


@mesh_app.command("track")
def mesh_track_command(
    node: Annotated[str, typer.Argument(help="Tracked node: name, operator alias, or !id.")],
    hours: Annotated[
        float, typer.Option("--hours", help="Look back this many hours (default 24).")
    ] = 24.0,
    since: Annotated[
        str | None, typer.Option("--since", help="ISO 8601 start (overrides --hours).")
    ] = None,
    until: Annotated[
        str | None, typer.Option("--until", help="ISO 8601 end (default: now).")
    ] = None,
    csv_out: Annotated[bool, typer.Option("--csv", help="Raw CSV export for mapping.")] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to config.toml.")
    ] = None,
) -> None:
    """The recorded position track (feature 117): time-ordered fixes with the address each
    rendered to at record time. Reads only the local history store — never the radio."""

    from datetime import datetime

    from obi.core import meshstore
    from obi.core.mesh import config_node_id

    setup_logging()
    err = Console(stderr=True)
    path = config_path or default_config_path()
    try:
        config = load_config(path)
    except ConfigError as exc:
        err.print(f"[red]config error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc
    if config.meshtastic is None:
        err.print(f"[red]meshtastic not configured:[/red] add a \\[meshtastic] section to {path}.")
        raise typer.Exit(code=2)
    settings = config.meshtastic

    node_id = config_node_id(settings, node)
    if node_id is None:
        allowed = ", ".join(settings.report_only) or "(nothing — report_only is empty)"
        Console().print(
            f"No tracked mesh node matches '{escape(node)}' — position history is "
            f"enabled only for {escape(allowed)}."
        )
        return
    display = settings.display_names.get(node_id) or node_id

    def parse_iso(text: str, flag: str) -> float:
        try:
            return datetime.fromisoformat(text).timestamp()
        except ValueError as exc:
            err.print(f"[red]{flag} is not an ISO 8601 timestamp:[/red] {escape(text)}")
            raise typer.Exit(code=2) from exc

    until_s = parse_iso(until, "--until") if until else _time.time()
    since_s = parse_iso(since, "--since") if since else until_s - hours * 3600.0
    range_text = (
        f"between {since} and {until or 'now'}" if since else f"in the last {hours:g} hours"
    )

    try:
        conn = meshstore.connect_readonly(settings.positions_db_path)
    except meshstore.StoreError:
        Console().print(f"no recorded fixes for {escape(display)} {range_text} (no history yet)")
        return
    try:
        fixes = meshstore.track(conn, node_id, since_s=int(since_s), until_s=int(until_s))
    finally:
        with contextlib.suppress(Exception):
            conn.close()

    if not fixes:
        Console().print(f"no recorded fixes for {escape(display)} {range_text}")
        return

    if csv_out:
        # Plain echo, not Rich: CSV must never be width-wrapped.
        typer.echo(
            "node_id,fix_time_s,observed_at_s,latitude,longitude,altitude_m,"
            "hops_away,snr_db,address"
        )
        for fix in fixes:
            typer.echo(
                f"{fix.node_id},{fix.fix_time_s},{fix.observed_at_s},"
                f"{fix.latitude:.7f},{fix.longitude:.7f},"
                f"{fix.altitude_m if fix.altitude_m is not None else ''},"
                f"{fix.hops_away if fix.hops_away is not None else ''},"
                f"{fix.snr_db if fix.snr_db is not None else ''},"
                f"{fix.address}"
            )
        return

    # Plain aligned lines, not a Rich table: at 80 columns a table folds cells across
    # lines, shredding addresses; this listing doubles as an eyeball-able export.
    typer.echo(f"{display} — {len(fixes)} fix(es) {range_text}")
    for fix in fixes:
        stamp = datetime.fromtimestamp(fix.effective_fix_s).strftime("%m-%d %H:%M:%S")
        if fix.effective_fix_s != fix.fix_time_s:
            stamp = f"~{stamp}"  # no sane device fix time — dated by observation
        heard_parts = []
        if fix.hops_away == 0:
            heard_parts.append("direct")
        elif fix.hops_away is not None:
            heard_parts.append(f"{fix.hops_away} hops")
        if fix.snr_db is not None:
            heard_parts.append(f"SNR {fix.snr_db:g}")
        altitude = f"{fix.altitude_m}m" if fix.altitude_m is not None else ""
        typer.echo(
            f"{stamp:>15}  {fix.address:<34}"
            f"{fix.latitude:>11.7f} {fix.longitude:>13.7f} {altitude:>6}  " + ", ".join(heard_parts)
        )


def _mesh_settings_or_exit(config_path: Path | None) -> tuple[Any, Path]:
    """(config, path) with a [meshtastic] section, or exit 2 with the standard message."""

    err = Console(stderr=True)
    path = config_path or default_config_path()
    try:
        config = load_config(path)
    except ConfigError as exc:
        err.print(f"[red]config error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc
    warning = perms_warning(path)
    if warning:
        err.print(f"[yellow]{escape(warning)}[/yellow]")
    if config.meshtastic is None:
        err.print(
            f"[red]meshtastic not configured:[/red] add a \\[meshtastic] section to {path} "
            "(an empty section is a valid all-defaults opt-in)."
        )
        raise typer.Exit(code=2)
    return config, path


playa_app = typer.Typer(
    add_completion=False,
    help=(
        "Event city data (feature 127). `locate` answers where any published camp "
        "or art installation is, from the locally downloaded datasets — asking never "
        "touches the internet. `fetch` downloads the season's art + camp datasets with "
        "the event's public API key ([playa] base_url): the feature's one network step, run once a "
        "season (X-API-Key on /api/art and /api/camp, the official OpenAPI scheme)."
    ),
)
app.add_typer(playa_app, name="playa")


@playa_app.command("locate")
def playa_locate_command(
    name: Annotated[str, typer.Argument(help="Camp or art installation name (partial names OK).")],
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to config.toml.")
    ] = None,
) -> None:
    """Where is a camp or art piece? Prints the same answer the playa_locate chat tool
    gives, from the locally downloaded datasets — never the internet."""

    setup_logging()
    err = Console(stderr=True)
    path = config_path or default_config_path()
    try:
        config = load_config(path)
    except ConfigError as exc:
        err.print(f"[red]config error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc
    warning = perms_warning(path)
    if warning:
        err.print(f"[yellow]{escape(warning)}[/yellow]")

    from obi.core.config import default_playa_state_path
    from obi.core.playa import PlayaLocator

    art_path = (
        config.meshtastic.art_path
        if config.meshtastic is not None
        else default_playa_state_path() / "art.json"
    )
    from obi.core.brcaddr import model_loader_for

    locator = PlayaLocator(
        art_path=art_path,
        camp_path=default_playa_state_path() / "camp.json",
        model_loader=model_loader_for(config.brc),
    )
    Console().print(escape(locator.locate(name)))


@playa_app.command("fetch")
def playa_fetch_command(
    key_file: Annotated[
        Path,
        typer.Option(
            "--key-file",
            help="File holding the event public API key (the key itself never "
            "appears in argv or logs).",
        ),
    ],
    year: Annotated[
        int | None, typer.Option("--year", help="Season to download (default: this year).")
    ] = None,
    base_url: Annotated[
        str | None,
        typer.Option("--base-url", help="API host override (default: [playa] base_url)."),
    ] = None,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to config.toml.")
    ] = None,
) -> None:
    """One-time operator download (feature 127): the season's art AND camp datasets from
    the event's public API ([playa] base_url) into the local files every location answer
    reads. This is the feature's ONLY internet path. A failed download leaves existing files
    untouched."""

    setup_logging()
    err = Console(stderr=True)
    path = config_path or default_config_path()
    try:
        config = load_config(path)
    except ConfigError as exc:
        err.print(f"[red]config error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc
    warning = perms_warning(path)
    if warning:
        err.print(f"[yellow]{escape(warning)}[/yellow]")

    from datetime import datetime

    import httpx

    from obi.core import playa
    from obi.core.config import default_playa_state_path

    try:
        key = key_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        err.print(f"[red]cannot read key file:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc
    if not key:
        err.print(f"[red]key file is empty:[/red] {escape(str(key_file))}")
        raise typer.Exit(code=2)

    api = base_url or (config.playa.base_url if config.playa is not None else None)
    if not api:
        err.print(
            "[red]playa API not configured:[/red] add \\[playa] base_url to the config, or pass "
            "--base-url (feature 137: there is no built-in API host)."
        )
        raise typer.Exit(code=2)

    season = year if year is not None else datetime.now().year
    art_path = (
        config.meshtastic.art_path
        if config.meshtastic is not None
        else default_playa_state_path() / "art.json"
    )
    targets = (("art", art_path), ("camp", default_playa_state_path() / "camp.json"))
    for kind, out_path in targets:
        try:
            count = playa.fetch_dataset(
                kind,
                year=season,
                key=key,
                out_path=out_path,
                base_url=api,
            )
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            hint = " — the API rejected the key" if status == 401 else ""
            err.print(
                f"[red]{kind} download failed:[/red] HTTP {status}{hint}; "
                "existing datasets left untouched."
            )
            raise typer.Exit(code=1) from exc
        except (httpx.HTTPError, ValueError, OSError) as exc:
            err.print(
                f"[red]{kind} download failed:[/red] {escape(str(exc))}; "
                "existing datasets left untouched."
            )
            raise typer.Exit(code=1) from exc
        Console().print(f"wrote {escape(str(out_path))}: {count} {kind} entries")
    Console().print('Location answers now cover camps and art — ask "where is <name>" anywhere.')


def _render_daemon_status(status: dict[str, Any]) -> str:
    state = str(status.get("state") or "unknown")
    detail = str(status.get("detail") or "")
    parts = [f"mesh daemon: {state} — {detail}"]
    age = status.get("snapshot_age_s")
    if isinstance(age, (int, float)):
        parts.append(f"node table {int(age)}s old")
    lease = status.get("lease_until_s")
    if isinstance(lease, (int, float)):
        parts.append(f"lease expires {_time.strftime('%H:%M', _time.localtime(lease))}")
    return "; ".join(parts)


@mesh_app.command("daemon")
def mesh_daemon_command(
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to config.toml.")
    ] = None,
) -> None:
    """Run the mesh daemon (feature 123) — the ONE process that owns the radio's serial
    port. Every other surface (dash tile, recorder, mesh_locate everywhere) reaches the
    radio through its socket; run under launchd as com.obi.mesh in production."""

    setup_logging()
    config, _path = _mesh_settings_or_exit(config_path)

    from obi.core.audit import AuditTrail, audit_scope, recorder_from_config
    from obi.core.meshdaemon import run_daemon

    recorder = recorder_from_config(config)
    session = f"meshd-{os.getpid()}"

    def scope_factory(source: str) -> AbstractContextManager[None]:
        return audit_scope(recorder, source, session)

    from obi.core.brcaddr import model_loader_for

    code = run_daemon(
        config.meshtastic,
        AuditTrail(config.audit_path),
        scope_factory=scope_factory,
        model_loader=model_loader_for(config.brc),
    )
    if code:
        raise typer.Exit(code=code)


@mesh_app.command("status")
def mesh_status_command(
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to config.toml.")
    ] = None,
) -> None:
    """The mesh daemon's status: attached / standby (with lease) / reconnecting, plus how
    old its node-table snapshot is. Read-only; exits 1 when the daemon is unreachable."""

    setup_logging()
    config, _path = _mesh_settings_or_exit(config_path)

    from obi.core.drivers.meshtastic import MeshFailure
    from obi.core.meshclient import MeshDaemonClient

    client = MeshDaemonClient(config.meshtastic, source="cli:mesh")
    status = client.status()
    if isinstance(status, MeshFailure):
        Console(stderr=True).print(f"[red]{escape(status.detail)}[/red]")
        raise typer.Exit(code=1)
    Console().print(escape(_render_daemon_status(status)))


@mesh_app.command("standby")
def mesh_standby_command(
    minutes: Annotated[
        float | None,
        typer.Option(
            "--minutes",
            help="Lease length; the daemon takes the port back by itself when it expires.",
        ),
    ] = None,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to config.toml.")
    ] = None,
) -> None:
    """Borrow the radio (feature 123): the daemon releases the serial port on a
    self-expiring lease so a direct CLI experiment can open it — the replacement for ever
    pausing monitoring crons. Resume early with `obi mesh resume`."""

    setup_logging()
    config, _path = _mesh_settings_or_exit(config_path)

    from obi.core.drivers.meshtastic import MeshFailure
    from obi.core.meshclient import MeshDaemonClient

    lease_s = int(minutes * 60) if minutes is not None else config.meshtastic.standby_lease_s
    client = MeshDaemonClient(config.meshtastic, source="cli:mesh")
    status = client.standby(lease_s)
    if isinstance(status, MeshFailure):
        Console(stderr=True).print(f"[red]{escape(status.detail)}[/red]")
        raise typer.Exit(code=1)
    Console().print(escape(_render_daemon_status(status)))


@mesh_app.command("resume")
def mesh_resume_command(
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to config.toml.")
    ] = None,
) -> None:
    """End a standby early: the daemon reattaches to the radio now."""

    setup_logging()
    config, _path = _mesh_settings_or_exit(config_path)

    from obi.core.drivers.meshtastic import MeshFailure
    from obi.core.meshclient import MeshDaemonClient

    client = MeshDaemonClient(config.meshtastic, source="cli:mesh")
    status = client.resume()
    if isinstance(status, MeshFailure):
        Console(stderr=True).print(f"[red]{escape(status.detail)}[/red]")
        raise typer.Exit(code=1)
    Console().print(escape(_render_daemon_status(status)))


wled_app = typer.Typer(
    add_completion=False,
    help=(
        "WLED lighting devices (features 103/107) — obi's only device-write surface, scoped to "
        "the \\[wled.devices] allowlist. `status` is read-only; `set` changes colors/palette/"
        "effect/speed/intensity/brightness/power (the overwritten look is captured first); "
        "`revert` puts the previous look back. Every write is audited with its pre-change "
        "snapshot; --dry-run previews."
    ),
)
app.add_typer(wled_app, name="wled")


def _wled_lighting_or_exit(config_path: Path | None) -> tuple[Config, Lighting]:
    """Shared load-and-gate for the wled commands: config + a ready Lighting engine."""

    setup_logging()
    err = Console(stderr=True)
    path = config_path or default_config_path()
    try:
        config = load_config(path)
    except ConfigError as exc:
        err.print(f"[red]config error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc
    if config.wled is None:
        err.print(
            f"[red]wled not configured:[/red] add a \\[wled.devices] section to {path} "
            '(e.g. portal = "https://portal-gw.example").'
        )
        raise typer.Exit(code=2)
    from obi.core.audit import AuditTrail
    from obi.core.lighting import Lighting

    return config, Lighting(config.wled, AuditTrail(config.audit_path))


@wled_app.command("status")
def wled_status_command(
    device: Annotated[
        str | None, typer.Argument(help="Configured device name (optional with one).")
    ] = None,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to config.toml.")
    ] = None,
) -> None:
    """Current lighting state: power, brightness, look, live-stream control, revert snapshot."""

    _config, lighting = _wled_lighting_or_exit(config_path)
    Console().print(escape(lighting.status(device)))


@wled_app.command("set")
def wled_set_command(
    device: Annotated[
        str | None, typer.Argument(help="Configured device name (optional with one).")
    ] = None,
    color: Annotated[
        str | None, typer.Option("--color", help="#RRGGBB hex or a common color name.")
    ] = None,
    color2: Annotated[
        str | None, typer.Option("--color2", help="Secondary/background color slot.")
    ] = None,
    color3: Annotated[str | None, typer.Option("--color3", help="Tertiary color slot.")] = None,
    effect: Annotated[
        str | None, typer.Option("--effect", help="Pattern from the device catalog.")
    ] = None,
    palette: Annotated[
        str | None,
        typer.Option("--palette", help="Color palette from the device catalog (e.g. Icefire)."),
    ] = None,
    brightness: Annotated[
        int | None, typer.Option("--brightness", min=0, max=100, help="Brightness percent.")
    ] = None,
    speed: Annotated[
        int | None, typer.Option("--speed", min=0, max=100, help="Effect speed percent.")
    ] = None,
    intensity: Annotated[
        int | None, typer.Option("--intensity", min=0, max=100, help="Effect intensity percent.")
    ] = None,
    custom1: Annotated[
        int | None,
        typer.Option("--custom1", min=0, max=100, help="Effect-specific slider 1 percent."),
    ] = None,
    custom2: Annotated[
        int | None,
        typer.Option("--custom2", min=0, max=100, help="Effect-specific slider 2 percent."),
    ] = None,
    custom3: Annotated[
        int | None,
        typer.Option("--custom3", min=0, max=100, help="Effect-specific slider 3 percent."),
    ] = None,
    on: Annotated[bool | None, typer.Option("--on/--off", help="Power on/off.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Preview; write nothing.")] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to config.toml.")
    ] = None,
) -> None:
    """Change the lighting (audited; the overwritten look is captured for `obi wled revert`)."""

    config, lighting = _wled_lighting_or_exit(config_path)
    with _audit_capture(config, "cli:wled"):
        out = lighting.set_look(
            device,
            color=color,
            color2=color2,
            color3=color3,
            effect=effect,
            palette=palette,
            brightness_pct=brightness,
            speed_pct=speed,
            intensity_pct=intensity,
            custom1_pct=custom1,
            custom2_pct=custom2,
            custom3_pct=custom3,
            power=on,
            dry_run=dry_run,
            reason="cli: obi wled set",
        )
    Console().print(escape(out))


@wled_app.command("revert")
def wled_revert_command(
    device: Annotated[
        str | None, typer.Argument(help="Configured device name (optional with one).")
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Preview; write nothing.")] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to config.toml.")
    ] = None,
) -> None:
    """Restore the look from before obi's most recent change."""

    config, lighting = _wled_lighting_or_exit(config_path)
    with _audit_capture(config, "cli:wled"):
        out = lighting.revert(device, dry_run=dry_run, reason="cli: obi wled revert")
    Console().print(escape(out))


@wled_app.command("status-band")
def wled_status_band_command(
    look: Annotated[str, typer.Argument(help="green | yellow | red | off")],
    device: Annotated[
        str | None, typer.Argument(help="Configured device name (optional with one).")
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Preview; write nothing.")] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to config.toml.")
    ] = None,
) -> None:
    """Set the tower's status bands (feature 128) — maintenance/self-test.

    The dash reconciler owns the bands in steady state and will heal any color that
    disagrees with the board within a minute; this command exists for field tests and
    manual overrides while the reconciler is disabled. One of the 2.3.0 clause's two
    sanctioned callers (the other is the dash cycle) — deliberately NOT a chat/radio/MCP
    capability.
    """

    config, lighting = _wled_lighting_or_exit(config_path)
    with _audit_capture(config, "cli:wled"):
        out = lighting.set_status_band(
            device, look=look, dry_run=dry_run, reason="cli: obi wled status-band"
        )
    Console().print(escape(out))


@dash_app.command("run")
def dash_run(
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit the evaluated board as JSON instead of a summary.")
    ] = False,
    scope: Annotated[
        Path | None,
        typer.Option("--scope", help="Path to the config/scope file (default: XDG config)."),
    ] = None,
) -> None:
    """Run one dashboard cycle (the ``* * * * *`` cron entrypoint) and write ``board.json``.

    Read-only toward devices: ICMP echoes from this host, a ``/tool/netwatch`` print per router,
    and the read-only reference ping from the border router. Degraded inputs render stale tiles,
    never a missing board. A previous still-running cycle makes this one a clean skip (exit 0).
    """

    setup_logging()
    logger = logging.getLogger("obi")
    err = Console(stderr=True)
    config_path = scope or default_config_path()

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        err.print(f"[red]config error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc

    warning = perms_warning(config_path)
    if warning:
        logger.warning("insecure config permissions", extra={"detail": warning})
        err.print(f"[yellow]warning:[/yellow] {warning}")

    if config.dash is None:
        err.print(
            "[yellow]dashboard not configured:[/yellow] add a [dash] section "
            "(see README.md § Status dashboard)."
        )
        raise typer.Exit(code=2)
    if not config.dash.enabled:
        # Disabled-but-present is a deliberate operator state (analysis finding U4): the cron
        # line can stay installed while the section is parked — one line, exit 0, no board write.
        Console().print("dash: disabled ([dash] enabled = false) — nothing to do")
        raise typer.Exit(code=0)

    from obi.core import dash as dash_mod
    from obi.core.models import _utcnow

    # US3: the pager delivers over the watch Slack DM path (deliver_status — the terse,
    # dedup-free line, by design). Config load already guaranteed a usable bot token.
    # Feature 106: run_pager emits structured PagerEvents; this adapter is the Slack channel
    # (and the retry driver). Secondary channels (102) compose here, best-effort, deduping
    # on event.event_id — see specs/106-dash-link-incidents/contracts/pager-events.md.
    pager_deliver: dash_mod.DeliverFn | None = None
    if config.dash.pager.enabled:
        from obi.slack.notifier import SlackNotifier

        notifier = SlackNotifier.from_config(config)

        def slack_deliver(event: dash_mod.PagerEvent) -> tuple[bool, str | None]:
            result = notifier.deliver_status(dash_mod.format_slack_page(event), label="dash page")
            return result.failed == 0 and result.error is None, result.error

        pager_deliver = slack_deliver
        # 102: paging as an additional transport, opt-in TWICE ([paging] present AND
        # [paging.alerts] enabled). Slack stays the primary and drives the retry loop —
        # the composite returns the Slack result unchanged; pages are additive,
        # best-effort, capped, and deduped per event id (a Slack outage re-emits the
        # same event every cycle; the page must not repeat).
        if config.paging is not None and config.paging.alerts.enabled:
            from datetime import UTC

            from obi.paging.alerts import wrap_deliver

            display_tz = config.logs.tzinfo() if config.logs is not None else UTC

            def notify_paging_failure(text: str) -> None:
                notifier.deliver_status(text, label="dash page")

            pager_deliver = wrap_deliver(
                slack_deliver,
                config.paging,
                state_dir=config.dash.state_dir,
                notify=notify_paging_failure,
                tz=display_tz,
            )

    now_us = int(_utcnow().timestamp() * 1_000_000)
    started = _time.monotonic()
    with dash_mod.cycle_lock(config.dash.state_dir) as acquired:
        if not acquired:
            logger.info("dash cycle skipped — previous cycle still running")
            Console().print("dash: previous cycle still running — skipped")
            raise typer.Exit(code=0)
        with _audit_capture(config, "cli:dash-run"):
            board = dash_mod.run_cycle(config, now_us=now_us, pager_deliver=pager_deliver)
    duration_s = _time.monotonic() - started

    # SC-006 observability (analysis finding G1): every cycle logs how long it took, so an
    # approach toward the 60 s period is visible long before it becomes lock-skips.
    logger.info(
        "dash cycle complete",
        extra={
            "state": board.state,
            "tiles": {tile.signal: tile.state for tile in board.tiles},
            "notes": len(board.notes),
            "duration_ms": round(duration_s * 1000),
        },
    )
    if json_output:
        import json as json_lib

        sys.stdout.write(json_lib.dumps(board.to_json()) + "\n")
    else:
        tiles = ", ".join(f"{tile.signal} {tile.state}" for tile in board.tiles)
        Console().print(f"dash: {board.state} — {tiles} ({duration_s:.1f}s)")


@dash_app.command("show")
def dash_show(
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit board.json verbatim instead of a table.")
    ] = False,
    scope: Annotated[
        Path | None,
        typer.Option("--scope", help="Path to the config/scope file (default: XDG config)."),
    ] = None,
) -> None:
    """Show the last evaluated status board (read-only over ``board.json``)."""

    setup_logging()
    err = Console(stderr=True)
    config_path = scope or default_config_path()

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        err.print(f"[red]config error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc

    from obi.core import dash as dash_mod
    from obi.core.config import default_dash_state_dir
    from obi.core.models import _utcnow

    state_dir = config.dash.state_dir if config.dash is not None else default_dash_state_dir()
    board = dash_mod.load_board(state_dir)
    if board is None:
        err.print("[yellow]no dashboard snapshot yet[/yellow] — run: obi dash run (cron)")
        raise typer.Exit(code=0)

    if json_output:
        import json as json_lib

        sys.stdout.write(json_lib.dumps(board.to_json()) + "\n")
    else:
        now_us = int(_utcnow().timestamp() * 1_000_000)
        _render_dash_board(board, now_us, Console())


page_app = typer.Typer(
    add_completion=False,
    help=(
        "PlayaPage paging (feature 102): send pages to real ESD pagers. `send` is the human "
        "path (names resolve against the local roster; ambiguity always refuses; --dry-run "
        "prints the payload). `recipients` searches the roster offline; `whoami` verifies "
        "credentials without paging anyone; `refresh` rebuilds the roster from the live lists. "
        "The model can never send a page — this CLI and the deterministic [paging.alerts] dash "
        "transport are the only two paths."
    ),
)
app.add_typer(page_app, name="page")


def _paging_config(scope: Path | None) -> Config:
    """Strictly load config for the page verbs; exit 2 unless ``[paging]`` is present."""

    err = Console(stderr=True)
    try:
        config = load_config(scope or default_config_path())
    except ConfigError as exc:
        err.print(f"[red]config error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc
    if config.paging is None:
        err.print(
            r"[yellow]paging not configured:[/yellow] add a \[paging] section "
            "(see docs/setup/paging.md)."
        )
        raise typer.Exit(code=2)
    return config


@page_app.command("send")
def page_send(
    to: Annotated[
        str | None,
        typer.Option("--to", help="Comma-separated IDs or names (person/group auto-typed)."),
    ] = None,
    people: Annotated[
        str | None, typer.Option("--people", help="Comma-separated person IDs or names.")
    ] = None,
    groups: Annotated[
        str | None, typer.Option("--groups", help="Comma-separated group IDs or names.")
    ] = None,
    message: Annotated[
        str | None,
        typer.Option("--message", "-m", help="Message text; '-' (or piped stdin) reads stdin."),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the exact uAPI payload; transmit nothing.")
    ] = False,
    scope: Annotated[
        Path | None,
        typer.Option("--scope", help="Path to the config file (default: XDG config)."),
    ] = None,
) -> None:
    """Send one page. Refusals (ambiguous name, over-length message) transmit NOTHING.

    The resolved recipients are printed before any send — informational, never a prompt
    (operator decision, clarification #4). Every attempt (including dry-runs) is audited.
    """

    setup_logging()
    console = Console()
    err = Console(stderr=True)
    config = _paging_config(scope)
    paging = config.paging
    assert paging is not None  # _paging_config guaranteed it

    from obi.paging.client import PagingClient, record_page
    from obi.paging.roster import ResolutionError, load_roster, resolve, split_tokens

    roster = load_roster(paging.roster_path)
    resolved_people: list[str] = []
    resolved_groups: list[str] = []
    try:
        if to:
            resolved = resolve(split_tokens(to), roster)
            resolved_people += resolved.people
            resolved_groups += resolved.groups
        if people:
            resolved_people += resolve(split_tokens(people), roster, force_type="person").people
        if groups:
            resolved_groups += resolve(split_tokens(groups), roster, force_type="group").groups
    except ResolutionError as exc:
        err.print(f"[red]refused:[/red] {escape(str(exc))}")
        for candidate in exc.candidates:
            err.print(f"  {escape(candidate.display())}")
        raise typer.Exit(code=2) from exc
    resolved_people = list(dict.fromkeys(resolved_people))
    resolved_groups = list(dict.fromkeys(resolved_groups))
    if not resolved_people and not resolved_groups:
        err.print("[red]no recipients[/red] — use --to, --people, and/or --groups")
        raise typer.Exit(code=2)

    if message == "-" or (message is None and not sys.stdin.isatty()):
        message = sys.stdin.read().strip()
    if not message:
        err.print("[red]empty message[/red] — pass --message TEXT (or '-' to read stdin)")
        raise typer.Exit(code=2)
    if len(message) > paging.message_max_chars:
        err.print(
            f"[red]message too long[/red] — {len(message)} chars over the "
            f"{paging.message_max_chars}-char page limit ([paging] message_max_chars)"
        )
        raise typer.Exit(code=2)

    # Informational display of exactly who resolves (never a prompt): roster names when known.
    by_id = {(e.type, e.id): e for e in roster or []}
    for kind, ids in (("person", resolved_people), ("group", resolved_groups)):
        for rid in ids:
            entry = by_id.get((kind, rid))
            name = f"  {escape(entry.name)}" if entry else ""
            console.print(f"{kind:<6} {rid:>4}{name}")
    console.print(f"message: {escape(message)}")

    payload = {
        "username": paging.username,
        "password": "***",
        "sendpage": {
            "recipients": (
                ({"people": resolved_people} if resolved_people else {})
                | ({"groups": resolved_groups} if resolved_groups else {})
            ),
            "message": message,
        },
    }
    with _audit_capture(config, "cli:page-send"):
        if dry_run:
            import json as json_lib

            sys.stdout.write(json_lib.dumps(payload, indent=2) + "\n")
            record_page(
                path="cli",
                people=resolved_people,
                groups=resolved_groups,
                body=message,
                outcome="dry_run",
            )
            console.print("[dim]dry-run: not sent[/dim]")
            return
        result = PagingClient(paging).send_page(
            people=resolved_people, groups=resolved_groups, message=message
        )
        record_page(
            path="cli",
            people=resolved_people,
            groups=resolved_groups,
            body=message,
            outcome="ok" if result.ok else "error",
            error=result.error,
        )
    if result.ok:
        console.print("[green]page sent[/green]")
        return
    err.print(f"[red]page failed[/red] — {escape(result.error or 'unknown error')}")
    raise typer.Exit(code=1)


@page_app.command("recipients")
def page_recipients(
    query: Annotated[str, typer.Argument(help="Name/ID substring filter (optional).")] = "",
    people_only: Annotated[bool, typer.Option("--people-only")] = False,
    groups_only: Annotated[bool, typer.Option("--groups-only")] = False,
    scope: Annotated[
        Path | None,
        typer.Option("--scope", help="Path to the config file (default: XDG config)."),
    ] = None,
) -> None:
    """Search the local roster snapshot — fully offline, no credentials, no network."""

    setup_logging()
    console = Console()
    err = Console(stderr=True)
    from obi.core.config import default_paging_roster_path
    from obi.paging.roster import load_roster, search

    config = _config_or_default(scope or default_config_path())
    roster_path = (
        config.paging.roster_path if config.paging is not None else default_paging_roster_path()
    )
    roster = load_roster(roster_path)
    if roster is None:
        err.print(
            f"[yellow]no roster snapshot at {escape(str(roster_path))}[/yellow] — "
            "run: obi page refresh"
        )
        raise typer.Exit(code=1)
    rows = search(roster, query, people_only=people_only, groups_only=groups_only)
    for entry in rows:
        console.print(escape(entry.display()))
    console.print(f"[dim]{len(rows)} match(es)[/dim]")


@page_app.command("whoami")
def page_whoami(
    scope: Annotated[
        Path | None,
        typer.Option("--scope", help="Path to the config file (default: XDG config)."),
    ] = None,
) -> None:
    """Verify the configured credentials via a list call — never sends a page."""

    setup_logging()
    console = Console()
    err = Console(stderr=True)
    config = _paging_config(scope)
    paging = config.paging
    assert paging is not None

    from obi.paging.client import PagingClient

    with _audit_capture(config, "cli:page-whoami"):
        result = PagingClient(paging).check_credentials()
    if result.ok:
        console.print(
            f"[green]✓[/green] credentials OK for {escape(paging.username)} "
            f"against {escape(paging.url)}"
        )
        return
    err.print(f"[red]credential check failed[/red] — {escape(result.error or 'unknown error')}")
    raise typer.Exit(code=1)


@page_app.command("refresh")
def page_refresh(
    scope: Annotated[
        Path | None,
        typer.Option("--scope", help="Path to the config file (default: XDG config)."),
    ] = None,
) -> None:
    """Rebuild the roster snapshot from the live people/group lists (previous kept as .bak)."""

    setup_logging()
    console = Console()
    err = Console(stderr=True)
    config = _paging_config(scope)
    paging = config.paging
    assert paging is not None

    from obi.paging.client import PagingClient
    from obi.paging.roster import parse_live, save_roster

    with _audit_capture(config, "cli:page-refresh"):
        client = PagingClient(paging)
        people_result = client.list_people()
        groups_result = client.list_groups()
    for label, result in (("listpeople", people_result), ("listgroups", groups_result)):
        if not result.ok:
            err.print(f"[red]{label} failed[/red] — {escape(result.error or 'unknown error')}")
            err.print("[dim]existing roster snapshot left untouched[/dim]")
            raise typer.Exit(code=1)
    try:
        entries = parse_live(people_result.body, groups_result.body)
    except ValueError as exc:
        err.print(f"[red]refresh failed[/red] — {escape(str(exc))}")
        err.print("[dim]existing roster snapshot left untouched[/dim]")
        raise typer.Exit(code=1) from exc
    save_roster(paging.roster_path, entries)
    people_count = sum(1 for e in entries if e.type == "person")
    groups_count = sum(1 for e in entries if e.type == "group")
    console.print(
        f"[green]roster refreshed[/green] — {people_count} people, {groups_count} groups → "
        f"{escape(str(paging.roster_path))}"
    )


voice_app = typer.Typer(
    add_completion=False,
    help=(
        "Talk to obi out loud (read-only). `obi voice talk` is local push-to-talk at this Mac. "
        "The browser talk surface lives on the unified web origin: `obi web serve` (033; voice "
        "mounts by default since 048)."
    ),
)
app.add_typer(voice_app, name="voice")


@voice_app.command("talk")
def voice_talk(
    scope: Annotated[
        Path | None,
        typer.Option("--scope", help="Path to the config/scope file (default: XDG config)."),
    ] = None,
    model: Annotated[
        str | None, typer.Option("--model", help="Override the chat model for this run.")
    ] = None,
    max_tokens: Annotated[
        int | None, typer.Option("--max-tokens", help="Override the response token cap.")
    ] = None,
    stt_model: Annotated[
        str | None,
        typer.Option("--stt-model", help="Override the local speech-recognition model."),
    ] = None,
    tts_voice: Annotated[
        str | None,
        typer.Option("--tts-voice", help="Override the macOS voice (see `say -v ?`)."),
    ] = None,
) -> None:
    """Talk to obi out loud (read-only): push-to-talk in, spoken answers out.

    Speech is captured, transcribed, and synthesized entirely on this Mac; only the
    transcribed text goes to the reasoning model — exactly what `obi chat` sends. Requires
    the voice extra: `uv sync --extra voice`.
    """

    setup_logging()
    logger = logging.getLogger("obi")
    err = Console(stderr=True)
    console = Console()
    config_path = scope or default_config_path()

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        err.print(f"[red]config error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc

    warning = perms_warning(config_path)
    if warning:
        logger.warning("insecure config permissions", extra={"detail": warning})
        err.print(f"[yellow]warning:[/yellow] {warning}")

    if model is not None:
        config.chat.model = model
    if max_tokens is not None:
        config.chat.max_tokens = max_tokens
    if stt_model is not None:
        config.voice.stt_model = stt_model
    if tts_voice is not None:
        config.voice.tts_voice = tts_voice

    from obi.chat.session import build_runtime
    from obi.core import inventory as inv
    from obi.core.llm import ChatUnavailable
    from obi.voice.audio import SounddeviceRecorder, ensure_input_device
    from obi.voice.errors import VoiceUnavailable
    from obi.voice.session import VoiceSession, ensure_model_available
    from obi.voice.stt import MlxWhisperTranscriber
    from obi.voice.tts import SaySpeaker

    try:
        runtime = build_runtime(config)
    except ChatUnavailable as exc:
        err.print(f"[red]voice unavailable:[/red] {exc}")
        raise typer.Exit(code=3) from exc

    if runtime.context_warning:
        logger.warning("chat context not loaded", extra={"detail": runtime.context_warning})
        err.print(f"[yellow]warning:[/yellow] {runtime.context_warning}")

    if inv.load(config.inventory_path) is None:
        err.print(
            "[red]no inventory found:[/red] run `obi routeros discover` first so obi has "
            f"something to talk about (expected at {config.inventory_path})."
        )
        raise typer.Exit(code=4)

    def _input(prompt: str = "") -> str:
        return console.input(prompt)

    try:
        ensure_input_device()
        if not ensure_model_available(config.voice, console, _input):
            raise typer.Exit(code=0)
        session = VoiceSession(
            runtime=runtime,
            settings=config.voice,
            recorder=SounddeviceRecorder(wait_for_stop=lambda: _input("")),
            transcriber=MlxWhisperTranscriber(
                config.voice.stt_model, rms_threshold=config.voice.rms_threshold
            ),
            speaker=SaySpeaker(voice=config.voice.tts_voice, rate=config.voice.tts_rate),
            console=console,
            err=err,
            input_fn=_input,
        )
    except VoiceUnavailable as exc:
        err.print(f"[red]voice unavailable:[/red] {exc}")
        raise typer.Exit(code=3) from exc

    with _audit_capture(config, "voice"):
        code = session.run()
    raise typer.Exit(code=code)


def _build_voice_web_app(
    config: Config, console: Console, err: Console, *, label: str
) -> VoiceWebApp:
    """Build the 024 voice turn engine (chat runtime + eager local STT + TTS).

    The one engine behind every browser voice surface. 024/032 kept two hosting commands
    from drifting by sharing this builder; 033 finished the job — the only consumer left is
    ``obi web serve`` (voice mounts by default since 048), hosting BOTH viewer versions'
    push-to-talk on one origin. Raises ``typer.Exit`` on an unbuildable engine (no key/inventory/
    STT deps); ``web serve`` catches it and degrades to a static+tools server (048).
    """

    from dataclasses import replace as _replace

    from obi.chat.session import build_runtime
    from obi.core import inventory as inv
    from obi.core.llm import ChatUnavailable
    from obi.core.prompt import VOICE_SYSTEM_ADDENDUM
    from obi.voice.errors import VoiceUnavailable
    from obi.voice.session import ensure_model_available
    from obi.voice.stt import MlxWhisperTranscriber
    from obi.voice.tts import render
    from obi.voice.web.turns import ConversationStore, VoiceWebApp

    logger = logging.getLogger("obi")
    try:
        runtime = build_runtime(config)
    except ChatUnavailable as exc:
        err.print(f"[red]{label} unavailable:[/red] {exc}")
        raise typer.Exit(code=3) from exc

    if runtime.context_warning:
        logger.warning("chat context not loaded", extra={"detail": runtime.context_warning})
        err.print(f"[yellow]warning:[/yellow] {runtime.context_warning}")

    if inv.load(config.inventory_path) is None:
        err.print(
            "[red]no inventory found:[/red] run `obi routeros discover` first so obi has "
            f"something to talk about (expected at {config.inventory_path})."
        )
        raise typer.Exit(code=4)

    def _input(prompt: str = "") -> str:
        return console.input(prompt)

    try:
        # FR-014 (024): the one-time model disclosure + eager fetch happens NOW, never mid-turn.
        if not ensure_model_available(config.voice, console, _input):
            raise typer.Exit(code=0)
        transcriber = MlxWhisperTranscriber(
            config.voice.stt_model, rms_threshold=config.voice.rms_threshold
        )
    except VoiceUnavailable as exc:
        err.print(f"[red]{label} unavailable:[/red] {exc}")
        raise typer.Exit(code=3) from exc

    # The spoken-style addendum is applied exactly once, at service level (023 research R5);
    # per-conversation actors are applied per turn in the engine.
    voice_settings = config.voice
    from obi.core.audit import recorder_from_config

    return VoiceWebApp(
        runtime=_replace(runtime, system=runtime.system + VOICE_SYSTEM_ADDENDUM),
        settings=voice_settings,
        transcriber=transcriber,
        render=lambda text: render(
            text, voice=voice_settings.tts_voice, rate=voice_settings.tts_rate
        ),
        store=ConversationStore(idle_minutes=voice_settings.web_idle_minutes),
        # feature 042: turns audit through a per-conversation scope over this recorder.
        recorder=recorder_from_config(config),
    )


_VOICE_CLOUD_DISCLOSURE = (
    "[dim]obi voice uses a cloud reasoning model. Transcribed questions and the read-only "
    "results obi looks up are sent to it; your credentials are not. Speech is captured in "
    "the browser, crosses only your LAN, and is transcribed and synthesized on this Mac — "
    "never retained, never sent to a cloud speech service.[/dim]"
)


@voice_app.command(
    "web",
    hidden=True,
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def voice_web_superseded(ctx: typer.Context) -> None:
    """Superseded (feature 033): the talk page merged into the unified web origin."""

    err = Console(stderr=True)
    err.print(
        "[red]obi voice web was merged in feature 033:[/red] use "
        "[bold]obi web serve[/bold] — one origin (default 8443) now serves the talk "
        "experience on every browser (the 2D page has a hold-to-talk button; the headset "
        "keeps in-viewer voice). Port 8444 is retired; remove \\[voice] web_port/web_bind "
        "from your config. Migration notes: README.md § Web terminal."
    )
    raise typer.Exit(code=2)


# --------------------------------------------------------------------------------------------------
# Daemon management (feature 081) — shared thin adapters over core/service.py (Principle III).
# The Slack listener and web server get identical lifecycle verbs; all logic lives in core/service.
# --------------------------------------------------------------------------------------------------


def _svc_audit(
    config: Config,
    action: str,
    target: str,
    *,
    ok: bool,
    params: dict[str, object] | None = None,
) -> None:
    from obi.core.audit import AuditTrail
    from obi.core.models import AuditOutcome, AuditRecord, _utcnow

    AuditTrail(config.audit_path).record(
        AuditRecord(
            timestamp=_utcnow(),
            action=action,
            target=target,
            reason="operator-initiated daemon management",
            outcome=AuditOutcome.OK if ok else AuditOutcome.ERROR,
            params=params or {},
        )
    )


def _fmt_age(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    s = int(seconds)
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m {s % 60}s ago"
    return f"{s // 3600}h {(s % 3600) // 60}m ago"


def _config_or_default(config_path: Path) -> Config:
    """Load config, or fall back to defaults so read-only verbs work with a bare/empty config."""

    from obi.core.config import Config

    try:
        return load_config(config_path)
    except ConfigError:
        return Config()


def _daemon_headline(spec: ServiceSpec, state: LaunchdState) -> str:
    name = spec.name
    if not state.installed and not state.loaded:
        return f"[bold]obi {name}[/bold] — [dim]not installed[/dim]"
    if state.running:
        return f"[bold]obi {name}[/bold] — [green]running[/green] (pid {state.pid})"
    if state.loaded:
        last = state.last_exit
        note = f", last exit {last}" if last not in (None, 0) else ""
        return f"[bold]obi {name}[/bold] — [yellow]loaded, not running[/yellow]{note}"
    return f"[bold]obi {name}[/bold] — [yellow]installed, not loaded[/yellow]"


def _liveness_line(spec: ServiceSpec, state: LaunchdState, config: Config) -> str:
    import time as _time

    from obi.core import service as svc

    if spec.kind is svc.ServiceKind.WEB:
        if not state.running:
            return "liveness: [dim]not running[/dim]"
        live = svc.probe_web(config.web.bind, config.web.port)
        if live.healthy:
            return "liveness: [green]healthy[/green] (GET /healthz → 200)"
        if live.reachable:
            return f"liveness: [yellow]HTTP {live.status}[/yellow] (GET /healthz)"
        return "liveness: [red]not answering[/red]"
    record = svc.read_heartbeat(spec.heartbeat_path)
    verdict = svc.interpret_heartbeat(record, now=_time.time())
    if verdict.state == "connected":
        return f"liveness: [green]connected[/green] ({_fmt_age(verdict.age_s)})"
    if verdict.state == "wedged":
        return f"liveness: [red]possibly wedged[/red] (no heartbeat in {_fmt_age(verdict.age_s)})"
    if verdict.state == "disconnected":
        return "liveness: [yellow]not connected[/yellow]"
    return "liveness: [dim]unknown (no heartbeat file)[/dim]"


def _print_collectors(console: Console, health: list[CollectorHealth]) -> None:
    table = Table(title="cron collectors", title_style="dim", header_style="dim", show_edge=False)
    table.add_column("collector")
    table.add_column("last run")
    table.add_column("state")
    for c in health:
        state = "[red]STALE[/red]" if c.stale else "[green]fresh[/green]"
        table.add_row(c.name, _fmt_age(c.age_s), state)
    console.print(table)


def _svc_require_macos(err: Console) -> None:
    from obi.core import service as svc

    try:
        svc.require_macos()
    except svc.ServiceError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc


def _svc_start(
    name: str, scope: Path | None, login: bool | None, exec_override: Path | None
) -> None:
    from obi.core import service as svc

    setup_logging()
    console = Console()
    err = Console(stderr=True)
    _svc_require_macos(err)
    config_path = scope or default_config_path()
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        err.print(f"[red]config error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc

    spec = svc.spec_for(name)
    if exec_override is not None:
        obi_exec = exec_override
    else:
        obi_exec = svc.resolve_obi_exec()
        try:
            svc.assert_primary_checkout(obi_exec)
        except svc.ServiceError as exc:
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc

    run_at_login = config.service.run_at_login if login is None else login

    if spec.kind is svc.ServiceKind.SLACK:
        strays = svc.detect_stray_listener()
        if strays:
            pids = ", ".join(str(p) for p in strays)
            err.print(
                f"[yellow]warning:[/yellow] a non-launchd `obi slack` is already running "
                f"(pid {pids}) — stop it (`kill {strays[0]}`) so you don't run two listeners."
            )

    try:
        state = svc.install(
            spec,
            obi_exec=obi_exec,
            run_at_login=run_at_login,
            environment=config.service.environment,
            workdir=svc.primary_workdir(obi_exec),
        )
    except svc.ServiceError as exc:
        _svc_audit(config, "service_start", spec.label, ok=False, params={"error": str(exc)})
        err.print(f"[red]start failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    _svc_audit(config, "service_start", spec.label, ok=True, params={"run_at_login": run_at_login})
    console.print(_daemon_headline(spec, state))
    console.print(f"  plist:  {spec.plist_path}")
    console.print(f"  logs:   {spec.log_path}  (obi {name} logs)")
    console.print(
        f"  restart at login: {'[green]yes[/green]' if run_at_login else '[yellow]no[/yellow]'}"
    )


def _svc_stop(name: str, scope: Path | None) -> None:
    from obi.core import service as svc

    setup_logging()
    console = Console()
    err = Console(stderr=True)
    _svc_require_macos(err)
    config = _config_or_default(scope or default_config_path())
    spec = svc.spec_for(name)
    state = svc.service_state(spec)
    if not state.loaded and not state.installed:
        console.print(f"obi {name}: [dim]not installed[/dim] (nothing to stop)")
        return
    try:
        svc.uninstall(spec)
    except svc.ServiceError as exc:
        _svc_audit(config, "service_stop", spec.label, ok=False, params={"error": str(exc)})
        err.print(f"[red]stop failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    _svc_audit(config, "service_stop", spec.label, ok=True)
    console.print(
        f"obi {name}: [green]stopped[/green] (booted out — will not restart until "
        f"`obi {name} start`)"
    )


def _svc_restart(name: str, scope: Path | None) -> None:
    from obi.core import service as svc

    setup_logging()
    console = Console()
    err = Console(stderr=True)
    _svc_require_macos(err)
    config = _config_or_default(scope or default_config_path())
    spec = svc.spec_for(name)
    try:
        state = svc.restart(spec)
    except svc.ServiceError as exc:
        _svc_audit(config, "service_restart", spec.label, ok=False, params={"error": str(exc)})
        err.print(f"[red]restart failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    _svc_audit(config, "service_restart", spec.label, ok=True)
    console.print(_daemon_headline(spec, state))


def _svc_status(name: str, scope: Path | None) -> None:
    from obi.core import service as svc

    setup_logging()
    console = Console()
    config = _config_or_default(scope or default_config_path())
    spec = svc.spec_for(name)
    state = svc.service_state(spec)
    console.print(_daemon_headline(spec, state))
    console.print("  " + _liveness_line(spec, state, config))
    console.print(f"  plist:  {spec.plist_path}")
    console.print(f"  logs:   {spec.log_path}")
    for other in svc.all_specs():
        if other.name != name:
            console.print()
            console.print(_daemon_headline(other, svc.service_state(other)))
    console.print()
    _print_collectors(console, svc.collector_health())


def _svc_logs(name: str, lines: int, follow: bool) -> None:
    import subprocess

    from obi.core import service as svc

    console = Console()
    spec = svc.spec_for(name)
    if not spec.log_path.exists():
        console.print(f"obi {name}: [dim]no log yet[/dim] at {spec.log_path}")
        return
    args = ["tail", f"-n{lines}"]
    if follow:
        args.append("-f")
    args.append(str(spec.log_path))
    subprocess.run(args)  # noqa: S603 — fixed argv, operator-invoked tail of a known log path


_SCOPE_OPT = typer.Option("--scope", help="Path to the config file (default: XDG config).")
_LOGIN_OPT = typer.Option(
    "--login/--no-login",
    help="Start at login (RunAtLoad). Default: [service] run_at_login.",
)
_EXEC_OPT = typer.Option(
    "--exec", help="Override the supervised obi executable (bypasses the primary-checkout guard)."
)
_LINES_OPT = typer.Option("--lines", "-n", help="Number of log lines to show.")
_FOLLOW_OPT = typer.Option("--follow", "-f", help="Follow the log (tail -f).")


slack_app = typer.Typer(
    no_args_is_help=True,
    help="The Slack interface: run the listener in the foreground, or supervise it via launchd.",
)
app.add_typer(slack_app, name="slack")


@slack_app.command("run")
def slack_run(
    scope: Annotated[Path | None, _SCOPE_OPT] = None,
) -> None:
    """Run the Slack listener in the foreground (long-lived; answers authorized operators in DMs).

    Read-only, same brain as ``obi chat``. Requires a [slack] config section (bot/app tokens +
    an allowlist of Slack user IDs). Connects outbound via Socket Mode; Ctrl-C stops it. This is
    what the launchd agent runs; use ``obi slack start`` to supervise it in the background.
    """

    setup_logging()
    logger = logging.getLogger("obi")
    err = Console(stderr=True)
    config_path = scope or default_config_path()

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        err.print(f"[red]config error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc

    warning = perms_warning(config_path)
    if warning:
        logger.warning("insecure config permissions", extra={"detail": warning})
        err.print(f"[yellow]warning:[/yellow] {warning}")

    if config.slack is None:
        err.print(
            "[red]config error:[/red] no \\[slack] section — add bot_token, app_token, and a "
            "non-empty allowlist (see README.md § Ask obi from Slack)."
        )
        raise typer.Exit(code=2)

    import os

    from obi.chat.session import build_runtime
    from obi.core.llm import ChatUnavailable
    from obi.core.service import Heartbeat, service_state, slack_spec

    spec = slack_spec()
    # 081: warn if a supervised instance is already loaded (two listeners double every answer) —
    # but NOT when we are that launchd instance ourselves (launchd sets XPC_SERVICE_NAME = label).
    if os.environ.get("XPC_SERVICE_NAME") != spec.label:
        try:
            if service_state(spec).loaded:
                err.print(
                    "[yellow]note:[/yellow] a launchd-managed `obi slack` is already loaded — "
                    "running a second foreground listener will double every reply. "
                    "`obi slack stop` first."
                )
        except Exception:  # noqa: BLE001 — the advisory check must never block the run
            pass

    try:
        runtime = build_runtime(config)
    except ChatUnavailable as exc:
        err.print(f"[red]chat unavailable:[/red] {exc}")
        raise typer.Exit(code=3) from exc

    if runtime.context_warning:
        logger.warning("chat context not loaded", extra={"detail": runtime.context_warning})
        err.print(f"[yellow]warning:[/yellow] {runtime.context_warning}")

    from obi.slack.listener import SlackListener

    err.print("[dim]obi slack: listening for DMs (read-only). Press Ctrl-C to stop.[/dim]")
    heartbeat = Heartbeat(spec.heartbeat_path, name="slack")
    raise typer.Exit(code=SlackListener(config, runtime, heartbeat).run())


@slack_app.command("start")
def slack_start(
    scope: Annotated[Path | None, _SCOPE_OPT] = None,
    login: Annotated[bool | None, _LOGIN_OPT] = None,
    exec_override: Annotated[Path | None, _EXEC_OPT] = None,
) -> None:
    """Install + start the listener as a supervised launchd agent (restart on crash, at login)."""

    _svc_start("slack", scope, login, exec_override)


@slack_app.command("stop")
def slack_stop(scope: Annotated[Path | None, _SCOPE_OPT] = None) -> None:
    """Stop the supervised listener (bootout — stays stopped until `obi slack start`)."""

    _svc_stop("slack", scope)


@slack_app.command("restart")
def slack_restart(scope: Annotated[Path | None, _SCOPE_OPT] = None) -> None:
    """Restart the supervised listener in place (kickstart -k)."""

    _svc_restart("slack", scope)


@slack_app.command("status")
def slack_status(scope: Annotated[Path | None, _SCOPE_OPT] = None) -> None:
    """Show the listener's launchd state, heartbeat liveness, and cron-collector freshness."""

    _svc_status("slack", scope)


@slack_app.command("say")
def slack_say(
    text: Annotated[str, typer.Argument(help="The message to send.")],
    to: Annotated[
        str | None,
        typer.Option("--to", help="User id or #channel. Default: the configured operator DM."),
    ] = None,
    scope: Annotated[Path | None, _SCOPE_OPT] = None,
) -> None:
    """Send a message through obi's real Slack path — the operator's end-to-end test.

    Uses the same notifier the watcher uses, so a success here is evidence about the path
    that actually delivers alerts, not about a parallel one wired up for testing.
    """

    setup_logging()
    console = Console()
    err = Console(stderr=True)
    config = _config_or_default(scope or default_config_path())

    message = text.strip()
    if not message:
        err.print("[red]nothing to send — the message is empty.[/red]")
        raise typer.Exit(code=1)
    if config.slack is None:
        err.print(r"[red]no \[slack] section in the config — nothing to send through.[/red]")
        raise typer.Exit(code=1)

    from obi.slack.notifier import SlackNotifier

    radio = config.radio
    with _audit_capture(config, "cli:slack-say"):
        notifier = SlackNotifier.from_config(
            config, recipient=radio.slack_user if radio is not None else None
        )
        result = notifier.deliver_status_to(to, message) if to else notifier.deliver_status(message)
    if result.delivered:
        console.print(f"[green]sent[/green] — [dim]{escape(to or 'operator DM')}[/dim]")
        return
    err.print(f"[red]send failed[/red] — {escape(result.error or 'unknown error')}")
    raise typer.Exit(code=1)


@slack_app.command("logs")
def slack_logs(
    lines: Annotated[int, _LINES_OPT] = 40,
    follow: Annotated[bool, _FOLLOW_OPT] = False,
) -> None:
    """Tail the listener's log (`~/.local/state/obi/slack.log`)."""

    _svc_logs("slack", lines, follow)


radio_app = typer.Typer(
    add_completion=False,
    help=(
        "Walkie-talkie radio station (feature 088): an always-on listener that transcribes the "
        "operations channel into a local store, and — behind the hard [radio] tx_enabled gate, "
        "default OFF — answers strict directed calls on the air. Requires the `radio` extra and "
        "the criticalrf USB interface. See docs/setup/radio.md."
    ),
)
app.add_typer(radio_app, name="radio")


def _radio_window_us(window: str, err: Console) -> int:
    """Parse `--window` (`45m` / `24h` / `7d`) into a starting epoch µs bound."""

    import re
    import time

    match = re.fullmatch(r"(\d+)([mhd])", window)
    if not match:
        err.print(f"[red]bad window:[/red] {escape(window)} (use e.g. 45m, 24h, 7d)")
        raise typer.Exit(code=2)
    scale = {"m": 60, "h": 3600, "d": 86400}[match.group(2)]
    return int((time.time() - int(match.group(1)) * scale) * 1_000_000)


def _print_radio_rows(console: Console, rows: list[RadioTransmission], *, as_json: bool) -> None:
    if as_json:
        import dataclasses
        import json as _json

        console.print_json(
            _json.dumps([dataclasses.asdict(row) for row in rows], ensure_ascii=False)
        )
        return
    for row in rows:
        who = row.caller or ("obi" if row.kind == "obi" else "")
        gate = "" if row.stt_gate == "ok" else f" [{row.stt_gate}]"
        marker = {"obi": "[cyan]obi>[/cyan]", "self": "[dim]self[/dim]"}.get(row.kind, "  rx ")
        if row.kind == "obi" and row.outbound_kind == "operator":
            # obi's voice, a human's words (092). Worth distinguishing when reading back a
            # channel: an operator test is not the model answering anyone.
            marker = "[magenta]op> [/magenta]"
            who = "obi (operator)"
        console.print(
            f"[dim]{row.ts_iso}[/dim] {marker} "
            f"{escape(who) + ': ' if who else ''}{escape(row.transcript)}{gate}"
        )


@radio_app.command("run")
def radio_run(
    scope: Annotated[Path | None, _SCOPE_OPT] = None,
) -> None:
    """Run the radio station in the foreground (capture → transcribe → store; TX per config).

    Takes the exclusive station lock first — exactly one radio process may exist (a second
    holder of the serial port turns a killed process into a stuck transmitter). This is what
    the launchd agent runs; use ``obi radio start`` to supervise it.
    """

    setup_logging()
    logger = logging.getLogger("obi")
    console = Console()
    err = Console(stderr=True)
    config_file = scope or default_config_path()
    config = _load_radio_config(config_file, err)
    radio = config.radio
    assert radio is not None

    import os
    import signal
    from datetime import UTC

    from obi.core.service import Heartbeat, radio_spec, service_state
    from obi.radio.capture import SounddeviceSource
    from obi.radio.runner import RadioRunner, acquire_station_lock

    spec = radio_spec()
    if os.environ.get("XPC_SERVICE_NAME") != spec.label:
        try:
            if service_state(spec).loaded:
                err.print(
                    "[yellow]note:[/yellow] a launchd-managed `obi radio` is already loaded — "
                    "the station lock below will refuse a second instance. `obi radio stop` "
                    "first to run in the foreground."
                )
        except Exception:  # noqa: BLE001 — the advisory check must never block the run
            pass

    try:
        station_lock = acquire_station_lock()
    except RuntimeError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    try:
        from obi.voice.errors import VoiceUnavailable
        from obi.voice.stt import MlxWhisperTranscriber, download_model, model_is_cached

        if not model_is_cached(radio.stt_model):
            # KeepAlive would crash-loop a daemon that can't transcribe; self-heal once,
            # loudly (~1.6 GB for the default model — a pre-event setup step, not playa).
            err.print(
                f"[yellow]STT model {radio.stt_model} not cached — downloading now "
                "(one-time).[/yellow]"
            )
            download_model(radio.stt_model)
        # language="en": the channel is English (spec scope) and Whisper's per-clip
        # auto-detect mis-locks on short radio audio (the 2026-08-19 Icelandic lesson).
        # The initial prompt biases decoding toward the channel's actual vocabulary —
        # callsigns, prowords, site/device names (the handle and site-name mis-hearing lessons).
        from obi.radio.runner import build_vocab_prompt

        transcriber = MlxWhisperTranscriber(
            radio.stt_model, language="en", initial_prompt=build_vocab_prompt(radio)
        )
    except (VoiceUnavailable, ModuleNotFoundError) as exc:
        err.print(f"[red]radio unavailable:[/red] {exc} — run: uv sync --extra radio")
        raise typer.Exit(code=3) from exc

    notifier = None
    if config.slack is not None and config.slack.bot_token.reveal():
        import contextlib as _contextlib

        from obi.slack.notifier import SlackNotifier

        with _contextlib.suppress(Exception):
            notifier = SlackNotifier.from_config(config, recipient=radio.slack_user)
    if notifier is None:
        err.print("[yellow]note:[/yellow] no usable [slack] bot token — radio notes disabled.")

    stage = radio.stage()
    console.print(
        f"obi radio: stage [bold]{stage}[/bold] "
        f"(tx_enabled={'[red]true[/red]' if radio.tx_enabled else '[green]false[/green]'})"
    )
    console.print(
        "[dim]obi cannot verify which channel the radio is tuned to — human tuning plus this "
        "config together form the rollout gate. Press Ctrl-C to stop.[/dim]"
    )

    from obi.core.audit import recorder_from_config
    from obi.core.maintenance import read_radio_maintenance

    recorder = recorder_from_config(config)
    runner = RadioRunner(
        settings=radio,
        source_factory=lambda: SounddeviceSource(
            audio_name=radio.audio_name, sample_rate=radio.sample_rate
        ),
        transcriber=transcriber,
        tz=config.logs.tzinfo() if config.logs is not None else UTC,
        notifier=notifier,
        heartbeat=Heartbeat(spec.heartbeat_path, name="radio"),
        recorder=recorder,
        log=lambda line: logger.info("radio", extra={"detail": line}),
        # 135: re-read [maintenance] at DM time — no restart to enter/leave maintenance.
        in_maintenance=lambda: read_radio_maintenance(config_file),
    )

    # The call flow exists only in the shadow-TX and live stages; pure shadow is a listener
    # whose trigger decisions are still recorded per transmission (stage 0 tuning data).
    ptt_controller = None
    if stage in ("shadow-tx", "live"):
        import time as _time

        from obi.core.llm import ChatUnavailable
        from obi.radio.calls import CallMachine
        from obi.radio.devices import resolve_serial_port
        from obi.radio.ptt import PttController, SerialPttPort
        from obi.radio.runner import build_composer
        from obi.radio.tx import SounddevicePlayer, TxExecutor
        from obi.voice.tts import render as tts_render

        try:
            composer = build_composer(config, recorder=recorder)
        except ChatUnavailable as exc:
            err.print(f"[red]chat unavailable:[/red] {exc}")
            raise typer.Exit(code=3) from exc
        if stage == "live":
            node = resolve_serial_port(radio.ftdi_serial, radio.serial_port)
            if node is None:
                err.print(
                    "[yellow]warning:[/yellow] tx_enabled but the serial half is absent — "
                    "running RX-only; transmissions will be blocked until it appears."
                )
            else:
                ptt_controller = PttController(
                    port_factory=lambda: SerialPttPort(node),
                    station_lock=station_lock,
                    watchdog_s=radio.tx_cap_s + (radio.lead_ms + radio.tail_ms) / 1000 + 2.0,
                )
                try:
                    # The one open() of the process lifetime (its 1-10 ms transient included).
                    ptt_controller.open()
                except OSError as exc:
                    # Field 2026-08-29: under restart churn the FTDI node can be stale or
                    # mid-re-enumeration. Same degradation as the absent-device branch
                    # above — RX-only beats a startup crash and a deaf station.
                    err.print(
                        f"[yellow]warning:[/yellow] PTT serial open failed ({exc}) — "
                        "running RX-only; transmissions will be blocked until restart."
                    )
                    ptt_controller = None
        from obi.radio.loudness import build_processor
        from obi.radio.tx import apply_pronunciations, speak_clock_times

        # The TX loudness stage (092) is composed INTO the renderer, not bolted after it:
        # that puts it before the duration cap (which must measure what actually
        # transmits) and inside the canned-phrase render cache (so a repeat ack pays the
        # cost once). tx_gain still applies later, at key-up, so gain changes never
        # invalidate the cache.
        loudness = build_processor(
            enabled=radio.tx_loudness, log=logging.getLogger(__name__).warning
        )

        executor = TxExecutor(
            settings=radio,
            monitor=runner.monitor,
            ptt=ptt_controller,
            # Pronunciation guide + clock respell apply to the AUDIO only — the recorded
            # text is already canonical by the time the renderer runs ('NOC' -> 'knock',
            # '6:15' -> 'six fifteen'; field 2026-08-29: the TTS read a BRC address as a
            # duration, "six minutes and fifteen seconds").
            renderer=lambda text: loudness.process(
                tts_render(
                    speak_clock_times(apply_pronunciations(text, radio.pronunciations)),
                    voice=radio.tts_voice,
                    rate=radio.tts_rate,
                    sample_rate=48_000,
                )
            ),
            player=SounddevicePlayer(audio_name=radio.audio_name),
            now_us=lambda: _time.time_ns() // 1000,
            sleep=_time.sleep,
            on_keyed_window=runner.note_self_tx,
            on_keyed_start=runner.note_self_tx_start,
        )
        machine = CallMachine(
            ack_text=radio.ack_text,
            say_again_text=radio.say_again_text,
            unavailable_text=radio.unavailable_text,
            garble_close_text=radio.garble_close_text,
            call_timeout_s=radio.call_timeout_s,
            compose_deadline_s=radio.compose_deadline_s,
            operator_handles=list(radio.operator_handles),
            sign_offs=list(radio.sign_offs),
            standby_text=radio.standby_text,
            standby_after_s=radio.standby_after_s,
        )
        runner.enable_calls(machine=machine, tx=executor, composer=composer)

    def _terminate(_signum: int, _frame: object) -> None:
        if ptt_controller is not None:
            ptt_controller.handle_signal(_signum, _frame)
        runner.stop()

    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGHUP, _terminate)
    try:
        runner.run()
    except KeyboardInterrupt:
        runner.stop()
    finally:
        if ptt_controller is not None:
            ptt_controller.close()
        del station_lock  # released at process exit; explicit for the reader
    if runner.watchdog_exit_requested:
        # Watchdog escalation (113, contract W6): teardown above already de-keyed; 75
        # tells `launchctl` this was a deliberate hand-back, not a crash or a stop.
        raise typer.Exit(code=75)


@radio_app.command("start")
def radio_start(
    scope: Annotated[Path | None, _SCOPE_OPT] = None,
    login: Annotated[bool | None, _LOGIN_OPT] = None,
    exec_override: Annotated[Path | None, _EXEC_OPT] = None,
) -> None:
    """Install + start the station as a supervised launchd agent (restart on crash, at login)."""

    _svc_start("radio", scope, login, exec_override)


@radio_app.command("stop")
def radio_stop(scope: Annotated[Path | None, _SCOPE_OPT] = None) -> None:
    """Stop the supervised station (bootout — the radio kill switch; stays stopped)."""

    _svc_stop("radio", scope)


@radio_app.command("restart")
def radio_restart(scope: Annotated[Path | None, _SCOPE_OPT] = None) -> None:
    """Restart the supervised station in place (kickstart -k)."""

    _svc_restart("radio", scope)


@radio_app.command("status")
def radio_status(scope: Annotated[Path | None, _SCOPE_OPT] = None) -> None:
    """Launchd state, heartbeat, both hardware halves, rollout stage, and last activity."""

    setup_logging()
    console = Console()
    err = Console(stderr=True)
    config = _load_radio_config(scope or default_config_path(), err)
    radio = config.radio
    assert radio is not None
    _svc_status("radio", scope)

    from obi.radio import devices as radio_devices

    report = radio_devices.probe(
        audio_name=radio.audio_name,
        ftdi_serial=radio.ftdi_serial,
        serial_port=radio.serial_port,
    )
    console.print()
    console.print(
        f"  audio:  {'[green]present[/green]' if report.audio.present else '[red]absent[/red]'} "
        f"— {escape(report.audio.detail)}"
    )
    console.print(
        f"  serial: {'[green]present[/green]' if report.serial.present else '[red]absent[/red]'} "
        f"— {escape(report.serial.detail)}"
    )
    console.print(
        f"  stage:  [bold]{radio.stage()}[/bold] (tx_enabled={str(radio.tx_enabled).lower()}) — "
        "[dim]channel tuning is a human responsibility; obi cannot see it[/dim]"
    )
    from obi.radio import hostgain

    gain = hostgain.read(
        radio.audio_name,
        input_gain=radio.host_input_gain,
        output_gain=radio.host_output_gain,
        tolerance=radio.host_gain_tolerance,
    )
    if gain.verdict == "calibrated":
        console.print(
            "  gains:  [green]calibrated[/green] — "
            f"[dim]input {radio.host_input_gain:.3f} / output {radio.host_output_gain:.2f}[/dim]"
        )
    elif gain.verdict == "drifted":
        console.print(
            "  gains:  [red]DRIFTED[/red] — [dim]the interface likely re-enumerated; both "
            "directions are affected. Run `obi radio gain --restore`[/dim]"
        )
    else:
        console.print(
            f"  gains:  [yellow]unknown[/yellow] — [dim]{escape(gain.detail or '')}[/dim]"
        )

    from obi.radio import loudness as radio_loudness

    if not radio.tx_loudness:
        console.print(
            "  loudness: [yellow]off[/yellow] — [dim]transmitting raw TTS; obi will read "
            "quieter than human talkers ([radio] tx_loudness)[/dim]"
        )
    elif radio_loudness.available():
        console.print(
            f"  loudness: [green]active[/green] — [dim]{radio_loudness.LOUDNORM_FILTER}[/dim]"
        )
    else:
        console.print(
            "  loudness: [red]UNAVAILABLE[/red] — [dim]ffmpeg is not on PATH; transmitting "
            "unprocessed audio. Fix: brew install ffmpeg[/dim]"
        )
    from obi.core import radiostore

    try:
        conn = radiostore.connect_readonly(radio.db_path)
    except radiostore.StoreError:
        console.print("  store:  [dim]absent — no traffic captured yet[/dim]")
        return
    try:
        rows = radiostore.query_window(conn, limit=1, include_self=True)
        obi_rows = radiostore.query_window(conn, limit=1, kind="obi")
    finally:
        conn.close()
    console.print(f"  last rx: {rows[-1].ts_iso if rows else '[dim]never[/dim]'}")
    console.print(f"  last tx: {obi_rows[-1].ts_iso if obi_rows else '[dim]never[/dim]'}")


def _print_gain_report(console: Console, report: object) -> None:
    """Render a host-gain report: values, references, verdict, consequence, knob reminder."""

    from obi.radio.hostgain import GainReport

    assert isinstance(report, GainReport)
    console.print(f"  device '{escape(report.device)}'")
    for scope in (report.input, report.output):
        label = "input " if scope.scope == "input" else "output"
        if not scope.controls:
            console.print(f"    {label}  [yellow]unknown[/yellow] — no readable volume control")
            continue
        for control in scope.controls:
            name = "master" if control.element == 0 else f"chan {control.element}"
            mark = {
                "calibrated": "[green]calibrated[/green]",
                "drifted": "[red]DRIFTED[/red]",
                "unknown": "[yellow]unknown[/yellow]",
            }[scope.verdict]
            console.print(
                f"    {label}  {name:<7} {control.scalar:.3f}   "
                f"(reference {scope.reference:.3f})   {mark}"
            )
        if scope.consequence:
            console.print(f"             [dim]{escape(scope.consequence)}[/dim]")
    if report.verdict == "calibrated":
        console.print("    verdict: [green]calibrated[/green]")
    elif report.verdict == "drifted":
        console.print(
            "    verdict: [red]DRIFTED[/red] — [dim]the interface likely re-enumerated. "
            "Run: obi radio gain --restore[/dim]"
        )
    else:
        console.print(
            f"    verdict: [yellow]unknown[/yellow] — [dim]{escape(report.detail or '')}[/dim]"
        )
    console.print(
        f"    [dim]operator check (obi cannot see this): {escape(report.operator_check)}[/dim]"
    )


@radio_app.command("gain")
def radio_gain(
    scope: Annotated[Path | None, _SCOPE_OPT] = None,
    restore: Annotated[
        bool,
        typer.Option("--restore", help="Set both gains back to the calibrated reference."),
    ] = False,
) -> None:
    """Read the host codec's gain stages against the calibrated reference.

    These do NOT survive USB re-enumeration: after a replug, dock change, or reboot the
    codec comes back at power-on defaults (input 0.504 = +12 dB and clipping, output 1.000)
    and nothing else in obi notices. Exit code carries the verdict, so this is usable as a
    shell check.

    Without ``--restore`` this only reports. obi never changes host audio state on its own.
    """

    setup_logging()
    console = Console()
    err = Console(stderr=True)
    config = _load_radio_config(scope or default_config_path(), err)
    radio = config.radio
    assert radio is not None

    from obi.radio import hostgain

    action = hostgain.restore if restore else hostgain.read
    if restore:
        console.print("[dim]restoring both scopes to the calibrated reference…[/dim]")
    report = action(
        radio.audio_name,
        input_gain=radio.host_input_gain,
        output_gain=radio.host_output_gain,
        tolerance=radio.host_gain_tolerance,
    )
    _print_gain_report(console, report)
    if report.verdict != "calibrated":
        raise typer.Exit(code=1)


@radio_app.command("say")
def radio_say(
    text: Annotated[str, typer.Argument(help="What obi should say on the air.")],
    scope: Annotated[Path | None, _SCOPE_OPT] = None,
    wait: Annotated[
        float,
        typer.Option("--wait", help="Seconds to wait for the station's verdict."),
    ] = 90.0,
) -> None:
    """TRANSMIT a message through the running station — the operator's on-air test.

    This does NOT key the radio itself. The station holds an exclusive lock on the serial
    port (a second holder is the stuck-PTT hazard), so the message is queued in the store
    and the running daemon transmits it through the ordinary gate: clear-channel check,
    duration cap, rate backstops, guaranteed de-key. It gets no exemptions — in particular
    it spends the same rate budget as a real answer, and can therefore suppress one.

    The text is recorded as kind 'operator', never as an 'answer': the log must show that a
    human wrote it. Unreachable from the model (contracts/tx-request.md Q2).
    """

    setup_logging()
    console = Console()
    err = Console(stderr=True)
    config = _load_radio_config(scope or default_config_path(), err)
    radio = config.radio
    assert radio is not None

    message = text.strip()
    if not message:
        err.print("[red]nothing to say — the message is empty.[/red]")
        raise typer.Exit(code=1)

    from obi.core import service as svc

    state = svc.service_state(svc.spec_for("radio"))
    if not state.loaded:
        err.print(
            "[red]the station is not running — nothing would transmit.[/red]\n"
            "[dim]start it with `obi radio start`. `obi radio say` deliberately cannot key "
            "the radio itself: the daemon owns the serial port.[/dim]"
        )
        raise typer.Exit(code=1)

    from datetime import UTC

    from obi.core import radiostore

    with _audit_capture(config, "cli:radio-say"):
        try:
            conn = radiostore.connect(radio.db_path)
        except radiostore.StoreError as exc:
            err.print(f"[red]cannot open the radio store: {exc}[/red]")
            raise typer.Exit(code=1) from exc
        try:
            request_id = radiostore.enqueue_tx_request(
                conn,
                created_us=_time.time_ns() // 1000,
                text=message,
                ttl_s=radio.operator_tx_ttl_s,
                # The 069 rule: ts_iso is localized at write time through the operator's
                # display tz. [logs] is optional, so fall back to UTC (notifier precedent).
                tz=config.logs.tzinfo() if config.logs is not None else UTC,
            )
            console.print(f"[dim]queued for the station — {escape(message)}[/dim]")
            deadline = _time.monotonic() + wait
            row = radiostore.get_tx_request(conn, request_id)
            while _time.monotonic() < deadline:
                row = radiostore.get_tx_request(conn, request_id)
                if row is not None and row.state in ("done", "expired"):
                    break
                _time.sleep(0.25)
        finally:
            conn.close()

    if row is None or row.state not in ("done", "expired"):
        err.print(
            f"[yellow]no verdict after {wait:g}s — the station may be wedged.[/yellow] "
            "[dim]check `obi radio status` and `obi radio logs`.[/dim]"
        )
        raise typer.Exit(code=1)
    if row.state == "expired":
        err.print(
            f"[red]expired[/red] — the station did not pick it up within "
            f"{radio.operator_tx_ttl_s}s. [dim]Nothing was transmitted.[/dim]"
        )
        raise typer.Exit(code=1)

    disposition = row.disposition or "unknown"
    detail = f" — {escape(row.detail)}" if row.detail else ""
    if disposition == "transmitted":
        console.print(f"[green]{disposition}[/green]{detail}")
        return
    err.print(f"[yellow]{disposition}[/yellow]{detail}")
    raise typer.Exit(code=1)


@radio_app.command("call")
def radio_call(
    callee: Annotated[str, typer.Argument(help="Who to hail on the air, by name.")],
    message: Annotated[str, typer.Argument(help="The message to deliver after their go-ahead.")],
    scope: Annotated[Path | None, _SCOPE_OPT] = None,
    wait: Annotated[
        float,
        typer.Option("--wait", help="Seconds to wait for the delivery outcome."),
    ] = 300.0,
) -> None:
    """CALL someone on the radio and deliver a message (feature 124).

    Channel protocol, not a broadcast: the station hails "<Name>, <Name>, obi", waits
    for a go-ahead ("go" / "go for <name>" / "go for obi"…), and only then transmits
    the message — fixed here, shown below, and never aired without the go-ahead. Up to
    2 re-hails, then the failure is reported instead of the message airing blind.

    Like `obi radio say`, this cannot key the radio itself: the request is queued in
    the store and the running daemon drives the exchange through the ordinary transmit
    gate. Reachable from the CLI and the MCP surface only — no chat surface, and no
    radio caller, can originate an outbound call (operator decision 2026-09-02).
    """

    setup_logging()
    console = Console()
    err = Console(stderr=True)
    config = _load_radio_config(scope or default_config_path(), err)
    radio = config.radio
    assert radio is not None

    from obi.radio.outbound import with_over_sign_off

    target = callee.strip()
    body = message.strip()
    if not target:
        err.print("[red]nobody to call — the callee name is empty.[/red]")
        raise typer.Exit(code=1)
    if not body:
        err.print("[red]nothing to deliver — the message is empty.[/red]")
        raise typer.Exit(code=1)
    if len(body) > radio.outbound_max_chars:
        err.print(
            f"[red]message too long for one transmission[/red] ({len(body)} chars > "
            f"[radio] outbound_max_chars = {radio.outbound_max_chars}). obi never "
            "truncates someone's message on the air — please shorten it."
        )
        raise typer.Exit(code=1)
    on_air = with_over_sign_off(body)

    from obi.core import service as svc

    state = svc.service_state(svc.spec_for("radio"))
    if not state.loaded:
        err.print(
            "[red]the station is not running — nobody would be hailed.[/red]\n"
            "[dim]start it with `obi radio start`. `obi radio call` deliberately cannot "
            "key the radio itself: the daemon owns the serial port.[/dim]"
        )
        raise typer.Exit(code=1)

    from datetime import UTC

    from obi.core import radiostore

    with _audit_capture(config, "cli:radio-call"):
        try:
            conn = radiostore.connect(radio.db_path)
        except radiostore.StoreError as exc:
            err.print(f"[red]cannot open the radio store: {exc}[/red]")
            raise typer.Exit(code=1) from exc
        try:
            request_id = radiostore.enqueue_call_request(
                conn,
                created_us=_time.time_ns() // 1000,
                callee=target,
                message=on_air,
                ttl_s=radio.outbound_ttl_s,
                tz=config.logs.tzinfo() if config.logs is not None else UTC,
                origin="cli",
            )
            console.print(f"[dim]hail:[/dim]    {escape(target)}, {escape(target)}, obi")
            console.print(f"[dim]message:[/dim] {escape(on_air)}")
            console.print(
                f"[dim]queued for the station (request {request_id}; up to "
                f"{1 + radio.outbound_max_rehails} hails, TTL {radio.outbound_ttl_s}s)…[/dim]"
            )
            deadline = _time.monotonic() + wait
            row = radiostore.get_call_request(conn, request_id)
            while _time.monotonic() < deadline:
                row = radiostore.get_call_request(conn, request_id)
                if row is not None and row.state in ("done", "expired"):
                    break
                _time.sleep(0.5)
        finally:
            conn.close()

    if row is None or row.state not in ("done", "expired"):
        err.print(
            f"[yellow]no outcome after {wait:g}s — the attempt may still be in "
            "progress.[/yellow] [dim]the result lands in Slack and the audit trail; "
            f"check `obi radio logs`, or re-run with --wait {radio.outbound_ttl_s}.[/dim]"
        )
        raise typer.Exit(code=1)

    outcome = row.outcome or ("expired" if row.state == "expired" else "unknown")
    detail = f" — {escape(row.detail)}" if row.detail else ""
    hails = f" (hails: {row.attempts if row.attempts else 1})"
    if outcome == "delivered":
        console.print(f"[green]delivered[/green]{detail}")
        return
    aired = "delivery not confirmed" if outcome == "failed" else "the message never aired"
    err.print(f"[yellow]{outcome}[/yellow]{detail}{hails} [dim]— {aired}.[/dim]")
    raise typer.Exit(code=1)


@radio_app.command("logs")
def radio_logs(
    lines: Annotated[int, _LINES_OPT] = 40,
    follow: Annotated[bool, _FOLLOW_OPT] = False,
) -> None:
    """Tail the station's log (`~/.local/state/obi/radio.log`)."""

    _svc_logs("radio", lines, follow)


@radio_app.command("tail")
def radio_tail(
    scope: Annotated[Path | None, _SCOPE_OPT] = None,
    count: Annotated[int, typer.Option("--lines", "-n", help="Rows to show.")] = 20,
    kind: Annotated[str | None, typer.Option("--kind", help="Filter: rx, obi, or self.")] = None,
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Keep tailing new rows.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Show recent channel transmissions from the transcript store (read-only)."""

    import time

    console = Console()
    err = Console(stderr=True)
    config = _load_radio_config(scope or default_config_path(), err)
    radio = config.radio
    assert radio is not None
    from obi.core import radiostore

    if kind is not None and kind not in radiostore.TRANSMISSION_KINDS:
        err.print(f"[red]bad --kind:[/red] {escape(kind)} (rx, obi, or self)")
        raise typer.Exit(code=2)
    try:
        conn = radiostore.connect_readonly(radio.db_path)
    except radiostore.StoreError:
        console.print("[dim]no radio store yet — run: obi radio start[/dim]")
        return
    try:
        rows = radiostore.query_window(conn, limit=count, kind=kind)
        _print_radio_rows(console, rows, as_json=as_json)
        if not follow:
            return
        cursor = radiostore.latest_cursor(conn) or 0
        while True:
            time.sleep(2.0)
            fresh = radiostore.query_since(conn, after_id=cursor, limit=200)
            if kind is not None:
                fresh = [r for r in fresh if r.kind == kind]
            if fresh:
                cursor = fresh[-1].id
                _print_radio_rows(console, fresh, as_json=as_json)
    except KeyboardInterrupt:
        return
    finally:
        conn.close()


@radio_app.command("search")
def radio_search(
    query: Annotated[str, typer.Argument(help="Text to find in transcripts (literal phrase).")],
    scope: Annotated[Path | None, _SCOPE_OPT] = None,
    window: Annotated[str, typer.Option("--window", help="How far back: 45m, 24h, 7d.")] = "7d",
    count: Annotated[int, typer.Option("--limit", "-n", help="Max rows.")] = 50,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Full-text search over stored transcripts (read-only)."""

    console = Console()
    err = Console(stderr=True)
    config = _load_radio_config(scope or default_config_path(), err)
    radio = config.radio
    assert radio is not None
    since_us = _radio_window_us(window, err)
    from obi.core import radiostore

    try:
        conn = radiostore.connect_readonly(radio.db_path)
    except radiostore.StoreError:
        console.print("[dim]no radio store yet — run: obi radio start[/dim]")
        return
    try:
        rows = radiostore.query_window(
            conn, limit=count, since_us=since_us, contains=query, include_self=True
        )
    finally:
        conn.close()
    if not rows:
        console.print(f"[dim]no matches for {escape(query)!s} in the last {window}[/dim]")
        return
    _print_radio_rows(console, rows, as_json=as_json)


@radio_app.command("mishears")
def radio_mishears(
    scope: Annotated[Path | None, _SCOPE_OPT] = None,
    count: Annotated[int, typer.Option("--limit", "-n", help="Recent hails to scan.")] = 500,
) -> None:
    """Suggest [radio.caller_aliases] entries from unidentified hails (read-only).

    Lists the raw handles heard in callsign position that matched nothing — "Obi, Obi,
    sir" — grouped by frequency, with paste-ready alias lines for the config."""

    console = Console()
    err = Console(stderr=True)
    config = _load_radio_config(scope or default_config_path(), err)
    radio = config.radio
    assert radio is not None
    from obi.core import radiostore
    from obi.radio.trigger import heard_handle, match_caller

    try:
        conn = radiostore.connect_readonly(radio.db_path)
    except radiostore.StoreError:
        console.print("[dim]no radio store yet — run: obi radio start[/dim]")
        return
    try:
        rows = conn.execute(
            "SELECT transcript, started_us FROM transmissions"
            " WHERE kind = 'rx' AND trigger_fired = 1 AND caller IS NULL"
            " ORDER BY id DESC LIMIT ?",
            (count,),
        ).fetchall()
    finally:
        conn.close()
    roster = [*radio.operator_handles, *radio.known_callers]
    heard: dict[str, dict[str, object]] = {}
    for transcript, started_us in rows:
        handle = heard_handle(str(transcript), variants=radio.name_variants)
        if handle is None:
            continue
        if match_caller(handle, roster, radio.caller_aliases) is not None:
            continue  # already resolves under the CURRENT aliases — curated
        entry = heard.setdefault(handle, {"count": 0, "last_us": 0, "sample": str(transcript)})
        entry["count"] = int(entry["count"]) + 1  # type: ignore[call-overload]
        if int(started_us) > int(entry["last_us"]):  # type: ignore[call-overload]
            entry["last_us"] = int(started_us)
            entry["sample"] = str(transcript)
    if not heard:
        console.print("[dim]no unresolved hail handles in the scanned rows[/dim]")
        return
    from datetime import datetime

    table = Table()
    table.add_column("heard", style="bold")
    table.add_column("count", justify="right")
    table.add_column("last", style="dim")
    table.add_column("sample", style="dim", max_width=48)
    ranked = sorted(heard.items(), key=lambda kv: -int(kv[1]["count"]))  # type: ignore[call-overload]
    for handle, entry in ranked:
        last = datetime.fromtimestamp(int(entry["last_us"]) / 1_000_000).astimezone()  # type: ignore[call-overload]
        table.add_row(
            handle,
            str(entry["count"]),
            last.strftime("%m-%d %H:%M"),
            escape(str(entry["sample"])),
        )
    console.print(table)
    console.print("paste into [bold]\\[radio.caller_aliases][/bold] with the real callsign:")
    for handle, _entry in ranked:
        console.print(f'  {handle} = "?"')


@radio_app.command("devices")
def radio_devices_cmd(scope: Annotated[Path | None, _SCOPE_OPT] = None) -> None:
    """Probe both halves of the USB interface (read-only; never opens the serial port)."""

    console = Console()
    err = Console(stderr=True)
    config = _load_radio_config(scope or default_config_path(), err)
    radio = config.radio
    assert radio is not None
    from obi.radio import devices as radio_devices

    report = radio_devices.probe(
        audio_name=radio.audio_name,
        ftdi_serial=radio.ftdi_serial,
        serial_port=radio.serial_port,
    )
    console.print(
        f"audio:  {'[green]present[/green]' if report.audio.present else '[red]absent[/red]'} "
        f"— {escape(report.audio.detail)}"
    )
    console.print(
        f"serial: {'[green]present[/green]' if report.serial.present else '[red]absent[/red]'} "
        f"— {escape(report.serial.detail)}"
    )
    if report.audio.present:
        # Presence is not health: a device that is plugged in but at power-on gains clips
        # RX and changes TX level, and looks perfectly "present" (092).
        from obi.radio import hostgain

        console.print()
        _print_gain_report(
            console,
            hostgain.read(
                radio.audio_name,
                input_gain=radio.host_input_gain,
                output_gain=radio.host_output_gain,
                tolerance=radio.host_gain_tolerance,
            ),
        )
    if not report.all_present:
        err.print(
            "[yellow]hint:[/yellow] if the interface is plugged in, suspect the hub chain "
            "first — try a direct machine port (criticalrf lesson)."
        )
        raise typer.Exit(code=1)


#: A block at or above this is pinned to the rail — the ADC is clipping, not the codec.
_LEVEL_CLIP_DBFS = -0.1

#: Peak-hold decay per block, so a peak stays readable for ~2 s after you stop talking.
_LEVEL_HOLD_DECAY_DB = 0.5

#: What a healthy channel looks like: the banner's own target of a -12…-6 dBFS speech PEAK,
#: which for speech (12-15 dB crest factor) puts RMS around -20. The code's criticalrf notes
#: cite voiced frames at -15..-30 dBFS, which agrees.
_LEVEL_TARGET_PEAK_DBFS = -9.0

#: The matching RMS target — the level judgement is made on this, not on the peak.
_LEVEL_TARGET_RMS_DBFS = -20.0

#: Speech must clear squelch by at least this much or quiet callers never open the gate.
_LEVEL_MIN_SQUELCH_HEADROOM_DB = 8.0

#: Fraction of SPEECH blocks pinned before the input counts as genuinely clipping.
_LEVEL_CLIPPED_PCT_HOT = 5.0

#: Peak-over-rms beyond this is not speech dynamics — it is clicks or interference.
_LEVEL_TRANSIENT_CREST_DB = 25.0

#: How far speech may sit either side of the target and still count as GOOD.
_LEVEL_GOOD_BAND_DB = 6.0


def _render_level_verdict(
    console: Console,
    speech_rms: list[float],
    block_peaks: list[float],
    clipped_blocks: int,
    max_peak: float,
    squelch_dbfs: float,
    block_ms: float,
) -> None:
    """Say what the setting should be, not just what it is.

    Two robustness lessons, both from the field (2026-08-21):

    Only blocks above squelch count — averaging in the silence between transmissions drags
    the figure toward the noise floor and makes a hot input look fine.

    And the SESSION MAX peak is not a level measurement. One key-up click or a burst of RFI
    pins it at 0 dBFS regardless of how the radio is set, so a verdict keyed on it chased
    the operator's radio down to 0/10 while speech fell to 3 dB above squelch. The level
    judgement is made on the median rms of speech; the peak is reported, and an isolated
    full-scale peak over quiet speech is called out for what it is — a transient, not gain.
    """

    if not speech_rms:
        console.print(
            "[yellow]no traffic above squelch[/yellow] — nothing to judge. Key up and talk "
            "for a few seconds while this runs."
        )
        return
    ordered = sorted(speech_rms)
    median_rms = ordered[len(ordered) // 2]
    peaks = sorted(block_peaks)
    # 95th percentile: representative of speech peaks, immune to a lone click.
    typical_peak = peaks[min(len(peaks) - 1, int(len(peaks) * 0.95))]
    crest = typical_peak - median_rms
    seconds_of_traffic = len(speech_rms) * block_ms / 1000.0
    clipped_pct = 100.0 * clipped_blocks / len(speech_rms)
    console.print(
        f"traffic: {len(speech_rms)} block(s), {seconds_of_traffic:.1f}s — "
        f"median rms {median_rms:.1f} dBFS, typical peak {typical_peak:.1f} dBFS "
        f"(max {max_peak:.1f}), {clipped_pct:.0f}% pinned to the rail"
    )
    adjust = _LEVEL_TARGET_RMS_DBFS - median_rms
    headroom = median_rms - squelch_dbfs
    # Is the clipping the VOICE, or something on top of it? Speech that clips does so at a
    # normal crest factor — its rms is up near the rail too. A 31 dB crest with speech at
    # -31 dBFS means the rail is being hit by something else entirely, and reading that as
    # "too hot" turns the volume down for a problem the volume does not control.
    transient = crest > _LEVEL_TRANSIENT_CREST_DB
    if clipped_pct >= _LEVEL_CLIPPED_PCT_HOT and not transient:
        console.print(
            f"[red]TOO HOT[/red] — the ADC is clipping on speech, so the distortion is in the "
            f"recording before obi sees it. Turn the RADIO's volume down about "
            f"{abs(adjust):.0f} dB and run this again (sized from the rms: a pinned peak "
            "reads 0.0 dBFS however far over you are)."
        )
    elif median_rms > _LEVEL_TARGET_RMS_DBFS + _LEVEL_GOOD_BAND_DB:
        console.print(
            f"[yellow]HOT[/yellow] — speech is {abs(adjust):.0f} dB above target. Come down "
            "about that much."
        )
    elif headroom < _LEVEL_MIN_SQUELCH_HEADROOM_DB:
        console.print(
            f"[yellow]TOO QUIET[/yellow] — speech sits only {headroom:.0f} dB above the "
            f"{squelch_dbfs:.0f} dBFS squelch, so a quiet caller will not open it at all. "
            f"Bring the radio UP about {adjust:.0f} dB."
        )
    elif median_rms < _LEVEL_TARGET_RMS_DBFS - _LEVEL_GOOD_BAND_DB:
        console.print(
            f"[yellow]LOW[/yellow] — usable, but speech is {adjust:.0f} dB under target and "
            "that much closer to the squelch than it needs to be. Bring the radio up about "
            f"{adjust:.0f} dB when you can."
        )
    else:
        console.print("[green]GOOD[/green] — speech sits in range with headroom to spare.")

    if transient:
        verb = "and it is what is clipping" if clipped_pct else "though it is not clipping yet"
        console.print(
            f"[yellow]note:[/yellow] peaks run {crest:.0f} dB above speech (normal is 12-18), "
            f"{verb}. Something is hitting full scale that is NOT the voice — a key-up click, "
            "squelch crash, or RF from a handheld too close to the cable. Turning the radio "
            "down will not fix it, and chasing it only starves the speech level."
        )


@radio_app.command("level")
def radio_level(
    scope: Annotated[Path | None, _SCOPE_OPT] = None,
    seconds: Annotated[int, typer.Option("--seconds", help="How long to meter.")] = 10,
) -> None:
    """Live dBFS meter from the receive path — the calibration aid (quickstart §1).

    Refuses while the daemon holds the station lock: two holders of the capture device
    give misleading readings, and the same lock guards the serial port.
    """

    import threading as _threading

    console = Console()
    err = Console(stderr=True)
    config = _load_radio_config(scope or default_config_path(), err)
    radio = config.radio
    assert radio is not None
    from obi.radio.capture import SounddeviceSource, dbfs
    from obi.radio.runner import acquire_station_lock

    try:
        station_lock = acquire_station_lock()
    except RuntimeError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    source = SounddeviceSource(audio_name=radio.audio_name, sample_rate=radio.sample_rate)
    timer = _threading.Timer(float(seconds), source.stop)
    timer.daemon = True
    timer.start()
    # Calibration is a knob-turning LOOP: talk, read, adjust, repeat. The instantaneous
    # line is unreadable for that (it shows silence the moment you stop to look), so the
    # meter holds the peak and — the number that actually decides the setting — summarizes
    # only the blocks that carried TRAFFIC, ignoring the silence between transmissions.
    max_peak = max_rms = -120.0
    hold_peak = -120.0
    speech_rms: list[float] = []
    block_peaks: list[float] = []
    clipped_blocks = 0
    block_ms = 0.0
    console.print(
        f"[dim]metering {seconds}s — squelch at {radio.squelch_dbfs} dBFS. Key up and talk "
        "normally; the verdict below is what matters, not the live line.[/dim]"
    )
    try:
        for _t_us, samples in source.blocks():
            peak, rms = dbfs(samples)
            max_peak, max_rms = max(max_peak, peak), max(max_rms, rms)
            hold_peak = max(hold_peak - _LEVEL_HOLD_DECAY_DB, peak)
            if not block_ms and radio.sample_rate:
                block_ms = 1000.0 * len(samples) / radio.sample_rate
            active = peak >= radio.squelch_dbfs
            if active:
                speech_rms.append(rms)
                block_peaks.append(peak)
                if peak >= _LEVEL_CLIP_DBFS:
                    clipped_blocks += 1
            flag = " CLIP" if peak >= _LEVEL_CLIP_DBFS else "     "
            print(
                f"\rpeak {peak:6.1f}  hold {hold_peak:6.1f}{flag}  rms {rms:6.1f} dBFS  "
                f"[{'ACTIVE' if active else 'quiet '}]  traffic blocks: {len(speech_rms):4d}",
                end="",
            )
    except KeyboardInterrupt:
        source.stop()
    except Exception as exc:  # noqa: BLE001 — a meter failure is a message, not a stack trace
        err.print(f"\n[red]capture failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    finally:
        timer.cancel()
        del station_lock
    print()
    console.print(f"session max: peak {max_peak:.1f} dBFS, rms {max_rms:.1f} dBFS")
    _render_level_verdict(
        console,
        speech_rms,
        block_peaks,
        clipped_blocks,
        max_peak,
        radio.squelch_dbfs,
        block_ms,
    )


@radio_app.command("selftest")
def radio_selftest(
    scope: Annotated[Path | None, _SCOPE_OPT] = None,
    hold: Annotated[
        int,
        typer.Option(
            "--hold",
            help="Hold PTT keyed for N seconds (for the operator kill-9 de-key check).",
        ),
    ] = 0,
) -> None:
    """KEYS THE TRANSMITTER briefly — the operator-run hardware verification (quickstart §4).

    Power the handset off (or use a dummy load) first. Asserts and reads back each control
    line, then clears both. `--hold N` keeps PTT asserted for N seconds and prints the PID
    so you can `kill -9` it from another terminal and watch the TX indicator drop — the
    sole-holder de-key check (SC-008). Refuses while the daemon holds the station lock.
    Never run by tests or CI (FR-036).
    """

    import os

    console = Console()
    err = Console(stderr=True)
    config = _load_radio_config(scope or default_config_path(), err)
    radio = config.radio
    assert radio is not None

    err.print(
        "[red bold]This will KEY THE TRANSMITTER.[/red bold] Power the radio off or use a "
        "dummy load first."
    )
    phrase = "key the transmitter"
    answer = typer.prompt(f"Type exactly '{phrase}' to continue", default="", show_default=False)
    if answer.strip() != phrase:
        err.print("[yellow]aborted — nothing was keyed.[/yellow]")
        raise typer.Exit(code=1)

    from obi.radio.devices import resolve_serial_port
    from obi.radio.runner import acquire_station_lock

    try:
        station_lock = acquire_station_lock()
    except RuntimeError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    node = resolve_serial_port(radio.ftdi_serial, radio.serial_port)
    if node is None:
        err.print("[red]no serial node found — is the FTDI half plugged in?[/red]")
        raise typer.Exit(code=1)

    import fcntl
    import struct
    import termios
    import time as _time

    import serial as pyserial

    def modem_bits(fd: int) -> tuple[bool, bool]:
        raw = fcntl.ioctl(fd, termios.TIOCMGET, struct.pack("I", 0))
        bits = struct.unpack("I", raw)[0]
        return bool(bits & termios.TIOCM_DTR), bool(bits & termios.TIOCM_RTS)

    with _audit_capture(config, "cli:radio-selftest"):
        from obi.core.audit import record_event

        port = pyserial.Serial(node, exclusive=True)
        try:
            port.dtr = False
            port.rts = False
            console.print(f"opened {node} — both lines cleared after the open transient")
            failures = 0
            for line in ("dtr", "rts"):
                setattr(port, line, True)
                _time.sleep(0.05)
                dtr, rts = modem_bits(port.fileno())
                asserted = dtr if line == "dtr" else rts
                setattr(port, line, False)
                _time.sleep(0.05)
                dtr, rts = modem_bits(port.fileno())
                cleared = not (dtr if line == "dtr" else rts)
                verdict = "[green]PASS[/green]" if asserted and cleared else "[red]FAIL[/red]"
                failures += 0 if (asserted and cleared) else 1
                console.print(
                    f"  {line.upper()}: assert {'ok' if asserted else 'STUCK LOW'}, "
                    f"clear {'ok' if cleared else 'STUCK HIGH'}  {verdict}"
                )
            if hold > 0:
                console.print(
                    f"[bold]holding PTT (RTS) for {hold}s — pid {os.getpid()}[/bold]  "
                    f"(kill -9 {os.getpid()} from another terminal; the TX indicator must "
                    "drop immediately)"
                )
                port.rts = True
                try:
                    for remaining in range(hold, 0, -1):
                        print(f"\r  keyed… {remaining:3d}s ", end="")
                        _time.sleep(1)
                finally:
                    port.rts = False
                    port.dtr = False
                    print("\r  de-keyed.          ")
            record_event(
                "action",
                summary=f"radio selftest on {node}",
                payload={"failures": failures, "hold_s": hold},
                outcome="ok" if failures == 0 else "error",
            )
            if failures:
                raise typer.Exit(code=1)
        finally:
            with contextlib.suppress(Exception):
                port.rts = False
                port.dtr = False
            port.close()
    del station_lock  # held through the keyed window; released at process exit regardless
    console.print("[green]selftest complete — transmitter unkeyed.[/green]")


logs_app = typer.Typer(
    add_completion=False,
    help=(
        "Query device syslog ingested from the syslog-ng collectors (feature 013). "
        "`obi logs ingest` parses the rsync-mirrored files into a local store; `obi logs query` "
        "reads it. The sync itself (rsync + timer) is an operator/ops task — see "
        "README.md § Device syslog."
    ),
)
app.add_typer(logs_app, name="logs")


def _render_log_report(report: LogReport, console: Console) -> None:
    """Render a :class:`LogReport` (feature 013) as a Rich table + summary line."""

    table = Table(title="Device syslog", title_justify="left")
    for column in ("Time", "Host", "Topics", "Severity", "Message", "Collectors"):
        table.add_column(column, overflow="fold")
    for event in report.events:
        # Escape every free-text cell: log lines are untrusted device output and routinely contain
        # bracketed tokens (e.g. a "[/etc/ble-config.json]" path) that Rich would parse as markup
        # and crash on (MarkupError). Escaping renders them literally.
        table.add_row(
            event.ts.strftime("%Y-%m-%d %H:%M:%S"),
            escape(event.host),
            escape(event.topics),
            event.severity or _DASH,
            escape(event.message),
            escape(",".join(event.collectors)) or _DASH,
        )
    console.print(table)

    if report.outcome is not SourceOutcome.REACHABLE:
        detail = f": {report.detail}" if report.detail else ""
        console.print(f"logs {report.outcome.value}{detail}")
        return
    counts = report.counts
    parts = [f"{k}={v}" for k, v in sorted(counts.items()) if k != "total"]
    suffix = " (truncated — narrow the filters for earlier lines)" if report.truncated else ""
    console.print(f"{counts['total']} line(s): " + ", ".join(parts) + suffix)


@logs_app.command("ingest")
def logs_ingest(
    scope: Annotated[
        Path | None,
        typer.Option("--scope", help="Path to the config file (default: XDG config)."),
    ] = None,
    mirror_root: Annotated[
        Path | None,
        typer.Option("--mirror-root", help="Override the rsync mirror root to ingest from."),
    ] = None,
    db: Annotated[Path | None, typer.Option("--db", help="Override the SQLite store path.")] = None,
) -> None:
    """Parse the rsync-mirrored syslog-ng files into the local store (incremental + idempotent)."""

    setup_logging()
    err = Console(stderr=True)
    config = _load_logs_config(scope or default_config_path(), err)
    assert config.logs is not None  # _load_logs_config guarantees it
    logs_cfg = config.logs
    root = (mirror_root or logs_cfg.mirror_root).expanduser()
    db_path = (db or logs_cfg.db_path).expanduser()

    if not root.exists():
        err.print(
            f"[yellow]note:[/yellow] mirror root {root} does not exist yet — sync the collectors "
            "first (rsync; see the quickstart). Nothing to ingest."
        )
        raise typer.Exit(code=0)

    from obi.core import logstore
    from obi.core.audit import AuditTrail
    from obi.core.models import AuditOutcome, AuditRecord, _utcnow

    with _audit_capture(config, "cli:logs-ingest"):
        conn = logstore.connect(db_path)
        try:
            stats = logstore.ingest_tree(conn, root, tz=logs_cfg.tzinfo())
        finally:
            conn.close()

        AuditTrail(config.audit_path).record(
            AuditRecord(
                timestamp=_utcnow(),
                action="logs_ingest",
                target=f"logs:{db_path}",
                reason="operator-initiated log ingest",
                outcome=AuditOutcome.OK,
                params={
                    "files_scanned": stats.files_scanned,
                    "files_ingested": stats.files_ingested,
                    "inserted": stats.inserted,
                    "deduped": stats.deduped,
                    "skipped_malformed": stats.skipped_malformed,
                },
            )
        )
    Console().print(
        f"ingested {root} → {db_path}: {stats.files_scanned} file(s) scanned, "
        f"{stats.lines_parsed} line(s) parsed, {stats.inserted} new, {stats.deduped} duplicate, "
        f"{stats.skipped_malformed} malformed skipped."
    )


@logs_app.command("query")
def logs_query(
    scope: Annotated[
        Path | None,
        typer.Option("--scope", help="Path to the config file (default: XDG config)."),
    ] = None,
    host: Annotated[
        str | None, typer.Option("--host", help="Case-insensitive substring of the host name.")
    ] = None,
    severity: Annotated[
        str | None,
        typer.Option("--severity", help="debug | info | warning | error | critical."),
    ] = None,
    contains: Annotated[
        str | None, typer.Option("--contains", help="Full-text phrase within the message.")
    ] = None,
    source: Annotated[
        str | None,
        typer.Option(
            "--source",
            help="Filter by source family: 'syslog' = device syslog only (all RouterOS collector "
            "hosts, no controller events), 'unifi' = UniFi events, 'uisp' = UISP events. A "
            "collector hostname/substring still works — disambiguates a UniFi site from a "
            "like-named RouterOS host.",
        ),
    ] = None,
    since: Annotated[
        str | None,
        typer.Option("--since", help="Window start: '1h'/'2d' lookback or an ISO date/time."),
    ] = None,
    until: Annotated[
        str | None, typer.Option("--until", help="Window end (same formats as --since).")
    ] = None,
    limit: Annotated[int | None, typer.Option("--limit", help="Max lines (newest first).")] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON instead of a table.")
    ] = False,
) -> None:
    """Query the ingested device syslog store (read-only)."""

    setup_logging()
    err = Console(stderr=True)
    config = _load_logs_config(scope or default_config_path(), err)
    assert config.logs is not None
    logs_cfg = config.logs

    from obi.core import logstore
    from obi.core.audit import AuditTrail
    from obi.core.drivers.sqlite_logs import SqliteLogSource
    from obi.core.engine import query_logs
    from obi.core.serialize import log_report_to_json

    tz = logs_cfg.tzinfo()
    report = query_logs(
        SqliteLogSource(logs_cfg),
        audit=AuditTrail(config.audit_path),
        db_label=str(logs_cfg.db_path),
        host=host,
        since=logstore.parse_when(since, tz),
        until=logstore.parse_when(until, tz),
        severity=severity.lower() if severity else None,
        contains=contains,
        source=source,
        limit=limit if limit is not None else logs_cfg.default_limit,
        timeout=logs_cfg.timeout,
    )
    if report.outcome is not SourceOutcome.REACHABLE:
        detail = f" — {report.detail}" if report.detail else ""
        err.print(f"[yellow]logs:[/yellow] {report.outcome.value}{detail}")
    if json_output:
        sys.stdout.write(log_report_to_json(report) + "\n")
    else:
        _render_log_report(report, Console())


unifi_logs_app = typer.Typer(
    add_completion=False,
    help=(
        "Pull UniFi controller event logs into the local store via the reverse-engineered "
        "controller API (feature 016). `obi unifi logs ingest` polls each configured site's "
        "system log into the \\[logs] store (then queryable via `obi logs query` and judged by "
        "`obi watch`); `obi unifi logs status` shows the per-site ingest watermark. Requires a "
        "\\[unifi-internal] local-admin credential — see README.md § UniFi controller events."
    ),
)
unifi_app.add_typer(unifi_logs_app, name="logs")


@unifi_logs_app.command("ingest")
def unifi_logs_ingest(
    scope: Annotated[
        Path | None,
        typer.Option("--scope", help="Path to the config file (default: XDG config)."),
    ] = None,
) -> None:
    """Pull UniFi controller events into the local log store (incremental; first run backfills)."""

    setup_logging()
    err = Console(stderr=True)
    config = _load_unifi_internal_config(scope or default_config_path(), err)

    from obi.core.audit import AuditTrail
    from obi.core.engine import unifi_ingest

    with _audit_capture(config, "cli:unifi-logs-ingest"):
        report = unifi_ingest(
            config,
            audit=AuditTrail(config.audit_path),
            reason="operator-initiated UniFi event ingest",
        )

    console = Console()
    for site in report.sites:
        if site.outcome is SourceOutcome.REACHABLE:
            console.print(
                f"{site.site}: {site.inserted} new, {site.deduped} duplicate, "
                f"{site.skipped} skipped"
            )
        else:
            detail = f" — {site.detail}" if site.detail else ""
            console.print(f"[yellow]{site.site}: {site.outcome.value}{detail}[/yellow]")
    if not report.sites:
        detail = f" — {report.detail}" if report.detail else ""
        err.print(f"[yellow]unifi-logs:[/yellow] no sites ingested{detail}")
    console.print(
        f"total: {report.inserted} new, {report.deduped} duplicate, {report.skipped} skipped "
        f"across {len(report.sites)} site(s), {report.failed_sites} failed."
    )


@unifi_logs_app.command("status")
def unifi_logs_status(
    scope: Annotated[
        Path | None,
        typer.Option("--scope", help="Path to the config file (default: XDG config)."),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON instead of a table.")
    ] = False,
) -> None:
    """Show the per-site UniFi ingest watermark (last event time + last ingest time)."""

    setup_logging()
    err = Console(stderr=True)
    config = _load_unifi_internal_config(scope or default_config_path(), err)

    import json
    from datetime import datetime

    from obi.core import logstore
    from obi.core.config import LogsSettings

    logs_cfg = config.logs or LogsSettings()
    tz = logs_cfg.tzinfo()
    db_path = logs_cfg.db_path.expanduser()
    watermarks: list[tuple[str, int, str | None, str]] = []
    if db_path.exists():
        conn = logstore.connect_readonly(db_path)
        try:
            watermarks = logstore.list_unifi_watermarks(conn)
        finally:
            conn.close()

    if json_output:
        payload = [
            {
                "site": site,
                "last_event": datetime.fromtimestamp(ts / 1000, tz=tz).isoformat(),
                "last_event_id": eid,
                "last_ingest": upd,
            }
            for (site, ts, eid, upd) in watermarks
        ]
        sys.stdout.write(json.dumps(payload) + "\n")
        return

    if not watermarks:
        Console().print("no UniFi events ingested yet — run `obi unifi logs ingest`.")
        return
    table = Table(title="UniFi ingest watermark", title_justify="left")
    for column in ("Site", "Last event", "Last ingest"):
        table.add_column(column, overflow="fold")
    for site, ts, _eid, upd in watermarks:
        when = datetime.fromtimestamp(ts / 1000, tz=tz).strftime("%Y-%m-%d %H:%M:%S")
        table.add_row(site, when, upd)
    Console().print(table)


uisp_logs_app = typer.Typer(
    add_completion=False,
    help=(
        "Pull UISP controller logs into the local store (feature 019). `obi uisp logs ingest` "
        "polls the controller's event log (GET /logs) and device outages (GET /outages) into the "
        "\\[logs] store (queryable via `obi logs query --source uisp` and judged by `obi watch`); "
        "`obi uisp logs status` shows the per-source ingest watermark. Requires the feature-003 "
        "\\[uisp] section with `ingest_events`/`ingest_outages` enabled — see "
        "README.md § UISP controller logs."
    ),
)
uisp_app.add_typer(uisp_logs_app, name="logs")


@uisp_logs_app.command("ingest")
def uisp_logs_ingest(
    scope: Annotated[
        Path | None,
        typer.Option("--scope", help="Path to the config file (default: XDG config)."),
    ] = None,
) -> None:
    """Pull UISP events + outages into the local log store (incremental; first run backfills)."""

    setup_logging()
    err = Console(stderr=True)
    config = _load_uisp_ingest_config(scope or default_config_path(), err)

    from obi.core.audit import AuditTrail
    from obi.core.engine import uisp_ingest

    with _audit_capture(config, "cli:uisp-logs-ingest"):
        report = uisp_ingest(
            config,
            audit=AuditTrail(config.audit_path),
            reason="operator-initiated UISP log ingest",
        )

    console = Console()
    for source in report.sources:
        if source.outcome is SourceOutcome.REACHABLE:
            console.print(
                f"{source.source}: {source.inserted} new, {source.deduped} duplicate, "
                f"{source.skipped} skipped"
            )
        else:
            detail = f" — {source.detail}" if source.detail else ""
            console.print(f"[yellow]{source.source}: {source.outcome.value}{detail}[/yellow]")
    if not report.sources:
        detail = f" — {report.detail}" if report.detail else ""
        err.print(f"[yellow]uisp-logs:[/yellow] no sources ingested{detail}")
    console.print(
        f"total: {report.inserted} new, {report.deduped} duplicate, {report.skipped} skipped "
        f"across {len(report.sources)} source(s), {report.failed_sources} failed."
    )


@uisp_logs_app.command("status")
def uisp_logs_status(
    scope: Annotated[
        Path | None,
        typer.Option("--scope", help="Path to the config file (default: XDG config)."),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON instead of a table.")
    ] = False,
) -> None:
    """Show the per-source UISP ingest watermark (last record time + last ingest time)."""

    setup_logging()
    err = Console(stderr=True)
    config = _load_uisp_ingest_config(scope or default_config_path(), err)

    import json
    from datetime import datetime

    from obi.core import logstore
    from obi.core.config import LogsSettings

    logs_cfg = config.logs or LogsSettings()
    tz = logs_cfg.tzinfo()
    db_path = logs_cfg.db_path.expanduser()
    watermarks: list[tuple[str, int, str | None, str]] = []
    if db_path.exists():
        conn = logstore.connect_readonly(db_path)
        try:
            watermarks = logstore.list_uisp_watermarks(conn)
        finally:
            conn.close()

    if json_output:
        payload = [
            {
                "source": source,
                "last_record": datetime.fromtimestamp(ts / 1000, tz=tz).isoformat(),
                "last_id": rid,
                "last_ingest": upd,
            }
            for (source, ts, rid, upd) in watermarks
        ]
        sys.stdout.write(json.dumps(payload) + "\n")
        return

    if not watermarks:
        Console().print("no UISP records ingested yet — run `obi uisp logs ingest`.")
        return
    table = Table(title="UISP ingest watermark", title_justify="left")
    for column in ("Source", "Last record", "Last ingest"):
        table.add_column(column, overflow="fold")
    for source, ts, _rid, upd in watermarks:
        when = datetime.fromtimestamp(ts / 1000, tz=tz).strftime("%Y-%m-%d %H:%M:%S")
        table.add_row(source, when, upd)
    Console().print(table)


web_app = typer.Typer(
    add_completion=False,
    help=(
        "The web terminal — a multi-window workspace where each tool is a widget (feature "
        "037 over the 022-033 viewer stack; read-only). `obi web export` writes the link "
        "geometry (UISP coordinates + PtP topology, resolved heights, health) to links.json "
        "plus terrain to terrain.json (ground profiles with Fresnel-clearance verdicts, "
        "per-site wireframe grids, GNIS peak labels — DEM tiles cached durably, offline "
        "after the first run) — run it on a cron (like the other ingests) for live link "
        "status; `obi web serve` hosts the workspace on the LAN over HTTPS (self-signed — "
        "accept the warning once per device): floating windows for the 2D map (MAP), the "
        "ask-obi conversation (TALK), live ping/traceroute from a router (PING/TRACE), the "
        "immersive 3D/AR/VR session "
        "launcher (XR, on WebXR headsets), the device-log tail (LOGS), and per-tool boards "
        "(UISP devices/LINKS backhaul/STAT/SIK/UNIFI/NBX from `obi web snapshot` cron files; "
        "WATCH/INV live from "
        "local stores — the server itself still never touches a device), metrics charts "
        "(MET, the [snmp.history] store) and config backups (CFG, coverage/history/diffs); "
        "mnemonic command line + menu, layouts persist "
        "per browser, each widget also full-screen at ?w=<id>; data re-read every 5 min. "
        "Requires the \\[uisp] section; peak labels need \\[web] gnis_states. See "
        "README.md § Web terminal."
    ),
)
app.add_typer(web_app, name="web")


@app.command(
    "ar",
    hidden=True,
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def ar_superseded(ctx: typer.Context) -> None:
    """Superseded (feature 033): the AR viewer merged into the unified web surface."""

    # Hard rename with a named replacement (FR-008) — the fleet precedent (`obi snmp` →
    # `obi metrics live`), plus the guidance a bare "no such command" never gave.
    err = Console(stderr=True)
    subcommand = ctx.args[0] if ctx.args else None
    if subcommand in ("export", "serve"):
        replacement = f"obi web {subcommand}"
    else:
        replacement = "obi web (export / serve)"
    err.print(
        f"[red]obi ar was replaced in feature 033:[/red] use [bold]{replacement}[/bold] — "
        "one origin now serves a 2D version (any browser) and the 3D/AR/VR version "
        "(WebXR headsets). Config section \\[ar] is now \\[web]. "
        "Migration notes: README.md § Web terminal."
    )
    raise typer.Exit(code=2)


@web_app.command("export")
def web_export_cmd(
    scope: Annotated[
        Path | None,
        typer.Option("--scope", help="Path to the config file (default: XDG config)."),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Export path (default: \\[web] export_path)."),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit a machine-readable summary instead of a table.")
    ] = False,
) -> None:
    """Export the PtP backhaul geometry + terrain (read-only; atomic; audited).

    First run fetches each site's ground elevation from USGS, the DEM tiles under the
    network, and (if \\[web] gnis_states is set) the GNIS gazetteer — all cached durably;
    later runs work with zero internet. Terrain that cannot be built is skipped and named,
    never fatal. Exit 0 = export written (even with exclusions/omissions); exit 1 =
    nothing written (controller unreachable — any prior export is untouched).
    """

    setup_logging()
    err = Console(stderr=True)
    config = _load_web_config(scope or default_config_path(), err)
    assert config.uisp is not None  # _load_web_config guarantees

    import json as json_module

    from obi.core.audit import AuditTrail
    from obi.core.drivers.uisp import UispError
    from obi.core.engine import web_export
    from obi.core.serialize import ar_geometry_to_dict

    out_path = (out or config.web.export_path).expanduser()
    try:
        # 066: the source-wiring + Siklu-merge + ar_export composition lives in core.web_export now;
        # the CLI keeps the audit scope, the error message, and the rendering below.
        with _audit_capture(config, "cli:web-export"):
            geometry = web_export(config, out_path=out_path, audit=AuditTrail(config.audit_path))
    except UispError as exc:
        err.print(f"[red]web export failed:[/red] {exc.detail} — previous export untouched.")
        raise typer.Exit(code=1) from exc

    # 025 FR-002: links exported without Fresnel geometry are named, never silent.
    no_zone = [link.name for link in geometry.links if link.fresnel_radius_m is None]
    # 031 FR-006: every marginal/obstructed verdict is reported with margin + location.
    flagged = [
        link
        for link in geometry.links
        if link.clearance is not None and link.clearance.verdict.value != "clear"
    ]

    if json_output:
        payload = ar_geometry_to_dict(geometry)
        summary = {
            "export_path": str(out_path),
            "generated_at": payload["generated_at"],
            "sites": len(geometry.sites),
            "endpoints": len(geometry.endpoints),
            "links": geometry.health_counts,
            "excluded": payload["excluded"],
            "degraded": payload["degraded"],
            "site_anchored": payload["site_anchored"],
            "tower_placed": payload["tower_placed"],
            "tower_unmatched": payload["tower_unmatched"],
            "no_zone": no_zone,
            "clearance_flagged": [
                {
                    "link": link.name,
                    "verdict": link.clearance.verdict.value,  # type: ignore[union-attr]
                    "margin_m": link.clearance.margin_m,  # type: ignore[union-attr]
                    "at_fraction": link.clearance.at_fraction,  # type: ignore[union-attr]
                }
                for link in flagged
            ],
            "terrain_omitted": payload["terrain_omitted"],
        }
        sys.stdout.write(json_module.dumps(summary, indent=2) + "\n")
        return

    console = Console()
    counts = geometry.health_counts
    health_bits = ", ".join(f"{counts[h.value]} {h.value}" for h in LinkHealth if counts[h.value])
    console.print(
        f"exported {counts['total']} links ({health_bits or 'none'}) across "
        f"{len(geometry.endpoints)} radios / {len(geometry.sites)} sites → {out_path}"
    )
    if geometry.excluded:
        table = Table(title="Excluded links", title_justify="left")
        table.add_column("Link", overflow="fold")
        table.add_column("Reason", overflow="fold")
        for item in geometry.excluded:
            table.add_row(item.link, item.reason)
        console.print(table)
    if geometry.degraded:
        console.print(
            "[yellow]degraded ground elevation (USGS unreachable, default used):[/yellow] "
            + ", ".join(geometry.degraded)
        )
    if geometry.site_anchored:
        # 079: endpoints relocated from a strayed device pin to their site pin — named, never
        # silent (same discipline as the excluded table above).
        table = Table(title="Site-anchored endpoints (079)", title_justify="left")
        table.add_column("Device", overflow="fold")
        table.add_column("Site", overflow="fold")
        table.add_column("Moved", justify="right")
        table.add_column("Device pin")
        table.add_column("Site pin")
        for anchor in geometry.site_anchored:
            table.add_row(
                anchor.device,
                anchor.site,
                f"{anchor.distance_m:.1f} m",
                f"{anchor.device_latitude:.6f}, {anchor.device_longitude:.6f}",
                f"{anchor.site_latitude:.6f}, {anchor.site_longitude:.6f}",
            )
        console.print(table)
    if geometry.tower_placed:
        # 097: endpoints placed on their site's declared tower — named, never silent
        # (sibling of the 079 table above).
        table = Table(title="Tower-placed endpoints (097)", title_justify="left")
        table.add_column("Device", overflow="fold")
        table.add_column("Site", overflow="fold")
        table.add_column("Tower", justify="right")
        table.add_column("Moved", justify="right")
        table.add_column("Height AMSL", justify="right")
        for placement in geometry.tower_placed:
            table.add_row(
                placement.device,
                placement.site,
                f"{placement.tower_height_m:.2f} m",
                f"{placement.distance_m:.1f} m",
                f"{placement.height_amsl_m:.2f} m",
            )
        console.print(table)
    if geometry.tower_unmatched:
        console.print(
            "[yellow]tower table names unmatched sites (typo or renamed?):[/yellow] "
            + ", ".join(geometry.tower_unmatched)
        )
    if no_zone:
        console.print(
            "[yellow]no Fresnel zone (no controller channel and unrecognized band):[/yellow] "
            + ", ".join(no_zone)
        )
    if flagged:
        table = Table(title="Terrain clearance (031)", title_justify="left")
        table.add_column("Link", overflow="fold")
        table.add_column("Verdict")
        table.add_column("Worst margin", justify="right")
        table.add_column("At", justify="right")
        for link in flagged:
            clearance = link.clearance
            assert clearance is not None  # flagged selection guarantees
            table.add_row(
                link.name,
                f"[red]{clearance.verdict.value}[/red]"
                if clearance.verdict.value == "obstructed"
                else f"[yellow]{clearance.verdict.value}[/yellow]",
                f"{clearance.margin_m:.1f} m",
                f"{clearance.at_fraction:.0%}",
            )
        console.print(table)
    if geometry.terrain_omitted:
        console.print(
            "[yellow]terrain omitted (data unavailable — see reasons):[/yellow] "
            + "; ".join(f"{o.item}: {o.reason}" for o in geometry.terrain_omitted)
        )
    if not geometry.links:
        console.print(
            "[yellow]no links exported[/yellow] — no wireless data-link had BOTH endpoints "
            "named per the *-ptp-* convention (see the exclusions above, if any)."
        )


@web_app.command("snapshot")
def web_snapshot_cmd(
    scope: Annotated[
        Path | None,
        typer.Option("--scope", help="Path to the config file (default: XDG config)."),
    ] = None,
    only: Annotated[
        str | None,
        typer.Option(
            "--only",
            help=(
                "Comma-separated subset of: status, uisp, uisp-links, siklu, unifi, "
                "netbox-drift, ospf."
            ),
        ),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit a machine-readable summary instead of a table.")
    ] = False,
) -> None:
    """Snapshot the device/controller tools for the web boards (feature 040; read-only).

    Writes one atomic JSON per configured tool into the web state dir's snapshots/
    directory — the boards (UISP/STAT/SIK/UNIFI/NBX) poll these files, keeping the web
    server device-blind. A tool builds iff its config section exists; --only narrows.
    A failed tool (controller down) keeps its previous snapshot and is named in the
    summary; per-device unreachability is data, not failure. One audit record per run.
    Exit 0 unless every attempted tool failed. Run it on a cron (stagger away from the
    other collectors — see the quickstart).
    """

    setup_logging()
    err = Console(stderr=True)
    config_path = scope or default_config_path()
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        err.print(f"[red]config error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc
    warning = perms_warning(config_path)
    if warning:
        err.print(f"[yellow]warning:[/yellow] {warning}")

    import json as json_module

    from obi.core import snapshot as snap
    from obi.core.audit import AuditTrail
    from obi.core.config import default_web_dir
    from obi.core.models import AuditOutcome, AuditRecord, _utcnow

    wanted = list(snap.SNAPSHOT_TOOL_NAMES)
    if only is not None:
        wanted = [name.strip() for name in only.split(",") if name.strip()]
        unknown = [name for name in wanted if name not in snap.SNAPSHOT_TOOL_NAMES]
        if unknown:
            err.print(
                f"[red]unknown tool(s):[/red] {', '.join(unknown)} — "
                f"choose from {', '.join(snap.SNAPSHOT_TOOL_NAMES)}"
            )
            raise typer.Exit(code=2)

    audit = AuditTrail(config.audit_path)
    out_dir = default_web_dir() / "snapshots"
    reason = "operator/cron ran `obi web snapshot`"

    # 066: the per-tool driver wiring + attempt loop lives in core.snapshot.run_all now; the CLI
    # keeps the audit scope, the run-level summary record below (outside the scope, as before),
    # and the rendering.
    with _audit_capture(config, "cli:web-snapshot"):
        outcomes = snap.run_all(config, wanted=wanted, audit=audit, out_dir=out_dir, reason=reason)

    audit.record(
        AuditRecord(
            timestamp=_utcnow(),
            action="web_snapshot",
            target=str(out_dir),
            reason=reason,
            outcome=AuditOutcome.OK
            if any(o == "built" for o in outcomes.values())
            else AuditOutcome.UNREACHABLE,
            detail="; ".join(f"{name}={outcome}" for name, outcome in outcomes.items()),
        )
    )

    if json_output:
        sys.stdout.write(
            json_module.dumps({"snapshots_dir": str(out_dir), "tools": outcomes}) + "\n"
        )
    else:
        console = Console()
        for name in wanted:
            outcome = outcomes.get(name, "skipped (unconfigured)")
            style = (
                "green"
                if outcome == "built"
                else ("yellow" if outcome.startswith("skipped") else "red")
            )
            console.print(f"  {name:<14} [{style}]{outcome}[/{style}]")
    attempted = [o for o in outcomes.values() if not o.startswith("skipped")]
    if attempted and all(o.startswith("failed") for o in attempted):
        raise typer.Exit(code=1)


@web_app.command("serve")
def web_serve_cmd(
    scope: Annotated[
        Path | None,
        typer.Option("--scope", help="Path to the config file (default: XDG config)."),
    ] = None,
    port: Annotated[
        int | None, typer.Option("--port", help="HTTPS port (default: \\[web] port, 8443).")
    ] = None,
    bind: Annotated[
        str | None, typer.Option("--bind", help="Bind address (default: \\[web] bind, 0.0.0.0).")
    ] = None,
    export: Annotated[
        Path | None,
        typer.Option("--export", help="Geometry file to serve (default: \\[web] export_path)."),
    ] = None,
    demo: Annotated[
        bool,
        typer.Option("--demo", help="Preview a built static demo bundle (136) on localhost."),
    ] = False,
    bundle: Annotated[
        Path | None, typer.Option("--bundle", help="--demo: bundle dir (default [demo.export]).")
    ] = None,
    base: Annotated[
        str, typer.Option("--base", help="--demo: the sub-path to serve under.")
    ] = "/obi/",
) -> None:
    """Serve both viewer versions + current export on the LAN, with live tools + voice.

    One origin: flat browsers get the 2D map, WebXR headsets get the immersive 3D viewer
    (auto-selected, manually switchable). The workspace also carries the PING/TRACE tools (048;
    run a live ping/traceroute FROM a chosen router, read-only) and — mounted by default (048;
    the old `--voice` flag was removed) — the 024 talk engine, so any browser or the headset can
    hold-to-talk to obi (features 032/033; local STT/TTS, read-only tools, one shared
    certificate). Voice can be turned off with `[web] voice = false`; a voice build failure
    degrades to a static+tools server rather than aborting startup. Viewer data stays fresh via
    the `obi web export` cron (both versions re-read it every 5 minutes). Ctrl-C stops it.
    """

    setup_logging()
    err = Console(stderr=True)
    if demo:
        _web_serve_demo(scope, bundle, base, port if port is not None else 8765)
        return
    config = _load_web_config(scope or default_config_path(), err)

    import json as json_module

    from obi.core.audit import AuditTrail
    from obi.core.config import default_web_dir
    from obi.core.models import AuditOutcome, AuditRecord, _utcnow
    from obi.web.server import create_server, urls_for

    export_path = (export or config.web.export_path).expanduser()
    serve_bind = bind if bind is not None else config.web.bind
    serve_port = port if port is not None else config.web.port
    # 048: voice is on by default (the --voice flag was removed); [web] voice = false opts out.
    voice_enabled = config.web.voice

    console = Console()
    voice_web_app = None
    if voice_enabled:
        try:
            voice_web_app = _build_voice_web_app(config, console, err, label="web voice")
        except Exception as exc:  # noqa: BLE001 - voice must never abort a static+tools serve (048)
            voice_web_app = None
            err.print(
                f"[yellow]voice unavailable:[/yellow] {exc} — serving the workspace, viewers, "
                "and net tools without it (set `[web] voice = false` to silence this)."
            )
    if export_path.exists():
        try:
            generated = json_module.loads(export_path.read_text(encoding="utf-8")).get(
                "generated_at"
            )
            console.print(f"serving export {export_path} (generated {generated})")
        except (OSError, ValueError):
            console.print(f"[yellow]warning:[/yellow] {export_path} is unreadable/invalid JSON")
    else:
        console.print(
            f"[yellow]no export yet[/yellow] ({export_path} missing) — the viewer will say so; "
            "run `obi web export`."
        )

    try:
        server = create_server(
            bind=serve_bind,
            port=serve_port,
            export_path=export_path,
            state_dir=default_web_dir(),
            voice_app=voice_web_app,
            voice_max_seconds=config.voice.web_max_seconds,
            # 038/040: the local-data routes — read the 013/014 stores + roster, never a device.
            logs_settings=config.logs,
            watch_dir=config.watch.watch_dir,
            inventory_path=config.inventory_path,
            metrics_settings=config.snmp.history if config.snmp is not None else None,
            configs_app_config=config,
            # 042: the AUD board's local-data route over the audit event store.
            audit_settings=config.audit,
            # 088: the RADIO board's local-data route over the radio transcript store.
            radio_settings=config.radio,
            dash_settings=config.dash,
        )
    except (RuntimeError, OSError) as exc:
        err.print(f"[red]web serve failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    AuditTrail(config.audit_path).record(
        AuditRecord(
            timestamp=_utcnow(),
            action="web_serve",
            target=f"{serve_bind}:{server.server_address[1]}",
            reason="operator-initiated web viewer serve",
            outcome=AuditOutcome.OK,
            # 048: record whether voice ACTUALLY mounted (it can be on-by-default but fail to
            # build), not just the intent — the disclosure below gates on the same truth.
            params={"export_path": str(export_path), "voice": voice_web_app is not None},
        )
    )
    if voice_web_app is not None:
        console.print(_VOICE_CLOUD_DISCLOSURE)
        console.print(
            "voice is mounted on this origin — hold grip in the headset, or hold the talk "
            "button on the 2D page."
        )
    console.print(
        "both versions re-read the export every 5 min — keep it fresh with an "
        "`obi web export` cron (see the quickstart)."
    )
    console.print("open on any LAN browser (accept the certificate warning once per device):")
    for url in urls_for(server, serve_bind):
        console.print(f"  [bold]{url}[/bold]")
    console.print("Ctrl-C to stop.")
    # 081: launchd `bootout` sends SIGTERM; shut down gracefully so a supervised stop is clean.
    # server.shutdown() must run OFF the serve_forever() thread — calling it inline deadlocks.
    import contextlib as _contextlib
    import signal as _signal
    import threading as _threading

    def _on_sigterm(_signum: int, _frame: object) -> None:
        _threading.Thread(target=server.shutdown, daemon=True).start()

    # not the main thread (e.g. under a test harness) ⇒ can't install a handler; harmless
    with _contextlib.suppress(ValueError):
        _signal.signal(_signal.SIGTERM, _on_sigterm)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console.print("stopped.")
    finally:
        server.server_close()


def _web_serve_demo(scope: Path | None, bundle: Path | None, base: str, port: int) -> None:
    """`obi web serve --demo`: the static bundle on localhost, as a static host serves it."""

    from obi.core.demo import DemoError
    from obi.core.demo.export import load_export_profile
    from obi.web.server import create_demo_server

    err = Console(stderr=True)
    if bundle is None:
        _config, raw, _profile, path = _demo_setup(scope)
        try:
            bundle = load_export_profile(raw, config_path=path).bundle_dir
        except DemoError as exc:
            err.print(f"[red]{type(exc).__name__}:[/red] {escape(str(exc))}")
            raise typer.Exit(code=exc.exit_code) from exc
    if not (bundle / "index.html").is_file():
        err.print(f"[red]no demo bundle at {escape(str(bundle))}[/red] — run `obi demo export`.")
        raise typer.Exit(code=2)
    server = create_demo_server(bundle, base=base, port=port)
    prefix = "/" + base.strip("/") + "/" if base.strip("/") else "/"
    Console().print(f"demo preview: http://127.0.0.1:{server.server_address[1]}{prefix} "
                    "(localhost only; Ctrl-C stops)")  # fmt: skip
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


@web_app.command("start")
def web_start(
    scope: Annotated[Path | None, _SCOPE_OPT] = None,
    login: Annotated[bool | None, _LOGIN_OPT] = None,
    exec_override: Annotated[Path | None, _EXEC_OPT] = None,
) -> None:
    """Install + start the web server as a supervised launchd agent (restart on crash, at login)."""

    _svc_start("web", scope, login, exec_override)


@web_app.command("stop")
def web_stop(scope: Annotated[Path | None, _SCOPE_OPT] = None) -> None:
    """Stop the supervised web server (bootout — stays stopped until `obi web start`)."""

    _svc_stop("web", scope)


@web_app.command("restart")
def web_restart(scope: Annotated[Path | None, _SCOPE_OPT] = None) -> None:
    """Restart the supervised web server in place (kickstart -k)."""

    _svc_restart("web", scope)


@web_app.command("status")
def web_status(scope: Annotated[Path | None, _SCOPE_OPT] = None) -> None:
    """Show the web server's launchd state, /healthz liveness, and cron-collector freshness."""

    _svc_status("web", scope)


@web_app.command("logs")
def web_logs(
    lines: Annotated[int, _LINES_OPT] = 40,
    follow: Annotated[bool, _FOLLOW_OPT] = False,
) -> None:
    """Tail the web server's log (`~/.local/state/obi/web-serve.log`)."""

    _svc_logs("web", lines, follow)


metrics_app = typer.Typer(
    add_completion=False,
    help=(
        "Historical / time-series SNMP metrics (feature 021). `obi metrics ingest` polls the "
        "gateways over SNMP and persists their readings to a local store separate from the logs; "
        "`obi metrics history` shows a metric over a past window (counters as rates); "
        "`obi metrics status` reports coverage. Requires an \\[snmp] section plus an "
        "\\[snmp.history] subsection (opt-in) — see README.md § SNMP. "
        "`obi metrics live` is the on-demand spot-read."
    ),
)
app.add_typer(metrics_app, name="metrics")


@metrics_app.command("live")
def metrics_live(
    target: Annotated[
        str, typer.Option("--target", help="Device address or identity, or 'all'.")
    ] = "all",
    profile: Annotated[
        str, typer.Option("--profile", help="Metric group: 'system' or 'interfaces'.")
    ] = "system",
    source: Annotated[
        str, typer.Option("--source", help="Which roster to query: 'mikrotik', 'netbox', 'all'.")
    ] = "all",
    timeout: Annotated[
        float | None, typer.Option("--timeout", help="Per-target SNMP timeout in seconds.")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON instead of a table.")
    ] = False,
    scope: Annotated[
        Path | None,
        typer.Option("--scope", help="Path to the config/scope file (default: XDG config)."),
    ] = None,
) -> None:
    """Read live SNMP metrics for the devices obi already knows (read-only).

    Targets come from the Mikrotik roster (run ``obi routeros discover`` first) and/or
    NetBox-documented hosts. Needs an \\[snmp] config section (community for v2c, or v3
    credentials). A device that is
    unreachable, mis-credentialed, or times out is reported with an explicit per-device status; the
    sweep never aborts on one bad device.
    """

    setup_logging()
    logger = logging.getLogger("obi")
    err = Console(stderr=True)
    config_path = scope or default_config_path()

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        err.print(f"[red]config error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc

    warning = perms_warning(config_path)
    if warning:
        logger.warning("insecure config permissions", extra={"detail": warning})
        err.print(f"[yellow]warning:[/yellow] {warning}")

    if config.snmp is None:
        err.print(
            "[red]snmp not configured:[/red] add an \\[snmp] section (community for v2c, or v3 "
            f"credentials) to {config_path} — see README.md § SNMP."
        )
        raise typer.Exit(code=2)

    from obi.core.drivers.snmp_profiles import PROFILES

    if profile not in PROFILES:
        err.print(f"[red]unknown profile:[/red] {profile!r} — choose one of {', '.join(PROFILES)}.")
        raise typer.Exit(code=2)

    if timeout is not None:
        config.snmp.timeout = timeout

    from obi.core.audit import AuditTrail
    from obi.core.drivers.netbox import NetboxSource
    from obi.core.drivers.snmp import SnmpMetricsSource
    from obi.core.engine import snmp_metrics
    from obi.core.serialize import metric_reports_to_json

    audit = AuditTrail(config.audit_path)
    netbox_source = NetboxSource(config.netbox) if config.netbox is not None else None
    with _audit_capture(config, "cli:metrics-live"):
        reports = snmp_metrics(
            SnmpMetricsSource(config.snmp),
            config=config,
            target=target,
            profile_name=profile,
            source=source,
            audit=audit,
            netbox_source=netbox_source,
        )

    if json_output:
        sys.stdout.write(metric_reports_to_json(reports) + "\n")
        return

    console = Console()
    if not reports:
        console.print(
            "No known devices to query over SNMP — run `obi routeros discover` (Mikrotik) and/or "
            "configure \\[netbox], then retry."
        )
        return
    _render_snmp_reports(reports, profile, console)


def _fmt_metric_value(value: float) -> str:
    """Compact number for a rendered series point (thousands-separated when large)."""

    if abs(value) >= 1000:
        return f"{value:,.0f}"
    return f"{value:.2f}".rstrip("0").rstrip(".")


_SPARK = "▁▂▃▄▅▆▇█"


def _sparkline(values: list[float | None]) -> str:
    """A unicode sparkline over the bucket averages; a gap bucket renders as a blank."""

    nums = [v for v in values if v is not None]
    if not nums:
        return ""
    lo, hi = min(nums), max(nums)
    span = (hi - lo) or 1.0
    cells = []
    for v in values:
        if v is None:
            cells.append(" ")
        else:
            cells.append(_SPARK[min(len(_SPARK) - 1, int((v - lo) / span * (len(_SPARK) - 1)))])
    return "".join(cells)


def _render_metrics_history(
    target: str, metric: str, series_list: list[Series], console: Console
) -> None:
    from obi.core.metricstore import Series

    if not series_list:
        console.print(
            f"no history for {target} / {metric} in that window — check `obi metrics status`, "
            "or widen --since."
        )
        return
    for series in series_list:
        assert isinstance(series, Series)
        pts = [b.avg for b in series.buckets if b.avg is not None]
        label = f"{series.device} · {series.ifname or metric}"
        kind = "rate" if series.is_rate else "value"
        if not pts:
            console.print(f"{label} [{kind}]: no data points in window")
            continue
        unit = f" {series.unit}" if series.unit else ""
        summary = (
            f"min {_fmt_metric_value(min(pts))} · avg {_fmt_metric_value(sum(pts) / len(pts))} · "
            f"max {_fmt_metric_value(max(pts))}{unit}"
        )
        console.print(f"{label}  [{kind}]  {summary}")
        spark = _sparkline([b.avg for b in series.buckets])
        if spark.strip():
            console.print(f"  {spark}")


@metrics_app.command("ingest")
def metrics_ingest_cmd(
    scope: Annotated[
        Path | None, typer.Option("--scope", help="Path to the config file (default: XDG config).")
    ] = None,
    db: Annotated[
        Path | None, typer.Option("--db", help="Override the metrics store path.")
    ] = None,
    targets: Annotated[
        str | None, typer.Option("--targets", help="Override the identity name-filter (e.g. -gw).")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit the cycle summary as JSON instead of a table.")
    ] = False,
) -> None:
    """Poll the matched gateways over SNMP and persist their readings (one cycle; cron-friendly)."""

    setup_logging()
    err = Console(stderr=True)
    config, history = _load_metrics_config(scope or default_config_path(), err)
    if db is not None:
        history.db_path = db.expanduser()
    if targets is not None:
        history.targets = targets

    import json

    from obi.core.audit import AuditTrail
    from obi.core.engine import metrics_ingest

    with _audit_capture(config, "cli:metrics-ingest"):
        report = metrics_ingest(
            config, audit=AuditTrail(config.audit_path), reason="operator-initiated metrics ingest"
        )

    if json_output:
        sys.stdout.write(
            json.dumps(
                {
                    "targets_matched": report.targets_matched,
                    "devices_reachable": report.devices_reachable,
                    "devices_failed": {
                        o: c
                        for o, c in report.outcomes.items()
                        if o not in ("reachable", "no_such_object")
                    },
                    "readings_stored": report.readings_stored,
                    "readings_pruned": report.readings_pruned,
                    "failures": report.failures,
                    "profiles": report.profiles,
                    "db_path": report.db_path,
                }
            )
            + "\n"
        )
        return

    console = Console()
    if report.detail and report.targets_matched == 0:
        console.print(f"[yellow]metrics:[/yellow] {report.detail}")
        return
    console.print(
        f"metrics: {report.readings_stored} stored, {report.readings_pruned} pruned; "
        f"{report.devices_reachable}/{report.targets_matched} device(s) reachable "
        f"[{', '.join(report.profiles)}]"
    )
    for failure in report.failures:  # per device+profile — catches a profile lost silently
        console.print(f"  {failure}")


@metrics_app.command("history")
def metrics_history_cmd(
    target: Annotated[str, typer.Option("--target", help="Device identity, e.g. core-gw-1.")],
    metric: Annotated[
        str, typer.Option("--metric", help="Metric, e.g. ifHCInOctets, ifInErrors, cpuLoad.")
    ],
    interface: Annotated[
        str | None,
        typer.Option("--interface", help="One port for a per-interface metric (e.g. ether1)."),
    ] = None,
    since: Annotated[
        str, typer.Option("--since", help="Lookback (1h/2d) or ISO date/time.")
    ] = "24h",
    until: Annotated[
        str | None, typer.Option("--until", help="End of window (relative or ISO); default now.")
    ] = None,
    db: Annotated[
        Path | None, typer.Option("--db", help="Override the metrics store path.")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON instead of a summary.")
    ] = False,
    scope: Annotated[
        Path | None, typer.Option("--scope", help="Path to the config file (default: XDG config).")
    ] = None,
) -> None:
    """Show a metric's history for a device over a window (counters as reset/gap-aware rates)."""

    setup_logging()
    err = Console(stderr=True)
    config, history = _load_metrics_config(scope or default_config_path(), err)
    db_path = (db or history.db_path).expanduser()

    import json
    from datetime import UTC, datetime, timedelta

    from obi.core import logstore, metricstore

    if not db_path.exists():
        Console().print("no metrics collected yet — run `obi metrics ingest`.")
        return

    now = datetime.now(UTC)
    since_dt = logstore.parse_when(since, UTC) or (now - timedelta(hours=24))
    until_dt = (logstore.parse_when(until, UTC) if until else now) or now
    since_us = int(since_dt.timestamp() * 1_000_000)
    until_us = int(until_dt.timestamp() * 1_000_000)

    conn = metricstore.connect_readonly(db_path)
    try:
        series_list = metricstore.query_series(
            conn,
            device=target,
            metric=metric,
            since_us=since_us,
            until_us=until_us,
            ifname=interface,
            buckets=history.query_buckets,
        )
    finally:
        conn.close()

    if json_output:
        from obi.core.serialize import metrics_history_to_dict

        bucket_seconds = max(1, (until_us - since_us) // max(1, history.query_buckets) // 1_000_000)
        payload = metrics_history_to_dict(
            target,
            metric,
            since_dt.isoformat(),
            until_dt.isoformat(),
            bucket_seconds,
            series_list,
        )
        sys.stdout.write(json.dumps(payload) + "\n")
        return

    _render_metrics_history(target, metric, series_list, Console())


@metrics_app.command("status")
def metrics_status_cmd(
    db: Annotated[
        Path | None, typer.Option("--db", help="Override the metrics store path.")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON instead of a table.")
    ] = False,
    scope: Annotated[
        Path | None, typer.Option("--scope", help="Path to the config file (default: XDG config).")
    ] = None,
) -> None:
    """Report metrics-collection coverage: readings, devices, oldest/newest, retention."""

    setup_logging()
    err = Console(stderr=True)
    config, history = _load_metrics_config(scope or default_config_path(), err)
    db_path = (db or history.db_path).expanduser()

    import json
    from datetime import UTC, datetime

    from obi.core import metricstore

    if not db_path.exists():
        if json_output:
            sys.stdout.write(
                json.dumps(
                    {
                        "db_path": str(db_path),
                        "total_readings": 0,
                        "devices": [],
                        "retain_days": history.retain_days,
                        "oldest": None,
                        "newest": None,
                    }
                )
                + "\n"
            )
            return
        Console().print("no metrics collected yet — run `obi metrics ingest`.")
        return

    conn = metricstore.connect_readonly(db_path)
    try:
        cov = metricstore.coverage(conn)
    finally:
        conn.close()

    def _iso(us: int | None) -> str | None:
        if us is None:
            return None
        return datetime.fromtimestamp(us / 1_000_000, tz=UTC).isoformat()

    if json_output:
        from obi.core.serialize import metrics_coverage_to_dict

        sys.stdout.write(
            json.dumps(
                metrics_coverage_to_dict(cov, db_path=db_path, retain_days=history.retain_days)
            )
            + "\n"
        )
        return

    if cov.total_readings == 0:
        Console().print("no metrics collected yet — run `obi metrics ingest`.")
        return
    table = Table(title="obi metrics coverage", title_justify="left")
    for column in ("Device", "Readings", "Newest"):
        table.add_column(column, overflow="fold")
    for d in cov.devices:
        newest = _iso(d.newest_us) or "—"
        table.add_row(d.device, str(d.readings), newest)
    console = Console()
    console.print(table)
    console.print(
        f"{cov.total_readings} readings · {len(cov.devices)} device(s) · "
        f"{_iso(cov.oldest_us)} → {_iso(cov.newest_us)} · retain {history.retain_days}d"
    )


health_app = typer.Typer(
    add_completion=False,
    help=(
        "Fleet hardware health (feature 096): temperatures, PSU/fan state, CPU/memory, and SFP "
        "optics for the SNMP-polled RouterOS gateways, plus the Linux collector VMs. "
        "`obi health status` shows the newest readings worst-first (stale devices float up); "
        "readings live in the feature-021 metrics store (`obi metrics ingest` collects the "
        "RouterOS side). Read-only. See README.md § Watching device health."
    ),
)
app.add_typer(health_app, name="health")

_NO_HEALTH_MSG = (
    "no health readings yet — run `obi metrics ingest` (RouterOS gateways) and "
    "`obi health probe` (collector VMs)."
)


def _health_age(now_us: int, ts_us: int) -> str:
    seconds = max(0, (now_us - ts_us) // 1_000_000)
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


@health_app.command("status")
def health_status_cmd(
    device: Annotated[
        str | None,
        typer.Option("--device", help="Show every reading for one device instead."),
    ] = None,
    db: Annotated[
        Path | None, typer.Option("--db", help="Override the metrics store path.")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON instead of a table.")
    ] = False,
    scope: Annotated[
        Path | None, typer.Option("--scope", help="Path to the config file (default: XDG config).")
    ] = None,
) -> None:
    """Show the fleet's newest hardware-health readings, worst-first."""

    setup_logging()
    err = Console(stderr=True)
    config, resolved_db = _load_health_config(scope or default_config_path(), err)
    db_path = (db or resolved_db).expanduser()

    import json
    from datetime import UTC, datetime

    from obi.core import health as health_core
    from obi.core import metricstore
    from obi.core.serialize import health_status_to_dict

    now_us = int(datetime.now(tz=UTC).timestamp() * 1_000_000)

    # Thresholds come from [watch.health] when configured, so this view annotates exactly
    # what the watch pass would breach; otherwise the shipped defaults apply.
    thresholds = health_core.thresholds_from_settings(config.watch.health)

    devices: list[health_core.DeviceHealth] = []
    if db_path.exists():
        conn = metricstore.connect_readonly(db_path)
        try:
            devices = health_core.fleet_status(conn, now_us=now_us, device=device)
        finally:
            conn.close()

    breaches_by_device = {d.device: health_core.evaluate_device(d, thresholds)[0] for d in devices}
    # Worst-first (US2 refinement): breaching devices first, then stale, then by name.
    devices.sort(key=lambda d: (not breaches_by_device.get(d.device), not d.stale, d.device))

    if json_output:
        payload = health_status_to_dict(
            devices,
            generated_at_us=now_us,
            db_path=db_path,
            breaches_by_device=breaches_by_device,
        )
        sys.stdout.write(json.dumps(payload) + "\n")
        return

    console = Console()
    if not devices:
        console.print(_NO_HEALTH_MSG)
        return

    def _fmt(value: float | None, suffix: str = "") -> str:
        return "—" if value is None else f"{value:g}{suffix}"

    if device is not None:
        detail = Table(title=f"obi health · {device}", title_justify="left")
        for column in ("Reading", "Instance", "Value", "Age"):
            detail.add_column(column, overflow="fold")
        for d in devices:
            for r in d.readings:
                detail.add_row(
                    r.metric,
                    r.label or r.instance or "—",
                    f"{r.value:g} {r.unit}".strip(),
                    _health_age(now_us, r.ts_us),
                )
        console.print(detail)
        return

    table = Table(title="obi fleet health", title_justify="left")
    for column in (
        "Device",
        "Kind",
        "Temp",
        "CPU",
        "Mem",
        "Rx",
        "Disk",
        "States",
        "Breaches",
        "Age",
    ):
        table.add_column(column, overflow="fold")
    for d in devices:
        s = health_core.summarize(d)
        has_states = any(
            r.metric.startswith("health:") and r.metric.endswith("-state") for r in d.readings
        )
        states = ", ".join(s.state_alerts) if s.state_alerts else ("ok" if has_states else "—")
        breaches = breaches_by_device.get(d.device, [])
        breach_text = "—"
        if breaches:
            named = [b.label or b.metric for b in breaches[:3]]
            breach_text = (
                "[red]" + ", ".join(named) + (", …" if len(breaches) > 3 else "") + "[/red]"
            )
        age = _health_age(now_us, d.newest_us) + (" [red]STALE[/red]" if d.stale else "")
        table.add_row(
            d.device,
            d.kind,
            _fmt(s.temperature_c, "°C"),
            _fmt(s.cpu_percent, "%"),
            _fmt(s.memory_percent, "%"),
            _fmt(s.rx_power_min_dbm, "dBm"),
            _fmt(s.disk_max_percent, "%"),
            states,
            breach_text,
            age,
        )
    console.print(table)
    stale = sum(1 for d in devices if d.stale)
    breaching = sum(1 for b in breaches_by_device.values() if b)
    console.print(
        f"{len(devices)} device(s) · {breaching} breaching · {stale} stale · store {db_path}"
    )


@health_app.command("probe")
def health_probe_cmd(
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON instead of a summary.")
    ] = False,
    scope: Annotated[
        Path | None, typer.Option("--scope", help="Path to the config file (default: XDG config).")
    ] = None,
) -> None:
    """Probe the Linux collector VMs read-only over SSH and store their health readings."""

    setup_logging()
    err = Console(stderr=True)
    config, _ = _load_health_config(scope or default_config_path(), err)
    if config.health_linux is None:
        err.print(
            "[red]linux health probe not configured:[/red] add a \\[health.linux] section with "
            "a hosts list — see README.md § Watching device health."
        )
        raise typer.Exit(code=2)

    import json

    from obi.core.audit import AuditTrail
    from obi.core.engine import health_probe

    with _audit_capture(config, "cli:health-probe"):
        report = health_probe(config, audit=AuditTrail(config.audit_path))

    if json_output:
        payload = {
            "hosts_probed": report.hosts_probed,
            "hosts_ok": report.hosts_ok,
            "readings_stored": report.readings_stored,
            "readings_pruned": report.readings_pruned,
            "outcomes": report.outcomes,
            "failures": report.failures,
            "db_path": report.db_path,
        }
        sys.stdout.write(json.dumps(payload) + "\n")
        return
    console = Console()
    console.print(
        f"probed {report.hosts_probed} host(s) · {report.hosts_ok} ok · "
        f"{report.readings_stored} readings stored · {report.readings_pruned} pruned"
    )
    for failure in report.failures:
        console.print(f"  [yellow]{failure}[/yellow]")


configs_app = typer.Typer(
    add_completion=False,
    help=(
        "RouterOS config backup into a local git repo (feature 026). `obi configs ingest` captures "
        "each roster device's redacted /export over SSH and commits the changed ones; "
        "`obi configs status` reports coverage; `obi configs show/history/diff` read the stored "
        "live + historical config. Read-only — obi never writes to a device. Requires a "
        "\\[configs] section (opt-in) and the account's RouterOS 'ssh' policy — see "
        "README.md § RouterOS config backup."
    ),
)
app.add_typer(configs_app, name="configs")


_CAPTURE_STYLE = {
    "captured_changed": "green",
    "captured_unchanged": "dim",
    "unreachable": "red",
    "timed_out": "red",
    "auth_failed": "red",
    "permission_denied": "yellow",
    "empty_export": "yellow",
}


def _fmt_ago(when: datetime | None) -> str:
    """A compact 'time ago' for a captured-at timestamp (or '—' when never captured)."""

    from datetime import UTC
    from datetime import datetime as _dt

    if when is None:
        return "—"
    delta = _dt.now(UTC) - when
    secs = int(delta.total_seconds())
    if secs < 0:
        return when.date().isoformat()
    for size, label in ((86400, "d"), (3600, "h"), (60, "m")):
        if secs >= size:
            return f"{secs // size}{label} ago"
    return "just now"


@configs_app.command("ingest")
def configs_ingest_cmd(
    scope: Annotated[
        Path | None, typer.Option("--scope", help="Path to the config file (default: XDG config).")
    ] = None,
    target: Annotated[
        str | None,
        typer.Option("--target", help="Override the [configs].targets identity filter ('' = all)."),
    ] = None,
    reason: Annotated[
        str | None, typer.Option("--reason", help="Audit reason for this run.")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit the run summary as JSON instead of a table.")
    ] = False,
) -> None:
    """Capture the roster's configs over SSH and commit the changed ones (one cron-friendly run)."""

    setup_logging()
    err = Console(stderr=True)
    config, settings = _load_configs_config(scope or default_config_path(), err)
    if target is not None:
        settings.targets = target

    from obi.core.audit import AuditTrail
    from obi.core.engine import configs_ingest
    from obi.core.serialize import configs_ingest_to_json

    with _audit_capture(config, "cli:configs-ingest"):
        report = configs_ingest(
            config,
            audit=AuditTrail(config.audit_path),
            reason=reason or "operator-initiated config backup",
        )

    if json_output:
        sys.stdout.write(configs_ingest_to_json(report, indent=None) + "\n")
        return

    console = Console()
    if report.detail and report.targets_matched == 0:
        console.print(f"[yellow]configs:[/yellow] {escape(report.detail)}")
        return
    console.print(
        f"configs: {report.targets_matched} targeted · [green]{report.changed} changed[/green] · "
        f"{report.unchanged} unchanged · "
        f"{('[red]' + str(report.failed) + ' failed[/red]') if report.failed else '0 failed'}  "
        f"[dim]{escape(report.repo_path)}[/dim]"
    )
    if report.results:
        table = Table(show_header=True, header_style="bold")
        for column in ("Device", "Address", "Outcome", "Note"):
            table.add_column(column, overflow="fold")
        for r in report.results:
            style = _CAPTURE_STYLE.get(r.outcome.value, "")
            table.add_row(
                escape(r.identity),
                escape(r.address),
                f"[{style}]{r.outcome.value}[/{style}]" if style else r.outcome.value,
                escape(r.note or ""),
            )
        console.print(table)


@configs_app.command("status")
def configs_status_cmd(
    scope: Annotated[
        Path | None, typer.Option("--scope", help="Path to the config file (default: XDG config).")
    ] = None,
    stale_after: Annotated[
        str,
        typer.Option(
            "--stale-after",
            help=(
                "Flag a device whose last SUCCESSFUL capture is older than this (e.g. 6h, 1d). "
                "An unchanged config is not stale — staleness is capture health (085)."
            ),
        ),
    ] = "1d",
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit coverage as JSON instead of a table.")
    ] = False,
) -> None:
    """Report backup coverage: which roster devices are backed up, how fresh, and any orphans."""

    setup_logging()
    err = Console(stderr=True)
    config, _settings = _load_configs_config(scope or default_config_path(), err)

    from obi.core.audit import AuditTrail
    from obi.core.engine import config_status
    from obi.core.serialize import config_status_to_json

    report = config_status(config, stale_after=stale_after, audit=AuditTrail(config.audit_path))

    if json_output:
        sys.stdout.write(config_status_to_json(report, indent=None) + "\n")
        return

    console = Console()
    if not report.rows:
        console.print(
            "no devices — run `obi routeros discover` to build the roster, then "
            "`obi configs ingest`."
        )
        return
    table = Table(title="obi config backup coverage", title_justify="left")
    for column in ("Device", "Address", "Backup", "Versions", "Captured", "Last change", "Note"):
        table.add_column(column, overflow="fold")
    for row in report.rows:
        backup = "[green]yes[/green]" if row.has_backup else "[red]no[/red]"
        captured = _fmt_ago(row.last_captured_at)
        if row.stale:
            captured = f"[red]{captured}[/red]"  # capture health — red means obi can't read it
        table.add_row(
            escape(row.identity),
            escape(row.address or "—"),
            backup,
            str(row.versions),
            captured,
            _fmt_ago(row.last_changed_at),  # informational: settled ≠ stale (085)
            escape(row.note or ""),
        )
    console.print(table)
    t = report.totals
    console.print(
        f"{t['devices']} device(s) · {t['backed_up']} backed up · "
        f"{t['stale']} capture-stale (>{escape(report.stale_after)}) · "
        f"{t['orphaned']} orphaned · [dim]{escape(report.repo_path)}[/dim]"
    )


@configs_app.command("show")
def configs_show_cmd(
    device: Annotated[str, typer.Argument(help="Device identity or address.")],
    scope: Annotated[Path | None, typer.Option("--scope", help="Config file path.")] = None,
    at: Annotated[
        str, typer.Option("--at", help="Version as of: 'current', a git sha, ISO date, or '1w'.")
    ] = "current",
    contains: Annotated[
        str | None,
        typer.Option("--contains", help="Show only config lines matching this (e.g. firewall)."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit as JSON.")] = False,
) -> None:
    """Print a device's stored configuration (latest, or as of --at)."""

    setup_logging()
    err = Console(stderr=True)
    config, _settings = _load_configs_config(scope or default_config_path(), err)

    from obi.core.audit import AuditTrail
    from obi.core.engine import config_show
    from obi.core.serialize import config_show_to_json

    result = config_show(
        config, device, at=at, contains=contains, audit=AuditTrail(config.audit_path)
    )

    if json_output:
        sys.stdout.write(config_show_to_json(result, indent=None) + "\n")
        return
    if not result.found:
        err.print(f"[yellow]{escape(result.detail or 'no stored config')}[/yellow]")
        raise typer.Exit(code=2)
    ver = f" · RouterOS {result.router_version}" if result.router_version else ""
    when = result.captured_at.isoformat() if result.captured_at else "?"
    Console().print(f"[dim]# {escape(device)} — {result.at} · captured {when}{escape(ver)}[/dim]")
    # Config text is device-supplied and full of [ ] — write raw so Rich never interprets markup.
    sys.stdout.write(result.content if result.content.endswith("\n") else result.content + "\n")


@configs_app.command("history")
def configs_history_cmd(
    device: Annotated[str, typer.Argument(help="Device identity or address.")],
    scope: Annotated[Path | None, typer.Option("--scope", help="Config file path.")] = None,
    limit: Annotated[int, typer.Option("--limit", help="Max change points (newest first).")] = 20,
    json_output: Annotated[bool, typer.Option("--json", help="Emit as JSON.")] = False,
) -> None:
    """List a device's config change points (git history)."""

    setup_logging()
    err = Console(stderr=True)
    config, _settings = _load_configs_config(scope or default_config_path(), err)

    from obi.core.audit import AuditTrail
    from obi.core.engine import config_history
    from obi.core.serialize import config_history_to_json

    result = config_history(config, device, limit=limit, audit=AuditTrail(config.audit_path))

    if json_output:
        sys.stdout.write(config_history_to_json(result, indent=None) + "\n")
        return
    if not result.found:
        err.print(f"[yellow]{escape(result.detail or 'no stored config')}[/yellow]")
        raise typer.Exit(code=2)
    console = Console()
    if not result.entries:
        console.print(f"{escape(device)}: backed up, but no recorded change points yet.")
        return
    table = Table(title=f"{device} — config history", title_justify="left")
    for column in ("Commit", "When", "Changes", "Summary"):
        table.add_column(column, overflow="fold")
    for e in result.entries:
        when = e.captured_at.isoformat() if e.captured_at else "?"
        table.add_row(
            e.commit[:12],
            when,
            f"[green]+{e.insertions}[/green] [red]-{e.deletions}[/red]",
            escape(e.summary),
        )
    console.print(table)
    if result.truncated:
        console.print(f"[dim]… more than {limit} — raise --limit to see older changes.[/dim]")


@configs_app.command("diff")
def configs_diff_cmd(
    device: Annotated[str, typer.Argument(help="Device identity or address.")],
    scope: Annotated[Path | None, typer.Option("--scope", help="Config file path.")] = None,
    from_ref: Annotated[
        str | None,
        typer.Option("--from", help="Base version (sha / ISO date / '1w'); default: previous."),
    ] = None,
    to_ref: Annotated[
        str, typer.Option("--to", help="Target version; default: current.")
    ] = "current",
    since: Annotated[
        str | None,
        typer.Option("--since", help="Shorthand: diff the version at/before this to current."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit as JSON.")] = False,
) -> None:
    """Show what changed in a device's config between two points in time."""

    setup_logging()
    err = Console(stderr=True)
    config, _settings = _load_configs_config(scope or default_config_path(), err)

    from rich.text import Text

    from obi.core.audit import AuditTrail
    from obi.core.engine import config_diff
    from obi.core.serialize import config_diff_to_json

    result = config_diff(
        config,
        device,
        from_ref=from_ref,
        to_ref=to_ref,
        since=since,
        audit=AuditTrail(config.audit_path),
    )

    if json_output:
        sys.stdout.write(config_diff_to_json(result, indent=None) + "\n")
        return
    if not result.found:
        err.print(f"[yellow]{escape(result.detail or 'no stored config')}[/yellow]")
        raise typer.Exit(code=2)
    console = Console()
    if not result.changed:
        console.print(f"{escape(device)}: {escape(result.detail or 'no changes in that range.')}")
        return
    console.print(f"[dim]# {escape(device)} — {result.from_ref} → {result.to_ref}[/dim]")
    # Render diff lines as literal Text (never Rich markup — config lines contain [ ]).
    for line in result.diff.splitlines():
        style = (
            "green"
            if line.startswith("+")
            else "red"
            if line.startswith("-")
            else ("cyan" if line.startswith("@@") else "")
        )
        console.print(Text(line, style=style) if style else Text(line))


watch_app = typer.Typer(
    add_completion=False,
    help=(
        "Autonomous read-only log watcher (feature 014). `obi watch run` reads the new syslog "
        "since its last run and, when a cloud model is configured, judges it for problems — "
        "writing per-finding reports. `obi watch reports` / `obi watch status` review them. "
        "With Slack delivery enabled (`slack_notify`), qualifying findings are also DM'd to the "
        "operator (feature 015). Schedule `run` after the syslog rsync; see "
        "README.md § Autonomous watcher."
    ),
)
app.add_typer(watch_app, name="watch")


def _render_watch_run(run: WatchRun, console: Console, *, dry_run: bool) -> None:
    prefix = "[dry-run] " if dry_run else ""
    line = (
        f"{prefix}watch {run.outcome}: {run.new_event_count} new event(s), "
        f"{len(run.finding_ids)} finding(s); "
        f"tokens in/out={run.tokens_input}/{run.tokens_output} ({run.llm_calls} call(s))"
    )
    if run.alerts_delivered or run.alerts_failed:
        line += f"; slack delivered/failed={run.alerts_delivered}/{run.alerts_failed}"
    if run.remaining_candidates:
        line += f"; behind {run.remaining_candidates} event(s)"
        if run.lag_seconds:
            line += f" (oldest {run.lag_seconds // 60}m)"
    if run.heartbeat_sent:
        line += "; heartbeat sent"
    if run.conditions:
        line += f"; conditions: {', '.join(run.conditions)}"
    console.print(line)


def _render_watch_reports(
    findings: list[WatchFinding], console: Console, *, display_tz: tzinfo
) -> None:
    if not findings:
        console.print("No watch findings match.")
        return
    table = Table(title="Watch findings", title_justify="left")
    for column in ("Time", "Device", "Severity", "Conf", "Source", "Summary"):
        table.add_column(column, overflow="fold")
    for finding in findings:
        table.add_row(
            # 069: the Time column is the operator's local zone, not created_at's stored UTC.
            finding.created_at.astimezone(display_tz).strftime("%Y-%m-%d %H:%M"),
            finding.device,
            finding.severity,
            finding.confidence,
            finding.source,
            finding.summary,
        )
    console.print(table)
    console.print(f"{len(findings)} finding(s).")


@watch_app.command("run")
def watch_run(
    scope: Annotated[
        Path | None,
        typer.Option("--scope", help="Path to the config file (default: XDG config)."),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Judge but write nothing and advance nothing.")
    ] = False,
    since_now: Annotated[
        bool,
        typer.Option("--since-now", help="Ignore the stored watermark; advance it to now."),
    ] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit the run summary as JSON.")
    ] = False,
) -> None:
    """Run one watcher cycle (the cron target). Read-only; exits 0 even on handled degradations.

    When Slack delivery is enabled (``slack_notify``), qualifying findings (post-dedup, at/above
    ``slack_min_severity``) are also DM'd to the operator — best-effort, never failing the cycle.
    """

    setup_logging()
    err = Console(stderr=True)
    config = _load_logs_config(scope or default_config_path(), err)

    if not config.watch.enabled:
        Console().print("watch is disabled (\\[watch].enabled = false); nothing to do.")
        raise typer.Exit(code=0)

    import json as json_lib

    from obi.chat.session import resolve_api_key
    from obi.core.audit import AuditTrail
    from obi.core.llm import AnthropicLLMClient
    from obi.core.models import _utcnow
    from obi.core.watch import run_cycle, run_to_dict
    from obi.core.watchstore import WatchStore

    api_key = resolve_api_key(config.chat)
    llm_client = AnthropicLLMClient(api_key) if api_key else None
    if llm_client is None:
        err.print(
            "[yellow]note:[/yellow] no chat API key — the watcher needs a cloud model to judge "
            "logs, so this cycle produces no findings. Set ANTHROPIC_API_KEY or \\[chat].api_key."
        )

    notifier = None
    if config.watch.slack_notify:
        from obi.slack.notifier import SlackNotifier

        notifier = SlackNotifier.from_config(config)

    from obi.core.audit import recorder_from_config

    run = run_cycle(
        config,
        WatchStore(config.watch.watch_dir),
        audit=AuditTrail(config.audit_path),
        now=_utcnow(),
        llm_client=llm_client,
        notifier=notifier,
        since_now=since_now,
        dry_run=dry_run,
        # feature 042: the cycle opens its own watch-<run> capture scope over this recorder.
        recorder=recorder_from_config(config),
    )
    if json_output:
        sys.stdout.write(json_lib.dumps(run_to_dict(run)) + "\n")
    else:
        _render_watch_run(run, Console(), dry_run=dry_run)
    raise typer.Exit(code=0)


@watch_app.command("reports")
def watch_reports(
    scope: Annotated[
        Path | None,
        typer.Option("--scope", help="Path to the config file (default: XDG config)."),
    ] = None,
    since: Annotated[
        str, typer.Option("--since", help="Only findings newer than this ('24h', '7d', ISO).")
    ] = "24h",
    severity: Annotated[
        str | None, typer.Option("--severity", help="info | warning | critical.")
    ] = None,
    device: Annotated[
        str | None, typer.Option("--device", help="Substring match on device.")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="Max findings (newest first).")] = 50,
    show: Annotated[
        str | None, typer.Option("--show", help="Print one finding's markdown report by id.")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit matching findings as JSON.")
    ] = False,
) -> None:
    """Review recent watch findings (read-only)."""

    setup_logging()
    err = Console(stderr=True)
    config = _load_logs_config(scope or default_config_path(), err)
    assert config.logs is not None

    from obi.core import logstore
    from obi.core.watch import finding_to_dict
    from obi.core.watchstore import WatchStore

    store = WatchStore(config.watch.watch_dir)

    if show is not None:
        markdown = store.get_markdown(show)
        if markdown is None:
            err.print(f"[red]no report:[/red] no finding with id '{show}'.")
            raise typer.Exit(code=4)
        Console().print(markdown, markup=False, highlight=False)
        return

    since_dt = logstore.parse_when(since, config.logs.tzinfo()) if since else None
    findings = store.list_findings(
        since=since_dt,
        severity=severity.lower() if severity else None,
        device=device,
        limit=limit,
    )
    if json_output:
        import json as json_lib

        display_tz = config.logs.tzinfo()
        sys.stdout.write(
            json_lib.dumps([finding_to_dict(f, display_tz=display_tz) for f in findings]) + "\n"
        )
    else:
        _render_watch_reports(findings, Console(), display_tz=config.logs.tzinfo())


@watch_app.command("status")
def watch_status(
    scope: Annotated[
        Path | None,
        typer.Option("--scope", help="Path to the config file (default: XDG config)."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit the status as JSON.")] = False,
) -> None:
    """Show the watcher's last-run health and token usage (read-only)."""

    setup_logging()
    err = Console(stderr=True)
    config = _load_logs_config(scope or default_config_path(), err)

    from datetime import timedelta

    from obi.core.models import _utcnow
    from obi.core.watch import run_to_dict
    from obi.core.watchstore import WatchStore

    store = WatchStore(config.watch.watch_dir)
    last = store.load_last_run()
    recent = store.recent_runs(since=_utcnow() - timedelta(hours=24))
    tokens_in = sum(run.tokens_input for run in recent)
    tokens_out = sum(run.tokens_output for run in recent)

    if json_output:
        import json as json_lib

        payload = {
            "enabled": config.watch.enabled,
            "last_run": run_to_dict(last) if last is not None else None,
            "last_24h": {
                "runs": len(recent),
                "tokens_input": tokens_in,
                "tokens_output": tokens_out,
            },
        }
        sys.stdout.write(json_lib.dumps(payload) + "\n")
        return

    console = Console()
    console.print(f"watch enabled: {config.watch.enabled}")
    if last is None:
        console.print("watch has not run yet.")
    else:
        console.print(f"last run: {last.finished_at.isoformat()} — {last.outcome}")
        console.print(f"  new events: {last.new_event_count}; findings: {len(last.finding_ids)}")
        console.print(
            f"  tokens in/out: {last.tokens_input}/{last.tokens_output} ({last.llm_calls} call(s))"
        )
        if last.alerts_delivered or last.alerts_failed:
            console.print(f"  slack delivered/failed: {last.alerts_delivered}/{last.alerts_failed}")
        if last.remaining_candidates:
            console.print(
                f"  behind: {last.remaining_candidates} unjudged event(s), "
                f"oldest {last.lag_seconds // 60}m old"
            )
        else:
            console.print("  backlog: caught up to newest log")
        if last.ingest_age_seconds is not None:
            console.print(f"  newest log age: {last.ingest_age_seconds // 60}m")
        if last.heartbeat_sent:
            console.print("  heartbeat: sent this run")
        if last.cursor_after is not None:
            console.print(f"  watermark id: {last.cursor_after.id}")
        if last.conditions:
            console.print(f"  conditions: {', '.join(last.conditions)}")
    console.print(f"last 24h: {len(recent)} run(s), tokens in/out {tokens_in}/{tokens_out}")


# --- audit trail inspection (feature 042) ------------------------------------------------------

audit_app = typer.Typer(
    add_completion=False,
    help=(
        "Inspect obi's audit trail: every LLM call, tool call, device command, and human "
        "communication obi records. Read-only over the local store; inspecting the trail is "
        "never itself audited."
    ),
)
app.add_typer(audit_app, name="audit")

_AUDIT_KINDS = ("llm_call", "tool_call", "device_command", "human_message", "report", "action")


def _local(ts_iso: str) -> str:
    from datetime import datetime as _dt

    try:
        return _dt.fromisoformat(ts_iso).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ts_iso


def _audit_when(value: str | None) -> datetime | None:
    from datetime import UTC as _utc

    from obi.core import logstore

    return logstore.parse_when(value, _utc) if value else None


@audit_app.command("list")
def audit_list(
    scope: Annotated[
        Path | None,
        typer.Option("--scope", help="Path to the config file (default: XDG config)."),
    ] = None,
    since: Annotated[
        str | None, typer.Option("--since", help="Lower bound: '2h', '3d', or ISO datetime.")
    ] = None,
    until: Annotated[str | None, typer.Option("--until", help="Upper bound, same formats.")] = None,
    kind: Annotated[
        list[str] | None,
        typer.Option(
            "--kind",
            help="llm_call | tool_call | device_command | human_message | "
            "report | action (repeatable).",
        ),
    ] = None,
    source: Annotated[
        list[str] | None,
        typer.Option("--source", help="chat | voice | slack | watch | cli:<command> (repeatable)."),
    ] = None,
    session: Annotated[str | None, typer.Option("--session", help="Exact session id.")] = None,
    target: Annotated[
        str | None, typer.Option("--target", help="Exact device/host target.")
    ] = None,
    outcome: Annotated[str | None, typer.Option("--outcome", help="ok | error.")] = None,
    text: Annotated[
        str | None, typer.Option("--text", help="Free-text search over summary + payload.")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="Max events (newest first).")] = 50,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit a JSON array instead of a table.")
    ] = False,
) -> None:
    """List audit events, newest first; all filters compose (AND)."""

    err = Console(stderr=True)
    config = _load_audit_config(scope or default_config_path(), err)
    db_path = _audit_db_or_exit(config, err)
    for value in kind or []:
        if value not in _AUDIT_KINDS:
            err.print(f"[red]unknown kind:[/red] {value} — choose from {', '.join(_AUDIT_KINDS)}")
            raise typer.Exit(code=2)
    if outcome is not None and outcome not in ("ok", "error"):
        err.print("[red]--outcome must be 'ok' or 'error'[/red]")
        raise typer.Exit(code=2)

    from obi.core import auditstore

    conn = auditstore.connect_readonly(db_path)
    try:
        events, total = auditstore.query_events(
            conn,
            since=_audit_when(since),
            until=_audit_when(until),
            kinds=list(kind) if kind else None,
            sources=list(source) if source else None,
            session_id=session,
            target=target,
            outcome=outcome,
            text=text,
            limit=limit,
        )
    finally:
        conn.close()

    if json_output:
        import json as json_lib

        sys.stdout.write(json_lib.dumps({"events": events, "total": total}) + "\n")
        return

    console = Console()
    if not events:
        console.print("no matching audit events.")
        return
    table = Table(show_header=True, header_style="bold")
    for column in ("Time", "Id", "Kind", "Source", "Target", "Outcome", "Summary"):
        table.add_column(column, overflow="fold")
    for event in events:
        outcome_cell = "[red]error[/red]" if event["outcome"] == "error" else "[green]ok[/green]"
        table.add_row(
            _local(event["ts"]),
            str(event["id"]),
            event["kind"],
            event["source"],
            event["target"] or _DASH,
            outcome_cell,
            event["summary"],
        )
    console.print(table)
    console.print(f"[dim]{len(events)} of {total} matching event(s).[/dim]")


@audit_app.command("show")
def audit_show(
    event_id: Annotated[int, typer.Argument(help="The event id (from `obi audit list`).")],
    scope: Annotated[
        Path | None,
        typer.Option("--scope", help="Path to the config file (default: XDG config)."),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit the full event as JSON.")
    ] = False,
) -> None:
    """Show one event's complete stored payload (full prompt/response, args/result, command)."""

    err = Console(stderr=True)
    config = _load_audit_config(scope or default_config_path(), err)
    db_path = _audit_db_or_exit(config, err)

    import json as json_lib

    from obi.core import auditstore

    conn = auditstore.connect_readonly(db_path)
    try:
        event = auditstore.get_event(conn, event_id)
    finally:
        conn.close()
    if event is None:
        err.print(f"[red]no audit event with id {event_id}[/red]")
        raise typer.Exit(code=1)

    if json_output:
        sys.stdout.write(json_lib.dumps(event) + "\n")
        return

    console = Console()
    console.print(f"[bold]event {event['id']}[/bold] — {event['kind']} ({event['source']})")
    console.print(f"  time:     {_local(event['ts'])} ({event['ts']})")
    if event["session_id"]:
        console.print(f"  session:  {event['session_id']}")
    if event["target"]:
        console.print(f"  target:   {event['target']}")
    error_note = f" — {event['error']}" if event["error"] else ""
    console.print(f"  outcome:  {event['outcome']}{error_note}")
    if event["duration_ms"] is not None:
        console.print(f"  duration: {event['duration_ms']:.0f} ms")
    if event["truncated"]:
        console.print(
            f"  [yellow]payload truncated[/yellow] (original {event['original_bytes']} bytes)"
        )
    console.print(f"  summary:  {event['summary']}")
    console.print("[bold]payload[/bold]:")
    console.print(escape(json_lib.dumps(event["payload"], indent=2, ensure_ascii=False)))


@audit_app.command("trace")
def audit_trace(
    session_id: Annotated[str, typer.Argument(help="A session id (chat-…, watch-…, cli-…).")],
    scope: Annotated[
        Path | None,
        typer.Option("--scope", help="Path to the config file (default: XDG config)."),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit the ordered event array as JSON.")
    ] = False,
) -> None:
    """Reconstruct a whole session in causal order: human turns, model calls, tools, devices."""

    err = Console(stderr=True)
    config = _load_audit_config(scope or default_config_path(), err)
    db_path = _audit_db_or_exit(config, err)

    from obi.core import auditstore

    conn = auditstore.connect_readonly(db_path)
    try:
        events = auditstore.session_trace(conn, session_id)
    finally:
        conn.close()
    if not events:
        err.print(f"[red]no audit events for session {session_id}[/red]")
        raise typer.Exit(code=1)

    if json_output:
        import json as json_lib

        sys.stdout.write(json_lib.dumps({"session": session_id, "events": events}) + "\n")
        return

    glyphs = {
        "human_message": "🗣",
        "llm_call": "🧠",
        "tool_call": "🔧",
        "device_command": "📡",
        "report": "📄",
        "action": "·",
    }
    console = Console()
    console.print(f"[bold]session {session_id}[/bold] — {len(events)} event(s)")
    for event in events:
        glyph = glyphs.get(event["kind"], "·")
        marker = "[red]✗[/red]" if event["outcome"] == "error" else " "
        console.print(
            f"  {_local(event['ts'])}  {glyph} [dim]#{event['id']}[/dim] "
            f"{event['kind']:<15}{marker} {escape(event['summary'])}"
        )


@audit_app.command("stats")
def audit_stats(
    scope: Annotated[
        Path | None,
        typer.Option("--scope", help="Path to the config file (default: XDG config)."),
    ] = None,
    since: Annotated[
        str, typer.Option("--since", help="Period lower bound ('24h', '7d', ISO).")
    ] = "24h",
    until: Annotated[str | None, typer.Option("--until", help="Period upper bound.")] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit the aggregates as JSON.")
    ] = False,
) -> None:
    """Usage stats for a period: events by kind/source, token usage, top tools and targets."""

    err = Console(stderr=True)
    config = _load_audit_config(scope or default_config_path(), err)
    db_path = _audit_db_or_exit(config, err)

    from obi.core import auditstore

    conn = auditstore.connect_readonly(db_path)
    try:
        result = auditstore.stats(conn, since=_audit_when(since), until=_audit_when(until))
    finally:
        conn.close()

    if json_output:
        import json as json_lib

        sys.stdout.write(json_lib.dumps({"period": {"since": since, "until": until}, **result}))
        sys.stdout.write("\n")
        return

    console = Console()
    until_note = f" until {until}" if until else ""
    console.print(f"[bold]audit stats[/bold] — since {since}{until_note}")
    console.print(
        "events by kind: "
        + (", ".join(f"{k}={v}" for k, v in sorted(result["by_kind"].items())) or "none")
    )
    console.print(
        "events by source: "
        + (", ".join(f"{s}={v}" for s, v in sorted(result["by_source"].items())) or "none")
    )
    tokens = result["tokens"]
    console.print(
        f"tokens: {tokens['total_input']} in / {tokens['total_output']} out"
        + (
            " ("
            + ", ".join(
                f"{s}: {t['input']}/{t['output']}" for s, t in sorted(tokens["by_source"].items())
            )
            + ")"
            if tokens["by_source"]
            else ""
        )
    )
    if result["top_tools"]:
        console.print(
            "top tools: " + ", ".join(f"{t['tool']}×{t['count']}" for t in result["top_tools"][:5])
        )
    if result["top_targets"]:
        console.print(
            "top targets: "
            + ", ".join(f"{t['target']}×{t['count']}" for t in result["top_targets"][:5])
        )
    console.print(f"errors: {result['errors']}")


@audit_app.command("status")
def audit_status(
    scope: Annotated[
        Path | None,
        typer.Option("--scope", help="Path to the config file (default: XDG config)."),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit the health fields as JSON.")
    ] = False,
) -> None:
    """Audit store health: enabled?, size, event count, oldest/newest, retention."""

    err = Console(stderr=True)
    config = _load_audit_config(scope or default_config_path(), err)

    from obi.core import auditstore

    status = auditstore.store_status(config.audit.db_path)
    status["enabled"] = config.audit.enabled
    status["retention_days"] = config.audit.retention_days
    status["max_payload_bytes"] = config.audit.max_payload_bytes

    if json_output:
        import json as json_lib

        sys.stdout.write(json_lib.dumps(status) + "\n")
        return

    console = Console()
    console.print(f"auditing enabled: {config.audit.enabled}")
    console.print(f"store: {status['db_path']}")
    if not status["present"]:
        console.print("no events recorded yet (the store does not exist).")
        return
    console.print(f"  size: {status['size_bytes']:,} bytes; events: {status['event_count']:,}")
    if status["oldest"]:
        console.print(f"  oldest: {_local(status['oldest'])}; newest: {_local(status['newest'])}")
    console.print(f"  last 24h: {status['events_last_24h']:,} event(s)")
    console.print(
        f"  retention: {config.audit.retention_days} d; "
        f"payload bound: {config.audit.max_payload_bytes:,} bytes/field"
    )


# --- MCP tool surface (feature 101) ------------------------------------------------------------

mcp_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help=(
        "Serve obi's read-only tools to MCP clients (Claude Desktop, Claude Code) on this "
        "machine. The tool list is the shared registry — every tool obi's chat has, every "
        "call audited."
    ),
)
app.add_typer(mcp_app, name="mcp")


@mcp_app.command("serve")
def mcp_serve(
    scope: Annotated[
        Path | None,
        typer.Option("--scope", help="Path to the config/scope file (default: XDG config)."),
    ] = None,
) -> None:
    """Run the stdio MCP server for one client connection (normally launched by the client).

    stdout carries only protocol frames; logs go to stderr. Exits when the client closes stdin.
    """

    setup_logging()
    err = Console(stderr=True)
    config_path = scope or default_config_path()
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        err.print(f"[red]config error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc
    warning = perms_warning(config_path)
    if warning:
        err.print(f"[yellow]warning:[/yellow] {warning}")

    from obi.mcp.server import serve

    serve(config)


@mcp_app.command("install")
def mcp_install(
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", help="Show what each client's registration would become; write nothing."
        ),
    ] = False,
) -> None:
    """Register the server with Claude Desktop and Claude Code (user scope). Idempotent."""

    from obi.mcp.install import install

    err = Console(stderr=True)
    console = Console()
    try:
        outcomes = install(dry_run=dry_run)
    except OSError as exc:  # e.g. the obi executable could not be resolved
        err.print(f"[red]install failed:[/red] {escape(str(exc))}")
        raise typer.Exit(code=1) from exc
    failed = False
    for outcome in outcomes:
        style = "red" if outcome.outcome == "error" else "green"
        console.print(
            f"{outcome.target}: [{style}]{outcome.outcome}[/{style}]  {escape(outcome.detail)}"
        )
        failed = failed or outcome.outcome == "error"
    if failed:
        raise typer.Exit(code=1)


# --- feature 134: the demo dataset ----------------------------------------------------------

demo_app = typer.Typer(
    add_completion=False,
    help=(
        "Publishable demo datasets (feature 134). `snapshot` turns this host's stores into a "
        "self-contained dataset of a FICTIONAL network (Example Valley Net) behind a fail-closed "
        "leak gate; `gate` re-scans a dataset; `lookup` traces a demo value through the private "
        "mapping. Read-only toward every device and every live store."
    ),
)
app.add_typer(demo_app, name="demo")

from obi.cli.identity import identity_app  # noqa: E402 - feature 137 group, own module

app.add_typer(identity_app, name="identity")


def _demo_setup(
    config_path: Path | None, overrides: dict[str, Any] | None = None
) -> tuple[Any, dict[str, Any], Any, Path]:  # (Config, raw TOML, DemoProfile, config path)
    """Load the config (for audit), the raw TOML (for the demo) and the demo profile; exit 2."""

    import tomllib

    from obi.core.demo import ProfileError
    from obi.core.demo.profile import load_profile

    err = Console(stderr=True)
    path = config_path or default_config_path()
    try:
        config = load_config(path)
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
        profile = load_profile(raw, config_path=path, overrides=overrides)
    except (ConfigError, ProfileError, OSError) as exc:
        err.print(f"[red]demo setup error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc
    return config, raw, profile, path


@demo_app.command("snapshot")
def demo_snapshot_command(
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to config.toml.")
    ] = None,
    out: Annotated[
        Path | None, typer.Option("--out", help="Dataset directory to create (must not exist).")
    ] = None,
    since: Annotated[
        str | None, typer.Option("--since", help="Window start (ISO); default: busiest 72 h.")
    ] = None,
    until: Annotated[str | None, typer.Option("--until", help="Window end (ISO).")] = None,
    allow_oversize: Annotated[
        bool, typer.Option("--allow-oversize", help="Publish even above max_dataset_mb.")
    ] = False,
    strict_event_terms: Annotated[
        bool, typer.Option("--strict-event-terms", help="Treat event-term warnings as fatal.")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Preview window, sources and size; write nothing.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable summary.")] = False,
) -> None:
    """Produce a sanitized, gated demo dataset from this host's stores (contracts/cli.md)."""

    import json as _json

    from obi.core.audit import AuditTrail
    from obi.core.demo import DemoError
    from obi.core.demo.snapshot import run_snapshot

    setup_logging()
    err = Console(stderr=True)
    console = Console()
    overrides: dict[str, Any] = {"output_dir": out, "since": since, "until": until}
    if strict_event_terms:
        overrides["strict_event_terms"] = True
    config, raw, profile, _path = _demo_setup(config_path, overrides)
    with _audit_capture(config, "cli:demo-snapshot"):
        try:
            result = run_snapshot(
                raw,
                profile,
                dry_run=dry_run,
                allow_oversize=allow_oversize,
                audit=None if dry_run else AuditTrail(config.audit_path),
            )
        except DemoError as exc:
            err.print(f"[red]{type(exc).__name__}:[/red]\n{escape(str(exc))}")
            raise typer.Exit(code=exc.exit_code) from exc

    start, end = result.window
    summary = {
        "state": result.state,
        "output": str(result.output_dir) if result.output_dir else None,
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "harvested_tokens": result.harvested_tokens,
        "estimates": result.estimates,
        "total_bytes": result.total_bytes,
        "sources": [
            {"name": r.name, "status": r.status, "rows_in": r.rows_in, "rows_out": r.rows_out,
             "bytes": r.bytes, **({"reason": r.reason} if r.reason else {})}
            for r in result.reports
        ],
        "gate": (
            {"fatal": len(result.gate.fatal), "warnings": len(result.gate.warnings)}
            if result.gate is not None
            else None
        ),
    }  # fmt: skip
    if as_json:
        console.print_json(_json.dumps(summary))
        return
    console.print(
        f"target window: {start:%Y-%m-%d %H:%M} → {end:%Y-%m-%d %H:%M} ({profile.timezone})"
    )
    console.print(f"harvested real tokens checked: {result.harvested_tokens}")
    if dry_run:
        for key, value in sorted(result.estimates.items()):
            console.print(f"  estimate {key}: {value}")
    table = Table(show_header=True, header_style="bold")
    for column in ("source", "status", "rows in → out", "bytes", "reason"):
        table.add_column(column)
    for r in result.reports:
        rows = f"{r.rows_in} → {r.rows_out}" if r.status == "included" else ""
        table.add_row(r.name, r.status, rows, str(r.bytes or ""), escape(r.reason or ""))
    console.print(table)
    if result.gate is not None:
        console.print(
            f"leak gate: [green]{len(result.gate.fatal)} fatal[/green], "
            f"{len(result.gate.warnings)} warning(s); "
            f"dataset {result.total_bytes // (1024 * 1024)} MB"
        )
    if result.output_dir is not None:
        console.print(f"published: {escape(str(result.output_dir))}")


@demo_app.command("export")
def demo_export_command(
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to config.toml.")
    ] = None,
    dataset: Annotated[
        Path | None, typer.Option("--dataset", help="134 dataset (default: [demo] output_dir).")
    ] = None,
    out: Annotated[
        Path | None, typer.Option("--out", help="Bundle directory to create (must not exist).")
    ] = None,
    allow_oversize: Annotated[
        bool, typer.Option("--allow-oversize", help="Build even above max_bundle_mb.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable summary.")] = False,
) -> None:
    """Build the static public demo bundle from a 134 dataset (feature 136, contracts/cli.md)."""

    import json as _json

    from obi.core.audit import AuditTrail
    from obi.core.demo import DemoError
    from obi.core.demo.export import load_export_profile, run_export

    setup_logging()
    err = Console(stderr=True)
    config, raw, _demo_profile, path = _demo_setup(config_path)
    try:
        profile = load_export_profile(
            raw, config_path=path, overrides={"dataset": dataset, "bundle_dir": out}
        )
    except DemoError as exc:
        err.print(f"[red]{type(exc).__name__}:[/red] {escape(str(exc))}")
        raise typer.Exit(code=exc.exit_code) from exc
    with _audit_capture(config, "cli:demo-export"):
        try:
            result = run_export(
                raw,
                path,
                profile,
                allow_oversize=allow_oversize,
                audit=AuditTrail(config.audit_path),
                recordings=_demo_recordings(raw, path),
            )
        except DemoError as exc:
            err.print(f"[red]{type(exc).__name__}:[/red]\n{escape(str(exc))}")
            raise typer.Exit(code=exc.exit_code) from exc
    summary = {
        "bundle": str(result.bundle_dir),
        "total_bytes": result.total_bytes,
        "routes": result.counts,
        "slices": result.slices,
        "recordings": result.recordings,
        "left_out": result.left_out,
        "gate": {"fatal": len(result.gate.fatal), "warnings": len(result.gate.warnings)},
    }
    if as_json:
        Console().print_json(_json.dumps(summary))
        return
    console = Console()
    console.print(f"bundle: {escape(str(result.bundle_dir))} ({result.total_bytes // 2**20} MB)")
    for route, n in sorted(result.counts.items()):
        console.print(f"  {route:<10} {n} file(s)")
    for name, sl in result.slices.items():
        console.print(f"  {name} slice: {sl['rows']:,} of {sl['of']:,}")
    console.print(
        f"  chat recordings: {result.recordings}"
        + (f" ({len(result.left_out)} left out — re-review)" if result.left_out else "")
    )
    console.print(
        f"leak gate: [green]{len(result.gate.fatal)} fatal[/green], "
        f"{len(result.gate.warnings)} warning(s)"
    )


@demo_app.command("publish")
def demo_publish_command(
    pages: Annotated[
        bool, typer.Option("--pages", help="Force-push to the Pages orphan branch.")
    ] = False,
    vm: Annotated[bool, typer.Option("--vm", help="rsync to [demo.publish] vm_target.")] = False,
    bundle: Annotated[
        Path | None, typer.Option("--bundle", help="Bundle dir (default: [demo.export]).")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Report what would be sent; send nothing.")
    ] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to config.toml.")
    ] = None,
) -> None:
    """Publish the last GATED bundle; refuses (exit 3) if it changed since (contracts/cli.md)."""

    from obi.core.audit import AuditTrail
    from obi.core.demo import DemoError
    from obi.core.demo.export import load_export_profile
    from obi.core.demo.publish import publish

    setup_logging()
    err = Console(stderr=True)
    config, raw, _demo_profile, path = _demo_setup(config_path)
    try:
        profile = load_export_profile(raw, config_path=path, overrides={"bundle_dir": bundle})
        with _audit_capture(config, "cli:demo-publish"):
            result = publish(
                profile.bundle_dir, profile, pages=pages, vm=vm, dry_run=dry_run,
                audit=AuditTrail(config.audit_path),
            )  # fmt: skip
    except DemoError as exc:
        err.print(f"[red]{type(exc).__name__}:[/red] {escape(str(exc))}")
        raise typer.Exit(code=exc.exit_code) from exc
    console = Console()
    verb = "would send" if result.dry_run else "sent"
    console.print(f"{verb} {result.files:,} files ({result.total_bytes / 2**20:.1f} MB):")
    for dest in result.destinations:
        tail = f" → {result.pushed[dest][:12]}" if dest in result.pushed else ""
        console.print(f"  {escape(dest)}{tail}")


def _demo_recordings(raw: dict[str, Any], config_path: Path) -> Any:
    """The approved-recordings provider for an export (US2/US3)."""

    from obi.core.demo.chats import bundle_recordings

    return lambda view, staging: bundle_recordings(raw, config_path, view, staging)


chats_app = typer.Typer(
    help=(
        "Curate the chat preview (feature 136): list the operator's own past conversations, "
        "show one exactly as it would ship, approve or revoke it. Only approved, gate-clean "
        "recordings whose rewrite is unchanged enter the bundle."
    ),
    no_args_is_help=True,
)
demo_app.add_typer(chats_app, name="chats")

_CHATS_CONFIG = typer.Option("--config", help="Path to config.toml.")
_CHATS_DATASET = typer.Option("--dataset", help="134 dataset (default: [demo] output_dir).")
_CHATS_REC = typer.Argument(help="A rec-… id from `obi demo chats list`.")


def _chats_session(config_path: Path | None, dataset: Path | None) -> tuple[Any, Any]:
    """(Config, Curation) — or exit with the error's code on a setup error."""

    from obi.core.demo import DemoError
    from obi.core.demo.chats import open_curation
    from obi.core.demo.export import load_export_profile

    config, raw, _profile, path = _demo_setup(config_path)
    try:
        export = load_export_profile(raw, config_path=path, overrides={"dataset": dataset})
        cur = open_curation(raw, path, export.dataset, clip_bytes=export.tool_result_clip_bytes)
    except DemoError as exc:
        Console(stderr=True).print(f"[red]{type(exc).__name__}:[/red] {escape(str(exc))}")
        raise typer.Exit(code=exc.exit_code) from exc
    return config, cur


def _chats_since(raw: str) -> Any:
    import re
    from datetime import timedelta

    match = re.fullmatch(r"(\d+)([hd])", raw)
    if match is None:
        Console(stderr=True).print(
            f"[red]--since must look like 12h / 30d[/red] (got {escape(raw)})"
        )
        raise typer.Exit(code=2)
    unit = "hours" if match.group(2) == "h" else "days"
    return timedelta(**{unit: int(match.group(1))})


def _chats_find(cur: Any, rec: str) -> Any:
    from datetime import timedelta

    from obi.core.demo.chats import find

    item = find(cur, rec, since=timedelta(days=3650))
    if item is None:
        Console(stderr=True).print(f"no candidate conversation {escape(rec)}.")
        raise typer.Exit(code=2)
    return item


def _chats_audit(config: Any, action: str, rec: str, result: str) -> None:
    """Audit approve/revoke with the FICTIONAL rec_id only — never content or the session."""

    from datetime import UTC, datetime

    from obi.core.audit import AuditTrail
    from obi.core.models import AuditOutcome, AuditRecord

    with _audit_capture(config, "cli:demo-chats"):
        AuditTrail(config.audit_path).record(
            AuditRecord(
                timestamp=datetime.now(UTC),
                action=action,
                target=rec,
                reason="operator chat-preview curation (feature 136)",
                outcome=AuditOutcome.OK if result == "ok" else AuditOutcome.ERROR,
                params={"result": result},
            )
        )


@chats_app.command("list")
def demo_chats_list_command(
    since: Annotated[str, typer.Option("--since", help="How far back (12h / 30d).")] = "30d",
    surface: Annotated[
        str | None, typer.Option("--surface", help="Only slack | chat | voice.")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable.")] = False,
    config_path: Annotated[Path | None, _CHATS_CONFIG] = None,
    dataset: Annotated[Path | None, _CHATS_DATASET] = None,
) -> None:
    """The operator's own conversations, rewritten, with their curation status."""

    import json as _json

    from rich.table import Table

    from obi.core.demo.chats import listings

    setup_logging()
    window = _chats_since(since)
    _config, cur = _chats_session(config_path, dataset)
    rows = [
        {
            "id": x.rec_id,
            "surface": x.candidate.surface,
            "date": x.candidate.started.astimezone().strftime("%Y-%m-%d %H:%M"),
            "persona": "admin",
            "question": x.recording["title"],
            "turns": len(x.recording["turns"]),
            "tools": len(x.candidate.tools),
            "status": x.status,
            "reasons": x.check.reasons,
        }
        for x in listings(cur, since=window)
        if surface is None or x.candidate.surface == surface
    ]
    if as_json:
        Console().print_json(_json.dumps(rows))
        return
    if not rows:
        Console().print("no candidate conversations in that window.")
        return
    colour = {"approved": "green", "new": "cyan", "stale": "yellow", "needs_review": "yellow",
              "refused": "red"}  # fmt: skip
    table = Table("id", "surface", "date", "persona", "first question", "turns", "tools",
                  "status")  # fmt: skip
    for r in rows:
        status = f"[{colour.get(r['status'], 'white')}]{r['status']}[/]"
        if r["reasons"]:
            status += f" ({escape(', '.join(r['reasons']))})"
        table.add_row(r["id"], r["surface"], r["date"], r["persona"], escape(r["question"]),
                      str(r["turns"]), str(r["tools"]), status)  # fmt: skip
    Console().print(table)


@chats_app.command("show")
def demo_chats_show_command(
    rec: Annotated[str, _CHATS_REC],
    config_path: Annotated[Path | None, _CHATS_CONFIG] = None,
    dataset: Annotated[Path | None, _CHATS_DATASET] = None,
) -> None:
    """Print the full rewritten recording exactly as it would ship."""

    import json as _json

    setup_logging()
    _config, cur = _chats_session(config_path, dataset)
    item = _chats_find(cur, rec)
    reasons = f" ({', '.join(item.check.reasons)})" if item.check.reasons else ""
    Console(stderr=True).print(f"status: {item.status}{escape(reasons)}")
    print(_json.dumps(item.recording, ensure_ascii=False, indent=2))


@chats_app.command("approve")
def demo_chats_approve_command(
    rec: Annotated[str, _CHATS_REC],
    accept_review: Annotated[
        bool,
        typer.Option("--accept-review", help="Also approve a needs_review one you have read."),
    ] = False,
    config_path: Annotated[Path | None, _CHATS_CONFIG] = None,
    dataset: Annotated[Path | None, _CHATS_DATASET] = None,
) -> None:
    """Re-run rewrite + gate + entity check; pin the recording's hash only if clean."""

    from obi.core.demo import LeakGateError
    from obi.core.demo.chats import approve

    setup_logging()
    config, cur = _chats_session(config_path, dataset)
    item = _chats_find(cur, rec)
    try:
        approve(item, accept_review=accept_review)
    except LeakGateError as exc:
        _chats_audit(config, "demo_chat_approve", rec, "refused")
        Console(stderr=True).print(f"[red]refused:[/red] {escape(str(exc))}")
        raise typer.Exit(code=3) from exc
    _chats_audit(config, "demo_chat_approve", rec, "ok")
    Console().print(f"approved {rec} — it ships with the next `obi demo export`.")


@chats_app.command("revoke")
def demo_chats_revoke_command(
    rec: Annotated[str, _CHATS_REC],
    config_path: Annotated[Path | None, _CHATS_CONFIG] = None,
    dataset: Annotated[Path | None, _CHATS_DATASET] = None,
) -> None:
    """Withdraw an approval; the recording leaves the next bundle."""

    from obi.core.demo.chats import revoke

    setup_logging()
    config, cur = _chats_session(config_path, dataset)
    item = _chats_find(cur, rec)
    removed = revoke(item)
    _chats_audit(config, "demo_chat_revoke", rec, "ok" if removed else "not_approved")
    Console().print(f"revoked {rec}." if removed else f"{rec} was not approved.")


@demo_app.command("gate")
def demo_gate_command(
    dataset: Annotated[Path, typer.Argument(help="Dataset directory to re-scan.")],
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to config.toml.")
    ] = None,
    strict_event_terms: Annotated[
        bool, typer.Option("--strict-event-terms", help="Treat event-term warnings as fatal.")
    ] = False,
) -> None:
    """Re-run the leak gate over an existing dataset (exit 3 on any fatal finding)."""

    from obi.core.demo.gate import LeakGate, format_report
    from obi.core.demo.snapshot import prepare, public_values_of

    setup_logging()
    if not dataset.is_dir():
        Console(stderr=True).print(f"[red]not a dataset directory:[/red] {escape(str(dataset))}")
        raise typer.Exit(code=2)
    overrides: dict[str, Any] = {"output_dir": dataset.parent / f"{dataset.name}.gate-check"}
    _config, raw, profile, _path = _demo_setup(config_path, overrides)
    _window, harvested, _mapping, _shift, _reloc = prepare(raw, profile)
    result = LeakGate(
        harvested,
        strict_event_terms=strict_event_terms,
        exempt_values=public_values_of(dataset),
    ).scan(dataset)
    Console().print(escape(format_report(result)))
    if result.fatal:
        raise typer.Exit(code=3)


@demo_app.command("lookup")
def demo_lookup_command(
    value: Annotated[
        str, typer.Argument(help="A demo (fictional) value; a real one with --reverse.")
    ],
    reverse: Annotated[
        bool, typer.Option("--reverse", help="Map a REAL value to its fictional one instead.")
    ] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to config.toml.")
    ] = None,
) -> None:
    """Trace a value through the PRIVATE mapping (local terminal only; never audited by value)."""

    from datetime import UTC, datetime

    from obi.core.audit import AuditTrail
    from obi.core.demo import ProfileError
    from obi.core.demo.mapping import PseudonymMap
    from obi.core.models import AuditOutcome, AuditRecord

    setup_logging()
    err = Console(stderr=True)
    config, _raw, profile, _path = _demo_setup(
        config_path, {"output_dir": Path.cwd() / ".demo-lookup"}
    )
    if not profile.mapping_path.exists():
        err.print("[red]no private mapping yet:[/red] run `obi demo snapshot` first.")
        raise typer.Exit(code=2)
    try:
        mapping = PseudonymMap.load_or_create(profile.mapping_path)
    except ProfileError as exc:
        err.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=2) from exc
    hits = mapping.lookup(value) if reverse else mapping.reverse(value)
    AuditTrail(config.audit_path).record(
        AuditRecord(
            timestamp=datetime.now(UTC),
            action="demo_lookup",
            target="demo-mapping",
            reason="operator lookup in the private demo mapping (feature 134)",
            outcome=AuditOutcome.OK,
            params={"direction": "real→fake" if reverse else "fake→real", "hits": len(hits)},
        )
    )
    if not hits:
        err.print("no mapping entry for that value.")
        raise typer.Exit(code=1)
    for cls, other in hits:
        Console().print(f"{cls}: {escape(other)}")


if __name__ == "__main__":  # pragma: no cover
    app()

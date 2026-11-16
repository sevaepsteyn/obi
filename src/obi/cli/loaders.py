"""CLI config-loader helpers (feature 066): load + validate config for a command, or exit.

Each returns the parsed :class:`~obi.core.config.Config` (plus any section-specific settings) after
surfacing a permissions warning, or raises ``typer.Exit`` with a clear message on a config error —
the shared command preamble, extracted from main.py so every sub-app loads config the same way.
"""

from __future__ import annotations

import logging
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

from obi.core.config import (
    Config,
    ConfigBackupSettings,
    ConfigError,
    SnmpHistorySettings,
    load_config,
    perms_warning,
)


def _load_logs_config(config_path: Path, err: Console) -> Config:
    """Load config and ensure a [logs] section exists, or exit with a clear message."""

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        err.print(f"[red]config error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc

    warning = perms_warning(config_path)
    if warning:
        logging.getLogger("obi").warning("insecure config permissions", extra={"detail": warning})
        err.print(f"[yellow]warning:[/yellow] {warning}")

    if config.logs is None:
        err.print(
            "[red]logs not configured:[/red] add a \\[logs] section to "
            f"{config_path} — see README.md § Device syslog."
        )
        raise typer.Exit(code=2)
    return config


def _load_radio_config(config_path: Path, err: Console) -> Config:
    """Load config and ensure a [radio] section exists, or exit with a clear message."""

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        err.print(f"[red]config error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc

    warning = perms_warning(config_path)
    if warning:
        logging.getLogger("obi").warning("insecure config permissions", extra={"detail": warning})
        err.print(f"[yellow]warning:[/yellow] {warning}")

    if config.radio is None:
        err.print(
            "[red]radio not configured:[/red] add a \\[radio] section to "
            f"{config_path} — see docs/setup/radio.md."
        )
        raise typer.Exit(code=2)
    return config


def _load_unifi_internal_config(config_path: Path, err: Console) -> Config:
    """Load config and ensure a [unifi-internal] section exists, or exit with a clear message."""

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        err.print(f"[red]config error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc

    warning = perms_warning(config_path)
    if warning:
        logging.getLogger("obi").warning("insecure config permissions", extra={"detail": warning})
        err.print(f"[yellow]warning:[/yellow] {warning}")

    if config.unifi_internal is None:
        err.print(
            "[red]UniFi event ingestion not configured:[/red] add a \\[unifi-internal] section "
            f"(local-admin username/password) to {config_path} — see "
            "README.md § UniFi controller events."
        )
        raise typer.Exit(code=2)
    return config


def _load_uisp_ingest_config(config_path: Path, err: Console) -> Config:
    """Load config and ensure UISP log ingestion is configured, or exit with a clear message."""

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        err.print(f"[red]config error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc

    warning = perms_warning(config_path)
    if warning:
        logging.getLogger("obi").warning("insecure config permissions", extra={"detail": warning})
        err.print(f"[yellow]warning:[/yellow] {warning}")

    if config.uisp is None or not config.uisp.ingest_enabled:
        err.print(
            "[red]UISP log ingestion not configured:[/red] add `ingest_events`/`ingest_outages` to "
            f"the \\[uisp] section in {config_path} — see README.md § UISP controller logs."
        )
        raise typer.Exit(code=2)
    return config


def _load_web_config(config_path: Path, err: Console) -> Config:
    """Load config and ensure the UISP geometry source is configured, or exit clearly."""

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        err.print(f"[red]config error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc

    warning = perms_warning(config_path)
    if warning:
        logging.getLogger("obi").warning("insecure config permissions", extra={"detail": warning})
        err.print(f"[yellow]warning:[/yellow] {warning}")

    if config.uisp is None:
        err.print(
            "[red]web export needs the UISP controller:[/red] add a \\[uisp] section to "
            f"{config_path} — see README.md § Web terminal."
        )
        raise typer.Exit(code=2)
    return config


def _load_metrics_config(config_path: Path, err: Console) -> tuple[Config, SnmpHistorySettings]:
    """Load config and ensure metrics collection is configured, or exit with a clear message."""

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        err.print(f"[red]config error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc

    warning = perms_warning(config_path)
    if warning:
        logging.getLogger("obi").warning("insecure config permissions", extra={"detail": warning})
        err.print(f"[yellow]warning:[/yellow] {warning}")

    if config.snmp is None:
        err.print(
            "[red]snmp not configured:[/red] add an \\[snmp] section (community for v2c, or v3 "
            f"credentials) to {config_path} — see README.md § SNMP."
        )
        raise typer.Exit(code=2)
    if config.snmp.history is None:
        err.print(
            "[red]metrics collection not configured:[/red] add an \\[snmp.history] subsection to "
            f"{config_path} — see README.md § SNMP."
        )
        raise typer.Exit(code=2)
    return config, config.snmp.history


def _load_health_config(config_path: Path, err: Console) -> tuple[Config, Path]:
    """Load config and resolve the health store path (feature 096).

    Health reads the metrics store; ``[snmp.history]`` supplies its path when configured,
    else the default location — the section is NOT required (a Linux-probe-only setup
    still has health data in the default store)."""

    from obi.core.config import default_metrics_db_path

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        err.print(f"[red]config error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc

    warning = perms_warning(config_path)
    if warning:
        logging.getLogger("obi").warning("insecure config permissions", extra={"detail": warning})
        err.print(f"[yellow]warning:[/yellow] {warning}")

    if config.snmp is not None and config.snmp.history is not None:
        return config, config.snmp.history.db_path
    return config, default_metrics_db_path()


def _load_configs_config(config_path: Path, err: Console) -> tuple[Config, ConfigBackupSettings]:
    """Load config and ensure config backup is configured, or exit with a clear message."""

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        err.print(f"[red]config error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc

    warning = perms_warning(config_path)
    if warning:
        logging.getLogger("obi").warning("insecure config permissions", extra={"detail": warning})
        err.print(f"[yellow]warning:[/yellow] {warning}")

    if config.configs is None:
        err.print(
            "[red]config backup not configured:[/red] add a \\[configs] section to "
            f"{config_path} — see README.md § RouterOS config backup. Your other commands "
            "still work."
        )
        raise typer.Exit(code=2)
    return config, config.configs


def _load_audit_config(config_path: Path, err: Console) -> Config:
    try:
        return load_config(config_path)
    except ConfigError as exc:
        err.print(f"[red]config error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc


def _audit_db_or_exit(config: Config, err: Console) -> Path:
    db_path = config.audit.db_path
    if not db_path.exists():
        state = "disabled" if not config.audit.enabled else "enabled"
        err.print(
            f"no audit events yet — the store does not exist at {db_path} (auditing {state}). "
            "Run any obi command (chat, watch run, an ingest) to start recording."
        )
        raise typer.Exit(code=0)
    return db_path

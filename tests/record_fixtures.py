"""Read-only helper to capture RouterOS fixtures from a real device.

This helper issues ONLY read (`print`) commands — never `set`/`add`/`remove`/`exec` — so it
is safe to run against the production network (Constitution Principle I). It records the
responses of `/system/identity`, `/system/resource`, `/system/routerboard`, `/ip/neighbor`,
and `/routing/ospf/neighbor` into a fixture JSON file used by the offline adapter/integration
tests.

Usage (from the repo root):

    uv run python tests/record_fixtures.py --host 192.0.2.1 --user admin --no-tls --name edge-gw-01
    # password is prompted (not echoed) unless --password is given

    # Or reuse obi's own 0600 config (host/user/password/TLS) so no secret is on the CLI:
    uv run python tests/record_fixtures.py --from-config --name chestnut-gw-01

The output file is written to ``tests/fixtures/<name>.json``. Review it and anonymize any
sensitive values (e.g. serial numbers) before committing.
"""

from __future__ import annotations

import argparse
import getpass
import json
from pathlib import Path
from typing import Any

import librouteros
from librouteros.exceptions import LibRouterosError

from obi.core.config import default_config_path, load_config
from obi.core.drivers.mikrotik import _ssl_wrapper

# Strictly read-only paths. Iterating a librouteros path issues a `print` (a read).
# Single-record paths capture the first record; list paths capture every record.
READ_PATHS: dict[str, tuple[str, ...]] = {
    "identity": ("system", "identity"),
    "resource": ("system", "resource"),
    "routerboard": ("system", "routerboard"),
}

# Multi-record read-only paths (a `print` of a table). The neighbor-discovery table feeds
# feature 002's crawl; the OSPF neighbor table feeds feature 004's link-local resolution
# (its `address` is the 169.254.x peer link-local; its `router-id` is the peer's management
# address). Feature 052 adds the OSPF topology read surface (contracts/routeros-ospf-reads.md):
# the link-state database (`routing/ospf/lsa` — the topology truth, incl. the `body` text blob),
# instance/area/interface-template/interface settings for per-router enrichment, and the VLAN
# parentage table (`interface/vlan`) that structural shared-fate detection reads. The 053 polish
# adds `/ip/address` — configured assignments let a one-sided ghost resolve its intended peer
# structurally (the /31 partner address) even when the far side's OSPF template is disabled. All
# read-only `print`s (iterating a librouteros path issues a `print`).
LIST_PATHS: dict[str, tuple[str, ...]] = {
    "neighbors": ("ip", "neighbor"),
    "ospf_neighbors": ("routing", "ospf", "neighbor"),
    "ospf_lsa": ("routing", "ospf", "lsa"),
    "ospf_instance": ("routing", "ospf", "instance"),
    "ospf_area": ("routing", "ospf", "area"),
    "ospf_iface_template": ("routing", "ospf", "interface-template"),
    "ospf_interface": ("routing", "ospf", "interface"),
    "vlan": ("interface", "vlan"),
    "ip_address": ("ip", "address"),
    # Feature 095: the netwatch table (per-target up/down + rtt/loss stats) feeds the status
    # dashboard's inter-site latency tile. A plain table `print` — read-only like everything here.
    "netwatch": ("tool", "netwatch"),
}

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# feature 075: the PHY console's physical/L2 read set. The print menus (iterating a path issues a
# `print`) and — new for 075 — two `monitor … once` telemetry reads, issued through the connection
# call seam exactly as the ping/traceroute capture would (a bare command sentence, not a path
# print). `monitor` self-terminates only with `once`; without it it streams forever, so every
# capture here passes it. All reads remain strictly READ-ONLY (Constitution Principle I).
PHY_PRINT_PATHS: dict[str, tuple[str, ...]] = {
    "routerboard": ("system", "routerboard"),  # single-record (dict); the rest are tables
    "ethernet": ("interface", "ethernet"),
    "ethernet-poe": ("interface", "ethernet", "poe"),
    "bridge-port": ("interface", "bridge", "port"),
    "bridge-vlan": ("interface", "bridge", "vlan"),
    "neighbor": ("ip", "neighbor"),
}
# The two command-word telemetry reads. ``numbers`` is filled at capture time from the device's own
# ethernet names (never user input); ``once`` is the self-terminating flag whose exact wire form
# (empty ``=once=`` vs a boolean) this recorder is the sanctioned place to pin (research.md D9 ✱).
PHY_MONITOR_COMMANDS: dict[str, str] = {
    "ethernet-monitor": "/interface/ethernet/monitor",
    "poe-monitor": "/interface/ethernet/poe/monitor",
}


def capture(
    *,
    host: str,
    user: str,
    password: str,
    name: str,
    use_tls: bool = True,
    port: int | None = None,
    out_dir: Path | None = None,
    phy: bool = False,
) -> Path:
    """Connect read-only and write a ``<name>.json`` fixture; returns the written path.

    When ``phy`` is set, ALSO write the feature-075 PHY fixture set as individual per-menu files
    (``routerboard.json``, ``ethernet.json``, ``ethernet-monitor.json``, ``poe-monitor.json``,
    ``bridge-port.json``, ``bridge-vlan.json``, ``neighbor.json``) alongside the combined fixture,
    over the same read-only login.
    """

    kwargs: dict[str, Any] = {
        "host": host,
        "username": user,
        "password": password,
        "timeout": 5.0,
        "port": port or (8729 if use_tls else 8728),
    }
    if use_tls:
        kwargs["ssl_wrapper"] = _ssl_wrapper

    api = librouteros.connect(**kwargs)
    try:
        data: dict[str, Any] = {
            "_comment": (
                f"Recorded READ-ONLY responses from {name} via print commands. "
                "No state was changed. Anonymize sensitive values before committing."
            )
        }
        for key, path in READ_PATHS.items():
            records = list(api.path(*path))
            data[key] = dict(records[0]) if records else {}
        for key, path in LIST_PATHS.items():
            data[key] = [dict(record) for record in api.path(*path)]
        if phy:
            capture_phy(api, out_dir or FIXTURES_DIR)
    finally:
        api.close()

    out = (out_dir or FIXTURES_DIR) / f"{name}.json"
    out.write_text(json.dumps(data, indent=2) + "\n")
    return out


def _monitor_once(api: Any, command: str, numbers: list[str]) -> list[dict[str, Any]]:
    """Issue one ``<command> once`` over the connection call seam for ``numbers`` (read-only).

    Mirrors the ping/traceroute capture shape: a bare command sentence via ``api(cmd, **kwargs)``.
    ``once`` self-terminates the monitor so this returns one snapshot per interface then stops.
    An empty ``numbers`` list ⇒ no call (a non-PoE model returns no poe-out ports).
    """

    if not numbers:
        return []
    # `.numbers` selects the interfaces; `once` is the self-terminating flag (empty value = flag).
    kwargs = {"once": "", "numbers": ",".join(numbers)}
    return [dict(sentence) for sentence in api(command, **kwargs)]


def capture_phy(api: Any, out_dir: Path) -> None:
    """Write the feature-075 PHY fixture set as individual per-menu files over an open connection.

    Strictly read-only: six ``print`` menus plus the two ``monitor … once`` telemetry reads, keyed
    by the device's own ethernet names. Each file holds the RAW RouterOS records (anonymize serials
    before committing)."""

    prints: dict[str, list[dict[str, Any]]] = {}
    for key, path in PHY_PRINT_PATHS.items():
        try:
            prints[key] = [dict(record) for record in api.path(*path)]
        except LibRouterosError as exc:
            # A menu absent on this model (e.g. /interface/ethernet/poe on a non-PoE router) is a
            # STATE, not a capture failure — record it empty, exactly as the engine degrades the
            # section. The parser tests want this shape too.
            print(f"  (menu /{'/'.join(path)} absent: {exc}) -> empty")
            prints[key] = []
    ether_names = [str(r["name"]) for r in prints["ethernet"] if r.get("name")]
    poe_names = [str(r["name"]) for r in prints["ethernet-poe"] if r.get("name")]

    monitors = {
        "ethernet-monitor": _monitor_once(
            api, PHY_MONITOR_COMMANDS["ethernet-monitor"], ether_names
        ),
        "poe-monitor": _monitor_once(api, PHY_MONITOR_COMMANDS["poe-monitor"], poe_names),
    }

    # routerboard is a single record; everything else is a table (list).
    (out_dir / "routerboard.json").write_text(
        json.dumps(prints["routerboard"][0] if prints["routerboard"] else {}, indent=2) + "\n"
    )
    for key, records in {**prints, **monitors}.items():
        if key == "routerboard":
            continue
        (out_dir / f"{key}.json").write_text(json.dumps(records, indent=2) + "\n")


# Feature 096: the two subtrees the health/optics profiles walk. The fixture maps each base
# to {row-suffix: value}, which feeds straight into tests.fakes.FakeSnmpAgent.table.
SNMP_HEALTH_WALKS: tuple[str, ...] = (
    "1.3.6.1.4.1.14988.1.1.3.100.1",  # mtxrHealth v7 sensor table (name/value/unit columns)
    "1.3.6.1.4.1.14988.1.1.19.1.1",  # mtxrOptical DDM table
)
_OPTICS_SERIAL_COLUMN = "12"  # mtxrOptical vendor-serial column — redacted before writing


def capture_snmp_health(*, host: str, name: str, out_dir: Path | None = None) -> Path:
    """GETBULK-walk the 096 health/optics subtrees READ-ONLY via obi's own SNMP transport and
    write ``tests/fixtures/snmp/<name>.json``. The community comes from obi's 0600 config
    (``[snmp]``) — never the CLI. Optics vendor serials are redacted at capture time."""

    from obi.core.drivers.snmp import ResolvedAuth, SnmpTransportError, _PysnmpTransport

    cfg = load_config(default_config_path())
    if cfg.snmp is None:
        raise SystemExit("no [snmp] section in config; cannot capture SNMP fixtures")
    auth = ResolvedAuth(
        version=cfg.snmp.version,
        port=cfg.snmp.port,
        timeout=cfg.snmp.timeout,
        retries=cfg.snmp.retries,
        community=cfg.snmp.community,
    )
    transport = _PysnmpTransport(host, auth)
    walks: dict[str, dict[str, int | float | str]] = {}
    for base in SNMP_HEALTH_WALKS:
        rows: dict[str, int | float | str] = {}
        try:
            raw_vars = transport.walk(base)
        except SnmpTransportError as exc:
            print(f"  (walk {base} failed: {exc.detail}) -> empty")
            raw_vars = []
        for var in raw_vars:
            if var.value is None or not var.oid.startswith(base + "."):
                continue
            suffix = var.oid[len(base) + 1 :]
            value = var.value
            if suffix.startswith(_OPTICS_SERIAL_COLUMN + "."):
                value = "SERIAL-REDACTED"
            rows[suffix] = value
        walks[base] = rows
    data: dict[str, Any] = {
        "_comment": (
            f"Recorded READ-ONLY SNMP GETBULK walks from {name} (096 health/optics subtrees). "
            "No state was changed. Optics vendor serials redacted at capture."
        ),
        "walks": walks,
    }
    target_dir = out_dir or (FIXTURES_DIR / "snmp")
    target_dir.mkdir(parents=True, exist_ok=True)
    out = target_dir / f"{name}.json"
    out.write_text(json.dumps(data, indent=2) + "\n")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture read-only RouterOS fixtures.")
    parser.add_argument("--host", default=None, help="device management address")
    parser.add_argument("--user", default=None, help="read-only RouterOS username")
    parser.add_argument("--password", default=None, help="password (prompted if omitted)")
    parser.add_argument("--name", required=True, help="fixture name (file stem)")
    parser.add_argument("--no-tls", action="store_true", help="use plain API (8728) instead of TLS")
    parser.add_argument("--port", type=int, default=None, help="override API port")
    parser.add_argument(
        "--phy",
        action="store_true",
        help="also write the feature-075 PHY fixture set (per-menu files: ethernet-monitor.json, "
        "poe-monitor.json, routerboard.json, ethernet.json, bridge-port.json, bridge-vlan.json, "
        "neighbor.json)",
    )
    parser.add_argument(
        "--from-config",
        action="store_true",
        help="fill host/user/password/TLS from obi's 0600 config (no secret on the CLI)",
    )
    parser.add_argument(
        "--snmp-health",
        action="store_true",
        help="capture the feature-096 SNMP health/optics walk fixture instead (read-only "
        "GETBULK; community from obi's config; writes tests/fixtures/snmp/<name>.json)",
    )
    args = parser.parse_args(argv)

    if args.snmp_health:
        if not args.host:
            parser.error("--host is required with --snmp-health")
        out = capture_snmp_health(host=args.host, name=args.name)
        print(f"wrote {out}")
        return 0

    host, user, password, port = args.host, args.user, args.password, args.port
    use_tls = not args.no_tls
    if args.from_config:
        cfg = load_config(default_config_path())
        host = host or (cfg.scope.seeds[0] if cfg.scope.seeds else None)
        user = user or cfg.routeros.username
        password = password or cfg.routeros.password.reveal()  # stays out of argv/history
        port = port or cfg.routeros.api_port
        if not args.no_tls:  # honor config's TLS choice unless --no-tls forces plain
            use_tls = cfg.routeros.use_tls

    if not host or not user:
        parser.error("--host and --user are required unless --from-config is given")
    if not password:
        password = getpass.getpass(f"RouterOS password for {user}@{host}: ")

    out = capture(
        host=host, user=user, password=password, name=args.name, use_tls=use_tls, port=port,
        phy=args.phy,
    )
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

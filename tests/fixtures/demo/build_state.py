"""A synthetic "real" obi state tree for the demo-sanitizer tests (feature 134, T002).

Every value here is FICTIONAL — the network plays the role of "the real network" so the tests can
assert that none of it survives a snapshot. It is built with each store module's own ``connect``
(so the schemas are the real ones) and serializers where obi has them. Nothing in this module may
resemble the operator's real network (no real address range, no real host or event names).

``build_state(root)`` writes ``root/obi/...`` (point ``XDG_STATE_HOME`` at ``root``) and returns a
:class:`SyntheticTruth` listing every planted identifier, by category.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from obi.core import (
    auditstore,
    configstore,
    icmpstore,
    logstore,
    meshstore,
    metricstore,
    radiostore,
    trafficstore,
    weatherstore,
)
from obi.core.models import (
    ArEndpoint,
    ArGeometry,
    ArLink,
    ArSite,
    FrequencyProvenance,
    GroundProvenance,
    HeightProvenance,
    LinkHealth,
)
from obi.core.serialize import ar_geometry_to_json

FIXTURE_DIR = Path(__file__).parent

# --- the fictional "real" network -------------------------------------------------------------

SITES = {
    # site word: (site id octet, lat, lon, ground m)
    "core": (0, 45.0000, -100.0000, 1190.0),
    "northfield": (1, 45.0400, -100.0300, 1192.0),
    "southgate": (2, 44.9600, -99.9800, 1189.0),
    "eastmoor": (3, 45.0100, -99.9400, 1191.0),
    "orchard": (4, 44.9900, -100.0500, 1190.5),
}
UISP_SITE_NAMES = {
    "core": "Core NOC",
    "northfield": "Northfield Relay - East",
    "southgate": "Southgate",
    "eastmoor": "Eastmoor - Kiosk7",
    "orchard": "Orchard",
}

DEVICES = [
    # identity, address, model, serial, mac
    ("core-gw-1", "10.99.0.1", "CCR2004-16G-2S+", "HZ90A1B2C3D", "4C:5E:0C:11:22:01"),
    ("northfield-gw", "10.99.1.1", "RB5009UG+S+", "HZ90A1B2C3E", "4C:5E:0C:11:22:02"),
    ("southgate-gw", "10.99.2.1", "RB5009UG+S+", "HZ90A1B2C3F", "4C:5E:0C:11:22:03"),
    ("eastmoor-gw", "10.99.3.1", "hAP ax3", "HZ90A1B2C40", "4C:5E:0C:11:22:04"),
    ("eastmoor-internet-gw", "10.99.30.1", "hAP ax3", "HZ90A1B2C41", "4C:5E:0C:11:22:05"),
    ("orchard-ap", "10.99.4.20", "cAP ax", "HZ90A1B2C42", "4C:5E:0C:11:22:06"),
]
RADIOS = [
    # name, site word, address, lat, lon, raw altitude
    ("northfield-bh-core", "northfield", "10.99.1.10", 45.0401, -100.0301, 1201.0),
    ("core-bh-northfield", "core", "10.99.0.10", 45.0001, -100.0001, 1199.0),
    ("southgate-bh-core", "southgate", "10.99.2.10", 44.9601, -99.9801, 8.0),
    ("core-bh-southgate", "core", "10.99.0.11", 45.0002, -100.0002, 9.0),
]
PUBLIC_UPLINK = "8.8.4.1"
SLACK_IDS = ("U0FAKE0001", "U0FAKE0002", "C0FAKECHAN")
PERSON = "Quillon"
PERSON_ALIAS = "quill"
EMAIL = "pat@fictional-org.test"
PHONE = "+1 555 010 4477"
SECRETS = {
    "routeros": "fakepass99",
    "snmp": "fakecommunity",
    "uisp": "fake-uisp-token-123",
    "slack_bot": "xoxb-fake-bot-token-0001",
    "slack_app": "xapp-fake-app-token-0001",
    "chat": "sk-ant-fake-key-0001",
    "paging": "fakepagerpw",
    "configs": "fakeconfigpw",
}
MESH_NODE = "!0fa4e001"
MESH_NAME = "Quillon Handheld"
CAMP = "Fictional Camp"
ART = "Imaginary Sculpture"
SSID = "Northfield-Guest"
UPLINK_DOMAIN = "fictional-org.test"
# 136 publish check: values that only ever appear inside RouterOS exports.
WG_PERSON = "marlo quartzbook"  # a WireGuard peer named after a person and their laptop
WG_KEYS = (
    "Zm9vYmFyYmF6cXV1eGNvcmdlZ3JhdWx0Z2FycGx5d2E=",
    "cXV1eGNvcmdlZ3JhdWx0Z2FycGx5d2Fsb29weW1hYzE=",
)
SSID_PLAIN = "Velvet Otter"  # a real SSID made only of ordinary words (also a profile name)
COMMENT_DOMAIN = "quuxhost.test"  # the operator's own domain, only in a firewall comment
CLOUD_UUID = "0a1b2c3d-1111-4222-8333-123456789abc"  # an account-scoped cloud console label

# Window anchors: four days of data, the busiest 72 h is aug 25 00:00 → aug 28 00:00 UTC.
DATA_START = datetime(2026, 8, 24, 0, 0, tzinfo=UTC)
BUSY_START = datetime(2026, 8, 25, 0, 0, tzinfo=UTC)
BUSY_END = BUSY_START + timedelta(hours=72)
DATA_END = DATA_START + timedelta(days=4)


@dataclass
class SyntheticTruth:
    """Every planted real identifier, by category — none may survive a snapshot."""

    ips: set[str] = field(default_factory=set)
    hostnames: set[str] = field(default_factory=set)
    distinctive_words: set[str] = field(default_factory=set)
    dictionary_words: set[str] = field(default_factory=set)
    serials: set[str] = field(default_factory=set)
    macs: set[str] = field(default_factory=set)
    slack_ids: set[str] = field(default_factory=set)
    emails: set[str] = field(default_factory=set)
    phones: set[str] = field(default_factory=set)
    secrets: set[str] = field(default_factory=set)
    persons: set[str] = field(default_factory=set)
    nodes: set[str] = field(default_factory=set)
    event_terms: set[str] = field(default_factory=set)
    coord_texts: set[str] = field(default_factory=set)
    domains: set[str] = field(default_factory=set)
    ssids: set[str] = field(default_factory=set)
    busy_start: datetime = BUSY_START
    busy_end: datetime = BUSY_END
    counts: dict[str, int] = field(default_factory=dict)

    def all_fatal_tokens(self) -> set[str]:
        """Tokens whose literal presence anywhere in a dataset is a leak (not dictionary words)."""

        return (
            self.ips
            | self.hostnames
            | self.distinctive_words
            | self.serials
            | self.macs
            | self.slack_ids
            | self.emails
            | self.phones
            | self.secrets
            | self.persons
            | self.nodes
            | self.coord_texts
            | self.domains
            | self.ssids
        )


def _us(dt: datetime) -> int:
    return int(dt.timestamp() * 1_000_000)


def _iter_times(start: datetime, end: datetime, step: timedelta) -> list[datetime]:
    out: list[datetime] = []
    t = start
    while t < end:
        out.append(t)
        t += step
    return out


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")


# --- builders ---------------------------------------------------------------------------------


def _inventory(state: Path) -> None:
    entries = []
    for identity, address, model, serial, _mac in DEVICES:
        entries.append(
            {
                "address": address,
                "identity": identity,
                "discovery_state": "identified",
                "model": model,
                "version": "7.16.2 (stable)",
                "platform": "MikroTik",
                "provenance": "seed" if identity == "core-gw-1" else f"neighbor-of:{address}",
                "first_seen": DATA_START.isoformat(),
                "last_seen": BUSY_END.isoformat(),
                "present_in_latest": True,
                "serial": serial,
                "alt_addresses": [address.rsplit(".", 1)[0] + ".254"],
            }
        )
    _write_json(
        state / "inventory.json",
        {
            "version": 2,
            "scope": {"ranges": ["10.99.0.0/16"], "seeds": ["10.99.0.1"]},
            "generated_at": BUSY_END.isoformat(),
            "entries": entries,
        },
    )


def _metrics(state: Path, truth: SyntheticTruth) -> None:
    conn = metricstore.connect(state / "metrics.db")
    rows = []
    for t in _iter_times(DATA_START, DATA_END, timedelta(minutes=30)):
        for i, (identity, address, *_rest) in enumerate(DEVICES):
            ts = _us(t)
            octets = int((t - DATA_START).total_seconds()) * 1000 * (i + 1)
            rows.append(
                (
                    ts,
                    t.isoformat(),
                    identity,
                    address,
                    "interfaces",
                    "ifHCInOctets",
                    "1",
                    "ether1",
                    "counter64",
                    octets,
                )
            )
            rows.append(
                (
                    ts,
                    t.isoformat(),
                    identity,
                    address,
                    "system",
                    "cpuLoad",
                    "",
                    None,
                    "gauge",
                    (i * 7 + t.hour) % 100,
                )
            )
            rows.append((ts, t.isoformat(), identity, address, "health", "temperature",
                         "", None, "gauge", 40 + i))  # fmt: skip
    conn.executemany(
        "INSERT INTO samples (ts_us, ts_iso, device, address, profile, metric, ifindex, ifname,"
        " kind, value) VALUES (?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    truth.counts["metrics"] = len(rows)


def _log_messages(t: datetime) -> list[tuple[str, str, str]]:
    """(host, topics, message) lines for one tick: every identifier family appears."""

    return [
        ("northfield-gw", "dhcp,info",
         f"dhcp1 assigned 10.99.1.57 to 4C:5E:0C:AA:BB:01 (host {SSID} client)"),
        ("southgate-gw", "ospf,info",
         "ospf neighbor 10.99.0.1 (core-gw-1) state change to Full via ether1"),
        ("core-gw-1", "system,error,critical",
         f"login failure for user admin from {PUBLIC_UPLINK} via ssh"),
        ("eastmoor-internet-gw", "interface,info",
         "sfp-sfpplus1 link up (speed 10G) uplink to eastmoor-gw"),
        ("orchard-ap", "wireless,info",
         "4c5e.0caa.bb02 connected to orchard-ap, signal -61; back orchard printer online"),
        ("core-gw-1", "snmp,debug",
         "snmp oid 1.3.6.1.4.1.14988.1.1.1 polled; RouterOS 7.16.2 build 1.2.3.4.5"),
        ("northfield-gw", "script,info",
         f"playa uplink check ok at {t:%b/%d/%Y %H:%M:%S}".lower()),
    ]  # fmt: skip


def _logs(state: Path, truth: SyntheticTruth) -> None:
    conn = logstore.connect(state / "logs.db")
    count = 0
    for t in _iter_times(DATA_START, DATA_END, timedelta(minutes=10)):
        busy = BUSY_START <= t < BUSY_END
        lines = _log_messages(t) if busy else _log_messages(t)[:2]
        for host, topics, message in lines:
            ts = _us(t) + count  # unique
            raw = f"{t:%b %e %H:%M:%S} {host} {topics}: {message}"
            key = hashlib.sha256(f"{ts}|{host}|{message}".encode()).hexdigest()
            cur = conn.execute(
                "INSERT INTO logs (ts_us, ts_iso, host, subsystem, topics, severity, message,"
                " raw_line, collectors, dedup_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ts, t.isoformat(), host, topics.split(",")[0], topics,
                 "error" if "error" in topics else None, message, raw, "core,northfield", key),
            )  # fmt: skip
            conn.execute(
                "INSERT INTO logs_fts (rowid, message, host, topics) VALUES (?,?,?,?)",
                (cur.lastrowid, message, host, topics),
            )
            count += 1
    conn.execute(
        "INSERT INTO ingest_state VALUES (?,?,?,?,?)",
        ("/var/log/remote/northfield-gw.log", 1, 1, 1, BUSY_END.isoformat()),
    )
    conn.commit()
    conn.close()
    truth.counts["logs"] = count


def _audit(state: Path) -> None:
    conn = auditstore.connect(state / "audit.db")
    rows = []
    for i, t in enumerate(_iter_times(BUSY_START, BUSY_END, timedelta(hours=6))):
        ts = _us(t)
        rows.extend(
            [
                (ts, t.isoformat(), "device_command", "cli:status", f"s{i}", "northfield-gw",
                 "print /system/resource on northfield-gw",
                 json.dumps({"address": "10.99.1.1", "serial": "HZ90A1B2C3E",
                             "output": "x" * 6000}), 12.0, "ok", None),
                (ts + 1, t.isoformat(), "tool_call", "chat", f"c{i}", "core-gw-1",
                 "routeros_query core-gw-1",
                 json.dumps({"args": {"question": f"ask {PERSON} about core-gw-1"}}),
                 5.0, "ok", None),
                (ts + 2, t.isoformat(), "tool_call", "watch", f"w{i}", "southgate-gw",
                 "logs_query southgate-gw", json.dumps({"args": {"host": "southgate-gw"}}),
                 3.0, "ok", None),
                (ts + 3, t.isoformat(), "llm_call", "slack", f"c{i}", None,
                 f"reply to {SLACK_IDS[0]}", json.dumps({"prompt": f"hi from {EMAIL}"}),
                 900.0, "ok", None),
                (ts + 4, t.isoformat(), "human_message", "radio", f"r{i}", None,
                 f"{PERSON} hailed obi", json.dumps({"text": "where is the orchard"}),
                 None, "ok", None),
                (ts + 6, t.isoformat(), "action", "cli:unifi-logs", f"u{i}",
                 f"unifi:{CLOUD_UUID}.unifi-hosting.{UPLINK_DOMAIN}",
                 f"unifi_logs_ingest → unifi:{CLOUD_UUID}.unifi-hosting.{UPLINK_DOMAIN}",
                 json.dumps({"changed": 0}), None, "ok", None),
                (ts + 5, t.isoformat(), "action", "cli:configs", f"a{i}", "eastmoor-gw",
                 "configs ingest", json.dumps({"actor": SLACK_IDS[0], "changed": 1}),
                 None, "ok", None),
            ]
        )  # fmt: skip
    conn.executemany(
        "INSERT INTO events (ts_us, ts_iso, kind, source, session_id, target, summary, payload,"
        " duration_ms, outcome, error) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    lines = [
        {"timestamp": BUSY_START.isoformat(), "action": "configs_ingest", "target": "eastmoor-gw",
         "reason": "cron", "outcome": "ok", "params": {"address": "10.99.3.1"}, "detail": None},
        {"timestamp": BUSY_START.isoformat(), "action": "slack_reply", "target": "slack",
         "reason": "dm", "outcome": "ok", "params": {}, "detail": None, "actor": SLACK_IDS[1]},
    ]  # fmt: skip
    (state / "audit.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))


def _icmp(state: Path) -> None:
    conn = icmpstore.connect(state / "icmp.db")
    for identity, address, *_rest in DEVICES:
        conn.execute(
            "INSERT INTO device_state (device, address, up, consecutive_misses, last_seen_us,"
            " last_change_us, updated_us, last_rtt_us) VALUES (?,?,?,?,?,?,?,?)",
            (identity, address, 1, 0, _us(BUSY_END), _us(BUSY_START), _us(BUSY_END), 1800),
        )
    conn.commit()
    conn.close()
    _write_json(
        state / "icmp-uisp-targets.json",
        [{"device": name, "address": addr, "maintenance": False} for name, _s, addr, *_ in RADIOS],
    )


def _configs(state: Path) -> None:
    repo = state / "config-repo"
    template = (
        "# {stamp} by RouterOS 7.16.2\n# software id = AB12-CD34\n# model = {model}\n"
        "# serial number = {serial}\n"
        '/interface bridge add name=bridge comment="{site} lan"\n'
        "/ip address add address={address}/24 interface=bridge network={net}.0\n"
        '/ip route add gateway={gw} comment="uplink {upname}"\n'
        "/snmp community set [ find default=yes ] name=" + SECRETS["snmp"] + "\n"
        "/system identity set name={identity}\n"
        "/user add name=pat password=" + SECRETS["routeros"] + " group=full\n"
        '/interface wifi set [ find ] configuration.ssid="' + SSID + '" '
        'security.passphrase="wifi-fake-passphrase"\n'
        '/system note set note="contact {email} {phone}"\n'
        "/interface wireguard peers\n"
        'add allowed-address=10.99.9.2/32 comment="' + WG_PERSON + '" interface=wg1 \\\n'
        '    name=marlo-quartzbook public-key="' + WG_KEYS[0] + '"\n'
        "add allowed-address=0.0.0.0/0 comment=northfield-gw interface=wg2 name=northfield-gw "
        'public-key="' + WG_KEYS[1] + '"\n'
        "/interface wireless security-profiles\n"
        "add authentication-types=wpa2-psk mode=dynamic-keys name=velvet-otter "
        "supplicant-identity=MikroTik\n"
        '/interface wireless set [ find default-name=wlan1 ] ssid="' + SSID_PLAIN + '"\n'
        '/ip firewall filter add action=accept chain=input comment="accept ' + COMMENT_DOMAIN
        + '" src-address=8.8.4.9\n'
        "/ip firewall address-list add address=" + COMMENT_DOMAIN + " list=admin\n"
        '/ip route add gateway=10.99.0.1 comment="{site} orchard relay"\n'
    )
    stamps = [DATA_START + timedelta(hours=2), BUSY_START + timedelta(hours=10),
              BUSY_START + timedelta(hours=40)]  # fmt: skip
    for n, when in enumerate(stamps):
        for identity, address, model, serial, _mac in DEVICES[:3]:
            text = template.format(
                stamp=f"{when:%Y-%m-%d %H:%M:%S}",
                model=model,
                serial=serial,
                site=identity.split("-")[0],
                address=address,
                net=address.rsplit(".", 1)[0],
                gw=PUBLIC_UPLINK if identity == "core-gw-1" else "10.99.0.1",
                upname="core-gw-1",
                identity=identity,
                email=EMAIL,
                phone=PHONE,
            ) + (f"# rev {n}\n" if n else "")
            configstore.record_version(
                repo, identity, text, address=address, router_version="7.16.2", when=when
            )
    _write_json(
        state / "config-captures.json",
        {
            identity: {
                "address": address,
                "last_attempt_at": BUSY_END.isoformat(),
                "last_outcome": "captured_changed",
                "last_success_at": BUSY_END.isoformat(),
                "note": f"captured from {address}",
            }
            for identity, address, *_ in DEVICES
        },
    )


def _geometry() -> ArGeometry:
    sites = [
        ArSite(
            name=UISP_SITE_NAMES[word],
            latitude=lat,
            longitude=lon,
            ground_elevation_m=ground,
            ground_provenance=GroundProvenance.CACHE,
        )
        for word, (_id, lat, lon, ground) in SITES.items()
        if word in ("core", "northfield", "southgate")
    ]
    endpoints = [
        ArEndpoint(
            name=name,
            site=UISP_SITE_NAMES[site],
            latitude=lat,
            longitude=lon,
            raw_altitude=alt,
            height_amsl_m=(alt if alt > 100 else SITES[site][3] + alt),
            height_provenance=HeightProvenance.AMSL if alt > 100 else HeightProvenance.AGL,
            status="active",
        )
        for name, site, _addr, lat, lon, alt in RADIOS
    ]
    links = [
        ArLink(
            id="link-0001",
            name=f"{UISP_SITE_NAMES['northfield']} ↔ {UISP_SITE_NAMES['core']} (60 GHz)",
            a="northfield-bh-core",
            b="core-bh-northfield",
            band="60",
            health=LinkHealth.HEALTHY,
            signal_dbm=-48.0,
            length_m=4800.0,
            frequency_ghz=60.48,
            frequency_provenance=FrequencyProvenance.LINK_CHANNEL,
            fresnel_radius_m=1.2,
        ),
        ArLink(
            id="link-0002",
            name=f"{UISP_SITE_NAMES['southgate']} ↔ {UISP_SITE_NAMES['core']} (5 GHz)",
            a="southgate-bh-core",
            b="core-bh-southgate",
            band="5",
            health=LinkHealth.DEGRADED,
            signal_dbm=-63.0,
            length_m=5100.0,
            frequency_ghz=5.8,
            frequency_provenance=FrequencyProvenance.BAND_NOMINAL,
            fresnel_radius_m=8.1,
        ),
    ]
    for site in sites:
        site.enu = {e.name: (0.0, 0.0, 0.0) for e in endpoints}
    return ArGeometry(
        generated_at=BUSY_END,
        controller=f"https://uisp.{UPLINK_DOMAIN}",
        sites=sites,
        endpoints=endpoints,
        links=links,
    )


def _ar(state: Path) -> None:
    ar = state / "ar"
    ar.mkdir(parents=True, exist_ok=True)
    (ar / "links.json").write_text(ar_geometry_to_json(_geometry()), encoding="utf-8")
    _write_json(ar / "elevations.json", {"45.04,-100.03": 1192.0, "37.76,-122.41": 20.0})
    _write_json(ar / "terrain.json", {"profiles": [], "sites": {}})
    _write_json(ar / "brc.json", {"sites": {CAMP: [1.0, 2.0]}})
    (ar / "ar-server.key").write_text(
        "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n"
    )
    (ar / "terrain-tiles" / "12").mkdir(parents=True, exist_ok=True)
    (ar / "terrain-tiles" / "12" / "1.png").write_bytes(b"\x89PNG fake tile")
    snaps = ar / "snapshots"
    devices = [
        {"identity": identity, "address": address, "model": model, "version": "7.16.2",
         "serial": serial, "reachability": "reachable", "uptime_seconds": 86400}
        for identity, address, model, serial, _mac in DEVICES
    ]  # fmt: skip
    gen = BUSY_END.isoformat()
    _write_json(
        snaps / "status.json",
        {
            "tool": "status",
            "generated_at": gen,
            "data": {
                "scope": {"ranges": ["10.99.0.0/16"], "seeds": ["10.99.0.1"]},
                "devices": devices,
            },
        },
    )
    _write_json(
        snaps / "uisp.json",
        {
            "tool": "uisp",
            "generated_at": gen,
            "data": {
                "controller": f"https://uisp.{UPLINK_DOMAIN}",
                "devices": [
                    {
                        "vendor": "ubiquiti",
                        "identity": name,
                        "address": addr,
                        "model": "AF60-LR",
                        "notes": [f"site {UISP_SITE_NAMES[site]}", f"mac 4C:5E:0C:33:44:{i:02d}"],
                    }
                    for i, (name, site, addr, *_r) in enumerate(RADIOS)
                ],
            },
        },
    )
    _write_json(
        snaps / "uisp-links.json",
        {
            "tool": "uisp_links",
            "generated_at": gen,
            "data": {
                "controller": f"https://uisp.{UPLINK_DOMAIN}",
                "links": [
                    {
                        "id": "link-0001",
                        "label": "northfield-bh-core ↔ core-bh-northfield",
                        "state": "active",
                        "ssid": SSID,
                        "frequency_mhz": 60480.0,
                        "distance_m": 4800.0,
                        "ends": [
                            {"name": "northfield-bh-core", "site": UISP_SITE_NAMES["northfield"]},
                            {"name": "core-bh-northfield", "site": UISP_SITE_NAMES["core"]},
                        ],
                    }
                ],
            },
        },
    )
    _write_json(
        snaps / "siklu.json",
        {
            "tool": "siklu",
            "generated_at": gen,
            "data": {
                "radios": [
                    {
                        "vendor": "siklu",
                        "identity": "eastmoor-eh-core",
                        "address": "10.99.3.40",
                        "serial": "SK0FAKE0001",
                    }
                ],
                "links": [],
            },
        },
    )
    _write_json(
        snaps / "unifi.json",
        {
            "tool": "unifi",
            "generated_at": gen,
            "data": {
                "console": "10.99.0.5",
                "devices": [
                    {"vendor": "ubiquiti", "identity": "orchard-ap", "address": "10.99.4.20"}
                ],
            },
        },
    )
    _write_json(
        snaps / "ospf.json",
        {
            "tool": "ospf",
            "generated_at": gen,
            "data": {
                "meta": {"source_router": "10.99.0.1"},
                "nodes": [
                    {
                        "router_id": address,
                        "identity": identity,
                        "address": address,
                        "site": identity.split("-")[0],
                    }
                    for identity, address, *_ in DEVICES[:4]
                ],
                "edges": [
                    {
                        "id": "e1",
                        "a": "10.99.0.1",
                        "b": "10.99.1.1",
                        "iface_a": "ether2",
                        "iface_b": "ether1",
                        "cost_ab": 10,
                        "cost_ba": 10,
                    },
                    {
                        "id": "e2",
                        "a": "10.99.0.1",
                        "b": "10.99.2.1",
                        "iface_a": "ether3",
                        "iface_b": "ether1",
                        "cost_ab": 10,
                        "cost_ba": 10,
                    },
                    {
                        "id": "e3",
                        "a": "10.99.0.1",
                        "b": "10.99.3.1",
                        "iface_a": "ether4",
                        "iface_b": "ether1",
                        "cost_ab": 20,
                        "cost_ba": 20,
                    },
                ],
            },
        },
    )
    drift = {"netbox": "https://netbox.fictional-org.test"}
    _write_json(
        snaps / "netbox-drift.json", {"tool": "netbox_drift", "generated_at": gen, "data": drift}
    )
    _write_json(
        state / "ospf-layout.json",
        {"10.99.0.1": {"x": 0.0, "y": 0.0}, "10.99.1.1": {"x": 1.0, "y": 0.5},
         "10.99.2.1": {"x": -1.0, "y": 0.5}, "__cloud__": {"x": 0.0, "y": -1.0}},
    )  # fmt: skip


def _dash(state: Path) -> None:
    dash = state / "dash"
    items = [
        {"key": identity, "label": identity, "state": "green", "detail": f"{address} up",
         "metrics": {"rtt_ms": 2.1}, "since_us": _us(BUSY_START)}
        for identity, address, *_ in DEVICES
    ]  # fmt: skip
    _write_json(dash / "board.json", {
        "generated_at": BUSY_END.isoformat(), "generated_at_us": _us(BUSY_END), "state": "green",
        "tiles": [{"signal": "fleet", "state": "green", "items": items, "cadence_s": 60,
                   "data_age_s": 3.0}], "notes": []})  # fmt: skip
    _write_json(dash / "netwatch-state.json", {
        "netwatch": {f"core-gw-1|{PUBLIC_UPLINK}": {"samples": [
            {"at_us": _us(BUSY_START), "done": 1, "failed": 0}]}},
        "ping": {"full_loss_cycles": 0}})  # fmt: skip
    _write_json(dash / "pager-state.json", {"items": {
        "fleet|southgate-gw": {"signal": "fleet", "label": "southgate-gw",
                               "detail": "10.99.2.1 unreachable", "red_since_us": _us(BUSY_START),
                               "observed_us": _us(BUSY_START), "paged_at_us": _us(BUSY_START),
                               "seeded": False}}})  # fmt: skip
    _write_json(dash / "mesh-status.json", {
        "read_at_s": BUSY_END.timestamp(), "reachable": True, "detail": "ok",
        "nodes": [{"node_id": MESH_NODE, "label": MESH_NAME, "last_heard_s": BUSY_END.timestamp(),
                   "snr_db": 6.5, "hops_away": 1}]})  # fmt: skip
    (dash / "run.lock").write_text("")


def _mesh(state: Path) -> None:
    conn = meshstore.connect(state / "meshtastic" / "positions.db")
    for i, t in enumerate(_iter_times(BUSY_START, BUSY_END, timedelta(hours=4))):
        lat, lon = 45.0123 + i * 0.0001, -100.0456
        conn.execute(
            "INSERT INTO position_fix (node_id, lat_udeg, lon_udeg, altitude_m, fix_time_s,"
            " observed_at_s, hops_away, snr_db, address) VALUES (?,?,?,?,?,?,?,?,?)",
            (MESH_NODE, int(lat * 1e7), int(lon * 1e7), 1195, int(t.timestamp()),
             int(t.timestamp()), 1, 5.5, f"7:30 & Esplanade, near {ART}"),
        )  # fmt: skip
    conn.commit()
    conn.close()
    _write_json(state / "meshtastic" / "daemon-status.json", {"node": MESH_NODE})
    (state / "meshtastic" / "daemon.lock").write_text("")


def _weather(state: Path) -> None:
    conn = weatherstore.connect(state / "weather.db")
    for t in _iter_times(DATA_START, DATA_END, timedelta(hours=1)):
        cur = conn.execute(
            "INSERT INTO snapshots (fetched_us, forecast_ok, alerts_ok, temp_f, humidity_pct,"
            " wind_mph) VALUES (?,?,?,?,?,?)",
            (_us(t), 1, 1, 70.0 + t.hour / 2, 12.0, 9.0),
        )
        conn.execute(
            "INSERT INTO hourly (snapshot_id, hour_us, temp_f) VALUES (?,?,?)",
            (cur.lastrowid, _us(t + timedelta(hours=1)), 71.0),
        )
        conn.execute(
            "INSERT INTO alerts (snapshot_id, event, headline) VALUES (?,?,?)",
            (cur.lastrowid, "High Wind Warning", "High Wind Warning for Fictional County"),
        )
    conn.commit()
    conn.close()


def _excluded_stores(state: Path) -> None:
    conn = radiostore.connect(state / "radio.db")
    conn.close()
    conn = trafficstore.connect(state / "traffic.db")
    conn.close()
    _write_json(state / "playa" / "camp.json", [{"name": CAMP, "location_string": "7:30 & E"}])
    _write_json(state / "playa" / "art.json", [{"name": ART, "location": {"gps_latitude": 45.01}}])
    (state / "paging").mkdir(parents=True, exist_ok=True)
    (state / "paging" / "roster.csv").write_text(
        "type,id,name,fullname\n"
        f"person,1001,{PERSON_ALIAS},{PERSON}\n"
        "group,2001,nightcrew,Night Crew\n"
    )
    (state / "watch").mkdir(parents=True, exist_ok=True)
    (state / "watch" / "state.json").write_text(json.dumps({"open_findings": {"core-gw-1": 1}}))
    (state / "metrics.log").write_text("collected core-gw-1 10.99.0.1\n")


def build_state(root: Path) -> SyntheticTruth:
    state = root / "obi"
    state.mkdir(parents=True, exist_ok=True)
    truth = SyntheticTruth()
    _inventory(state)
    _metrics(state, truth)
    _logs(state, truth)
    _audit(state)
    _icmp(state)
    _configs(state)
    _ar(state)
    _dash(state)
    _mesh(state)
    _weather(state)
    _excluded_stores(state)

    truth.ips = {d[1] for d in DEVICES} | {r[2] for r in RADIOS} | {
        PUBLIC_UPLINK, "10.99.1.57", "10.99.3.40", "10.99.0.5"}  # fmt: skip
    truth.hostnames = {d[0] for d in DEVICES if d[0] != "core-gw-1"} | {
        r[0] for r in RADIOS} | {"eastmoor-eh-core"}  # fmt: skip
    truth.distinctive_words = {"northfield", "southgate", "eastmoor"}
    truth.dictionary_words = {"orchard"}
    truth.serials = {d[3] for d in DEVICES} | {"SK0FAKE0001"}
    truth.macs = {d[4] for d in DEVICES} | {"4C:5E:0C:AA:BB:01", "4c5e.0caa.bb02"}
    truth.slack_ids = set(SLACK_IDS)
    truth.emails = {EMAIL}
    truth.phones = {PHONE}
    truth.secrets = set(SECRETS.values()) | {"wifi-fake-passphrase"}
    truth.persons = {PERSON}
    truth.nodes = {MESH_NODE}
    truth.event_terms = {"playa", "Esplanade", CAMP, ART}
    truth.coord_texts = {"45.0123", "-100.0456", "45.0401"}
    truth.domains = {UPLINK_DOMAIN, COMMENT_DOMAIN}
    truth.persons |= {"marlo", "quartzbook"}
    truth.secrets |= {*WG_KEYS, CLOUD_UUID, "fake-unifi-key-0001"}
    truth.ssids = {SSID_PLAIN, "velvet-otter"}
    return truth


def write_config(path: Path, *, output_dir: Path, denylist: Path | None = None) -> Path:
    """Copy the fixture (fixture.toml) to ``path`` with the demo paths filled in."""

    text = (FIXTURE_DIR / "fixture.toml").read_text(encoding="utf-8")
    text = text.replace("@OUTPUT_DIR@", str(output_dir))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    deny = denylist or path.parent / "demo-denylist.txt"
    deny.write_text((FIXTURE_DIR / "denylist.txt").read_text(encoding="utf-8"))
    deny.chmod(0o600)
    return path

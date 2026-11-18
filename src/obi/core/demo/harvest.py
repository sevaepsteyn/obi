"""Harvest every real identifier the dataset must not contain (feature 134, data-model.md).

The harvest is the shared INPUT of the rewriter and the leak gate — never shared code: each
compiles its own matchers from it (research R6), so a rewriter bug cannot also blind the gate.
Sources, all read-only: the raw config (curated identifier and secret keys), ``inventory.json``,
the window's distinct hosts/devices, the web export (sites, endpoints, coordinates), snapshot
JSONs, ICMP targets, dash state, mesh nodes, the excluded stores' people (paging roster, radio
callers) and event data (playa camp/art names), plus the operator's private denylist.

Names are classified per word: a *distinctive* place word (``northfield``) is fatal anywhere; a
*dictionary-word* place (``office``) is fatal only in identifier context (research R5/R6).
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from obi.core.demo import SourceError, open_source_db
from obi.core.demo.ipremap import IPV4_LITERAL
from obi.core.demo.mapping import normalize_mac
from obi.core.demo.naming import GENERIC_TOKENS, is_dictionary_word, place_words

_SECRET_KEY_RE = re.compile(
    r"(pass(word|wd|phrase)?|secret|token|api[_-]?key|community|v3_(auth|priv)_key|psk)$",
    re.IGNORECASE,
)
_SLACK_ID_RE = re.compile(r"^[UWCGD][A-Z0-9]{8,11}$")
_NODE_ID_RE = re.compile(r"^![0-9a-f]{8}$")
_MIN_SECRET = 4
_MIN_SERIAL = 6
#: A UUID used as a hostname label (a UniFi cloud console: ``<uuid>.unifi-hosting.ui.com``) is a
#: unique account identifier: harvested as a secret, so it is redacted everywhere.
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

#: Snapshot / dash JSON keys whose string values are device or site names.
#: Snapshot / dash JSON keys whose string values are AUTHORITATIVE device or site identities.
#: Generic ``name``/``label`` fields are deliberately absent: RouterOS objects named ``default``,
#: ``peer`` or ``first`` are not places (the first live run harvested dozens of such words).
NAME_KEYS = frozenset({"identity", "site", "device", "host", "hostname", "source_router"})
ADDRESS_KEYS = frozenset({"address", "router_id", "console", "ip", "gateway", "a", "b"})
SERIAL_KEYS = frozenset({"serial", "serial_number", "serialnumber"})


def _is_ip(text: str) -> bool:
    """An IPv4/IPv6 address or network. A UniFi name like ``Aspen: Switch - Cedartown`` has a
    colon too, and was once filed as an "address", so its words were never harvested."""

    import ipaddress

    try:
        ipaddress.ip_network(text.strip(), strict=False)
    except ValueError:
        return False
    return True


@dataclass
class Harvest:
    """The real identifier set, by category (casefolded where matching is case-insensitive)."""

    place_words: dict[str, bool] = field(default_factory=dict)  # word → is dictionary word
    names: set[str] = field(default_factory=set)  # full real device/site names, as seen
    persons: set[str] = field(default_factory=set)  # full person strings, casefolded
    person_words: dict[str, bool] = field(default_factory=dict)  # word → is dictionary word
    node_ids: set[str] = field(default_factory=set)
    node_names: dict[str, str] = field(default_factory=dict)  # casefolded name → node id
    ssids: set[str] = field(default_factory=set)
    ips: set[str] = field(default_factory=set)
    networks: set[str] = field(default_factory=set)
    macs: set[str] = field(default_factory=set)  # 12 lower-hex
    serials: set[str] = field(default_factory=set)  # upper
    slack_ids: set[str] = field(default_factory=set)
    phones: set[str] = field(default_factory=set)
    secrets: set[str] = field(default_factory=set)
    domains: set[str] = field(default_factory=set)  # casefolded registrable-ish suffixes
    event_terms: dict[str, bool] = field(default_factory=dict)  # term → fatal
    coords: list[tuple[float, float]] = field(default_factory=list)
    site_coords: list[tuple[float, float]] = field(default_factory=list)
    sources: dict[str, int] = field(default_factory=dict)

    # -- adders -------------------------------------------------------------------------------

    def add_name(self, name: str, dictionary: frozenset[str]) -> None:
        text = name.strip()
        if not text or IPV4_LITERAL.fullmatch(text):
            if text:
                self.ips.add(text)
            return
        self.names.add(text)
        for word in place_words(text):
            self.place_words.setdefault(word, is_dictionary_word(word, dictionary))

    def add_person(self, name: str, dictionary: frozenset[str]) -> None:
        text = name.strip()
        if len(text) < 3 or text.casefold() in GENERIC_TOKENS:
            return
        self.persons.add(text.casefold())
        for word in place_words(text):
            self.person_words.setdefault(word, is_dictionary_word(word, dictionary))

    def add_address(self, value: str) -> None:
        text = value.strip()
        if "/" in text:
            self.networks.add(text)
            self.ips.add(text.split("/")[0])
        elif text:
            self.ips.add(text)

    def add_host(self, value: str, dictionary: frozenset[str]) -> None:
        """A host field: an IP, a DNS name, or a bare device name."""

        text = value.strip()
        if not text:
            return
        if IPV4_LITERAL.fullmatch(text) or _is_ip(text):
            self.add_address(text)
            return
        if "." in text and not text.endswith("."):
            labels = text.split(".")
            if len(labels) >= 2 and labels[-1].isalpha():
                self.domains.add(".".join(labels[-2:]).casefold())
                for label in labels[:-1]:
                    if _UUID_RE.match(label):
                        self.add_secret(label)
                    else:
                        self.add_name(label, dictionary)
                return
        self.add_name(text, dictionary)

    def add_url(self, value: str, dictionary: frozenset[str]) -> None:
        host = urlsplit(value).hostname
        if host:
            self.add_host(host, dictionary)

    def add_secret(self, value: str) -> None:
        if len(value) >= _MIN_SECRET:
            self.secrets.add(value)

    def add_serial(self, value: str) -> None:
        text = value.strip().upper()
        if len(text) >= _MIN_SERIAL and text not in {"UNKNOWN", "NONE"}:
            self.serials.add(text)

    def add_mac(self, value: str) -> None:
        normalized = normalize_mac(value)
        if normalized:
            self.macs.add(normalized)

    def add_event_term(
        self, term: str, *, fatal: bool = False, dictionary: frozenset[str] | None = None
    ) -> None:
        """Record an event/organisation term. With ``dictionary`` (automatic sources: STT
        vocabulary, roster groups, camp listings) a term made only of generic or ordinary words
        is skipped — "router", "security" or "Tech" identify nothing, and treating them as event
        vocabulary rewrote ordinary prose on the first live run. The curated denylist passes no
        dictionary: every operator line is kept as written."""

        text = term.strip().casefold()
        if len(text) < 3:
            return
        if dictionary is not None:
            words = re.findall(r"[a-z]+", text)
            if not words or all(
                w in GENERIC_TOKENS or is_dictionary_word(w, dictionary) for w in words
            ):
                return
        self.event_terms[text] = self.event_terms.get(text, False) or fatal

    def bump(self, source: str, n: int = 1) -> None:
        self.sources[source] = self.sources.get(source, 0) + n

    # -- views --------------------------------------------------------------------------------

    def distinctive_words(self) -> set[str]:
        return {w for w, d in self.place_words.items() if not d} | {
            w for w, d in self.person_words.items() if not d
        }

    def dictionary_words(self) -> set[str]:
        return {w for w, d in self.place_words.items() if d}

    def forbidden_tokens(self) -> set[str]:
        """What no fictional value may equal (fed to :meth:`PseudonymMap.forbid`)."""

        return (
            set(self.place_words)
            | set(self.person_words)
            | {p for p in self.persons}
            | {s.casefold() for s in self.serials}
            # 137: a fake SSID label must never equal a real one
            | {s.casefold() for s in self.ssids}
            | set(self.event_terms)
        )

    def token_count(self) -> int:
        return (
            len(self.place_words)
            + len(self.persons)
            + len(self.node_ids)
            + len(self.ssids)
            + len(self.ips)
            + len(self.macs)
            + len(self.serials)
            + len(self.slack_ids)
            + len(self.secrets)
            + len(self.domains)
            + len(self.event_terms)
        )


# --- config -----------------------------------------------------------------------------------


def _walk(obj: Any, path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], Any]]:
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from _walk(value, (*path, str(key)))
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk(item, (*path, "[]"))
    else:
        yield path, obj


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                yield item


def _harvest_config(raw: dict[str, Any], h: Harvest, words: frozenset[str]) -> None:
    for path, value in _walk(raw):
        if path and path[0] == "demo":
            continue
        if isinstance(value, str) and _SECRET_KEY_RE.search(path[-1]):
            h.add_secret(value)
        if isinstance(value, str) and _SLACK_ID_RE.match(value):
            h.slack_ids.add(value)
        if isinstance(value, str) and (path[-1] == "base_url" or path[-1].endswith("url")):
            h.add_url(value, words)
        if isinstance(value, str) and path[-1].endswith("serial"):
            h.add_serial(value)
    disc = raw.get("discovery", {})
    for value in (*disc.get("ranges", []), *disc.get("seeds", [])):
        h.add_address(str(value))
    for section in ("routeros", "siklu", "paging", "health", "unifi-internal"):
        user = raw.get(section, {}).get("username")
        if isinstance(user, str):
            h.add_person(user, words)
    uisp = raw.get("uisp", {})
    for value in (*_strings(uisp.get("name_filter")), *_strings(uisp.get("ignore_names"))):
        h.add_name(value, words)
    for value in _strings(raw.get("unifi-network", {}).get("sites")):
        h.add_name(value, words)
    logs = raw.get("logs", {})
    for value in _strings(logs.get("collectors")):
        h.add_name(value, words)
    for value in _strings(raw.get("watch", {}).get("ignore_collectors")):
        h.add_name(value, words)
    for key in ("hosts",):
        for section in (raw.get("siklu", {}), raw.get("health", {}).get("linux", {})):
            for value in _strings(section.get(key)):
                h.add_host(value, words)
    for value in _strings(raw.get("icmp", {}).get("extra_hosts")):
        h.add_host(value, words)
    dash = raw.get("dash", {})
    for link in dash.get("internet_links", []) or []:
        for key in ("device", "label"):
            if isinstance(link.get(key), str):
                h.add_name(link[key], words)
    for value in _strings(dash.get("pager", {}).get("extra_sites")):
        h.add_name(value, words)
    for value in _strings(dash.get("ping_refs")):
        h.add_host(value, words)
    maint = raw.get("maintenance", {})
    for key in ("sites", "uisp_sites", "devices"):
        for value in _strings(maint.get(key)):
            h.add_name(value, words)
    web = raw.get("web", {})
    for table in ("site_towers", "ground_overrides"):
        for name in web.get(table) or {}:
            h.add_name(str(name), words)
    radio = raw.get("radio", {})
    for key in ("known_callers", "operator_handles"):
        for value in _strings(radio.get(key)):
            h.add_person(value, words)
    for value in (radio.get("caller_aliases") or {}).values():
        if isinstance(value, str):
            h.add_person(value, words)
    for name in radio.get("pronunciations") or {}:
        h.add_person(str(name), words)
    # Speech-recognition vocabulary is event/jargon wording, not a place: warning-level terms.
    for value in _strings(radio.get("vocab_terms")):
        h.add_event_term(value, dictionary=words)
    for value in (radio.get("speech_aliases") or {}).values():
        if isinstance(value, str):
            h.add_event_term(value, dictionary=words)
    paging = raw.get("paging", {}).get("slack", {})
    for value in (*_strings(paging.get("recipients")), *_strings(paging.get("channels"))):
        if _SLACK_ID_RE.match(value):
            h.slack_ids.add(value)
        else:
            h.add_person(value, words)
    wled = raw.get("wled", {})
    for name, host in (wled.get("devices") or {}).items():
        h.add_name(str(name), words)
        if isinstance(host, str):
            h.add_host(host, words)
    mesh = raw.get("meshtastic", {})
    for node_id, name in (mesh.get("names") or {}).items():
        if _NODE_ID_RE.match(str(node_id)):
            h.node_ids.add(str(node_id))
            if isinstance(name, str):
                h.node_names[name.casefold()] = str(node_id)
                h.add_person(name, words)
    for alias, target in (mesh.get("aliases") or {}).items():
        for value in (str(alias), *_strings(target)):
            h.add_person(value, words)
    for value in _strings(mesh.get("report_only")):
        if _NODE_ID_RE.match(value):
            h.node_ids.add(value)
        else:
            h.add_person(value, words)
    brc = raw.get("brc", {})
    if isinstance(brc.get("origin_lat"), int | float) and isinstance(
        brc.get("origin_lon"), int | float
    ):
        h.coords.append((float(brc["origin_lat"]), float(brc["origin_lon"])))
    weather = raw.get("weather", {})
    if isinstance(weather.get("latitude"), int | float) and isinstance(
        weather.get("longitude"), int | float
    ):
        h.coords.append((float(weather["latitude"]), float(weather["longitude"])))
    h.bump("config")


def harvest_config(raw: dict[str, Any], dictionary: frozenset[str]) -> Harvest:
    """The identifiers in the raw config ALONE — no store, inventory or state file is opened.

    Feature 137's identity guard runs this on every scan and commit, where the full
    :func:`harvest` (multi-GB stores) would be far too slow."""

    h = Harvest()
    _harvest_config(raw, h, dictionary)
    for word in list(h.place_words):
        if word in GENERIC_TOKENS:
            del h.place_words[word]
    return h


# --- stores -----------------------------------------------------------------------------------


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def harvest_json_tree(obj: Any, h: Harvest, words: frozenset[str]) -> None:
    """Snapshot/dash JSON: name-keyed strings, addresses, serials, MACs, lat/lon pairs."""

    if isinstance(obj, dict):
        lat = obj.get("latitude", obj.get("lat"))
        lon = obj.get("longitude", obj.get("lon"))
        if isinstance(lat, int | float) and isinstance(lon, int | float):
            h.coords.append((float(lat), float(lon)))
        for key, value in obj.items():
            lowered = str(key).casefold()
            if isinstance(value, str):
                if lowered in NAME_KEYS:
                    h.add_host(value, words)
                elif lowered in ADDRESS_KEYS:
                    if IPV4_LITERAL.fullmatch(value.split("/")[0]) or value.count(":") >= 2:
                        h.add_address(value)
                elif lowered in SERIAL_KEYS:
                    h.add_serial(value)
                elif lowered in ("mac", "mac_address"):
                    h.add_mac(value)
                elif lowered == "ssid":
                    h.ssids.add(value)
                elif lowered in ("controller", "netbox", "url"):
                    h.add_url(value, words)
                elif lowered == "node_id" and _NODE_ID_RE.match(value):
                    h.node_ids.add(value)
            harvest_json_tree(value, h, words)
    elif isinstance(obj, list):
        for item in obj:
            harvest_json_tree(item, h, words)


def _harvest_inventory(state: Path, h: Harvest, words: frozenset[str]) -> None:
    data = _load_json(state / "inventory.json")
    if not isinstance(data, dict):
        return
    scope = data.get("scope", {})
    for value in (*scope.get("ranges", []), *scope.get("seeds", [])):
        h.add_address(str(value))
    for entry in data.get("entries", []):
        if entry.get("identity"):
            h.add_name(str(entry["identity"]), words)
        if entry.get("address"):
            h.add_address(str(entry["address"]))
        for alt in entry.get("alt_addresses", []) or []:
            h.add_address(str(alt))
        if entry.get("serial"):
            h.add_serial(str(entry["serial"]))
    h.bump("inventory", len(data.get("entries", [])))


def _distinct(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> list[Any]:
    try:
        return [row[0] for row in conn.execute(sql, params)]
    except sqlite3.Error:
        return []


def _harvest_stores(
    state: Path, window: tuple[int, int], h: Harvest, words: frozenset[str]
) -> None:
    lo, hi = window
    metrics = state / "metrics.db"
    if metrics.exists():
        conn = open_source_db(metrics)
        try:
            for device, address in conn.execute(
                "SELECT DISTINCT device, address FROM samples WHERE ts_us BETWEEN ? AND ?",
                (lo, hi),
            ):
                h.add_name(device, words)
                h.add_host(address, words)
                h.bump("metrics")
        finally:
            conn.close()
    logs = state / "logs.db"
    if logs.exists():
        conn = open_source_db(logs)
        try:
            for host in _distinct(
                conn, "SELECT DISTINCT host FROM logs WHERE ts_us BETWEEN ? AND ?", (lo, hi)
            ):
                h.add_host(str(host), words)
                h.bump("logs")
        finally:
            conn.close()
    icmp = state / "icmp.db"
    if icmp.exists():
        conn = open_source_db(icmp)
        try:
            for device, address in conn.execute("SELECT device, address FROM device_state"):
                h.add_name(device, words)
                h.add_host(address, words)
        finally:
            conn.close()
    mesh = state / "meshtastic" / "positions.db"
    if mesh.exists():
        conn = open_source_db(mesh)
        try:
            for node_id, lat, lon in conn.execute(
                "SELECT node_id, lat_udeg, lon_udeg FROM position_fix"
            ):
                h.node_ids.add(str(node_id))
                h.coords.append((lat / 1e7, lon / 1e7))
        finally:
            conn.close()
    radio = state / "radio.db"
    if radio.exists():
        conn = open_source_db(radio)
        try:
            for caller in _distinct(conn, "SELECT DISTINCT caller FROM calls", ()):
                if isinstance(caller, str):
                    h.add_person(caller, words)
        finally:
            conn.close()


def _harvest_files(state: Path, h: Harvest, words: frozenset[str]) -> None:
    links = _load_json(state / "ar" / "links.json")
    if isinstance(links, dict):
        if isinstance(links.get("controller"), str):
            h.add_url(links["controller"], words)
        for site in links.get("sites", []):
            h.add_name(str(site.get("name", "")), words)
            h.site_coords.append((float(site["latitude"]), float(site["longitude"])))
        for endpoint in links.get("endpoints", []):
            h.add_name(str(endpoint.get("name", "")), words)
            h.coords.append((float(endpoint["latitude"]), float(endpoint["longitude"])))
    for path in sorted((state / "ar" / "snapshots").glob("*.json")):
        harvest_json_tree(_load_json(path), h, words)
        h.bump("snapshots")
    for name in ("board.json", "netwatch-state.json", "pager-state.json", "mesh-status.json"):
        data = _load_json(state / "dash" / name)
        harvest_json_tree(data, h, words)
        if name == "pager-state.json" and isinstance(data, dict):
            for key in data.get("items") or {}:
                h.add_name(str(key).split("|", 1)[-1], words)
    targets = _load_json(state / "icmp-uisp-targets.json")
    if isinstance(targets, list):
        for item in targets:
            h.add_name(str(item.get("device", "")), words)
            h.add_host(str(item.get("address", "")), words)
    captures = _load_json(state / "config-captures.json")
    if isinstance(captures, dict):
        for identity, info in captures.items():
            h.add_name(identity, words)
            if isinstance(info, dict) and info.get("address"):
                h.add_host(str(info["address"]), words)
    layout = _load_json(state / "ospf-layout.json")
    if isinstance(layout, dict):
        for key in layout:
            if IPV4_LITERAL.fullmatch(str(key)):
                h.add_address(str(key))
    repo = state / "config-repo"
    if repo.is_dir():
        for rsc in repo.glob("*.rsc"):
            h.add_name(rsc.stem, words)
    # Excluded stores still feed the harvest: their people and places must never leak elsewhere.
    roster = state / "paging" / "roster.csv"
    if roster.exists():
        # type,id,name,fullname — only PERSON rows are people; groups (teams, departments) are
        # organisation vocabulary, so they become warning-level event terms. Pager ids are
        # numeric and would match counts anywhere: not harvested.
        with roster.open(encoding="utf-8", errors="ignore", newline="") as handle:
            for row in csv.DictReader(handle):
                kind = (row.get("type") or "").strip().casefold()
                for column in ("name", "fullname"):
                    value = (row.get(column) or "").strip()
                    if not value:
                        continue
                    if kind == "person":
                        h.add_person(value, words)
                    else:
                        h.add_event_term(value, dictionary=words)
    for name in ("camp.json", "art.json"):
        data = _load_json(state / "playa" / name)
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and isinstance(item.get("name"), str):
                    h.add_event_term(item["name"], dictionary=words)
    audit_jsonl = state / "audit.jsonl"
    if audit_jsonl.exists():
        with audit_jsonl.open(encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                if '"actor"' not in line:
                    continue
                try:
                    actor = json.loads(line).get("actor")
                except ValueError:
                    continue
                if isinstance(actor, str) and _SLACK_ID_RE.match(actor):
                    h.slack_ids.add(actor)
                elif isinstance(actor, str):
                    h.add_person(actor, words)
    mesh_status = _load_json(state / "dash" / "mesh-status.json")
    if isinstance(mesh_status, dict):
        for node in mesh_status.get("nodes", []):
            if node.get("node_id") and node.get("label"):
                h.node_names[str(node["label"]).casefold()] = str(node["node_id"])
                h.node_ids.add(str(node["node_id"]))


# --- RouterOS exports (the config-backup repo) --------------------------------------------------

#: ``key=value`` in one logical export line; a value is quoted (with ``\"`` escapes) or bare.
_ROS_KV = re.compile(r'(?<![\w.-])([a-z][\w.-]*)=("(?:[^"\\]|\\.)*"|[^\s"]+)')
#: A dotted DNS-shaped token with an alphabetic TLD (not RouterOS ``a.b=`` property paths, which
#: never appear inside a value).
_DOMAIN_TOKEN = re.compile(r"(?<![\w.-])((?:[a-z0-9-]+\.)+[a-z]{2,24})(?![\w.-])", re.IGNORECASE)
#: Public infrastructure names that identify nobody (kept as written; never harvested).
PUBLIC_DOMAINS = (
    "pool.ntp.org", "ntp.org", "mikrotik.com", "routerboard.com", "example.com", "example.net",
    "example.org", "google.com", "cloudflare.com", "quad9.net", "opendns.com",
)  # fmt: skip
#: Values that name a domain/host inside these keys (a comment can too).
_HOSTISH_KEYS = frozenset({"address", "name", "host", "dns-name", "servers", "server", "comment",
                           "endpoint-address", "regexp", "url"})  # fmt: skip
_PEER_PLACEHOLDER = re.compile(r"^(peer|client|wg|user)[-_]?\d*$", re.IGNORECASE)
_KEY_KEYS = frozenset({"public-key", "private-key", "preshared-key"})


def _logical_lines(text: str) -> Iterator[str]:
    """An ``/export`` with its ``\\``-newline continuations joined, section paths inherited."""

    section = ""
    for line in re.sub(r"\\\n\s*", "", text).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("/"):
            head, _, rest = stripped.partition(" ")
            parts = [head]
            for word in rest.split():
                if word in ("add", "set", "remove") or "=" in word or word.startswith("["):
                    break
                parts.append(word)
            section = " ".join(parts)
            yield stripped
        else:
            yield f"{section} {stripped}"


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return value


def _is_public_domain(domain: str) -> bool:
    d = domain.casefold()
    return any(d == p or d.endswith("." + p) for p in PUBLIC_DOMAINS)


def harvest_routeros_export(text: str, h: Harvest, words: frozenset[str]) -> None:
    """People, SSIDs, keys and domains that only ever appear inside device configs.

    The first live publish check found WireGuard peers named after people and their laptops,
    real SSIDs (also reused as security-profile names), WireGuard public keys and the operator's
    own domain in firewall comments — none of which any other source names.
    """

    for line in _logical_lines(text):
        kv = {k.casefold(): _unquote(v) for k, v in _ROS_KV.findall(line)}
        wireguard_peer = line.startswith("/interface wireguard peers") or "public-key" in kv
        for key, value in kv.items():
            if key in _KEY_KEYS or key.endswith("-key") and len(value) >= 40:
                h.add_secret(value)
            elif key == "ssid" or key.endswith(".ssid"):
                _add_ssid(h, value)
            if key in _HOSTISH_KEYS or key.endswith("-address"):
                for m in _DOMAIN_TOKEN.finditer(value):
                    if not _is_public_domain(m.group(1)) and not IPV4_LITERAL.fullmatch(
                        m.group(1)
                    ):
                        h.add_host(m.group(1), words)
        if wireguard_peer:
            for key in ("name", "comment"):
                if key in kv:
                    _add_peer_label(h, kv[key], words)
        security_profile = line.startswith(
            ("/interface wireless security-profiles", "/interface wifi security")
        ) or ("authentication-types" in kv and "name" in kv)
        if security_profile and "name" in kv and kv["name"] != "default":
            _add_ssid(h, kv["name"])
    h.bump("routeros_exports")


def _add_ssid(h: Harvest, value: str) -> None:
    text = value.strip()
    if len(text) < 4:
        return
    h.ssids.add(text)
    slug = re.sub(r"[\s_]+", "-", text)
    h.ssids.update({slug, slug.casefold(), text.casefold()})


def _add_peer_label(h: Harvest, value: str, words: frozenset[str]) -> None:
    """A WireGuard peer's name/comment: a device or site (left to the name passes) or a PERSON
    and their machine ("quillon mae", "quill-mobile")."""

    text = value.strip()
    folded = text.casefold()
    if not text or folded == "obi" or _PEER_PLACEHOLDER.match(text) or IPV4_LITERAL.match(text):
        return
    tokens = re.findall(r"[a-z0-9]+", folded)
    if any(t in GENERIC_TOKENS for t in tokens) or text in h.names or any(
        t in h.place_words for t in tokens
    ):
        return  # a site tunnel ("siteb-internet-gw") or a known device
    h.add_person(text, words)
    spaced = re.sub(r"[-_]+", " ", text)
    if spaced != text:
        h.add_person(spaced, words)
    # The first word is the person; machine words ("mercury") are ordinary or device-ish.
    if tokens and len(tokens[0]) >= 3:
        h.add_person(tokens[0], words)


def _harvest_config_repo(
    state: Path, window: tuple[int, int], h: Harvest, words: frozenset[str]
) -> None:
    """Every export version the configs source can publish: the baseline + in-window commits."""

    from datetime import UTC, datetime

    from obi.core import configstore

    repo = state / "config-repo"
    if not (repo / ".git").exists():
        return
    end_of_time = 253_402_300_799 * 10**6  # 9999-12-31: callers pass (0, 2**62) for "all"
    lo, hi = (min(max(t, 0), end_of_time) for t in window)
    since = datetime.fromtimestamp(lo / 1e6, tz=UTC)
    until = datetime.fromtimestamp(hi / 1e6, tz=UTC)
    texts: list[str] = []
    baseline = configstore.last_commit_before(repo, since)
    if baseline is not None:
        texts.extend(configstore.tree_files(repo, baseline).values())
    for sha, _when in configstore.commits_between(repo, since, until):
        texts.extend(t for t in configstore.changed_files(repo, sha).values() if t is not None)
    for rsc in sorted(repo.glob("*.rsc")):  # the working tree (latest), for the router view
        texts.append(rsc.read_text(encoding="utf-8", errors="ignore"))
    for text in texts:
        harvest_routeros_export(text, h, words)


def read_denylist(path: Path, h: Harvest) -> None:
    """``term`` lines are WARNING event terms; ``!term`` lines are FATAL (people, handles);
    ``@term`` lines are real PLACE/organisation words (an ISP, a site nickname) that no source
    names on its own: mapped like a site word and fatal anywhere."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SourceError(f"could not read the private denylist: {exc}") from exc
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if text.startswith("@"):
            word = text[1:].strip().casefold()
            if len(word) >= 2:
                h.place_words[word] = False
        elif text.startswith("!"):
            h.add_event_term(text[1:], fatal=True)
        else:
            h.add_event_term(text)


def harvest(
    raw_config: dict[str, Any],
    state_dir: Path,
    window: tuple[int, int],
    denylist_path: Path,
    dictionary: frozenset[str],
) -> Harvest:
    """Collect the real identifier set for one snapshot (read-only on every source)."""

    h = Harvest()
    _harvest_config(raw_config, h, dictionary)
    _harvest_inventory(state_dir, h, dictionary)
    _harvest_stores(state_dir, window, h, dictionary)
    _harvest_files(state_dir, h, dictionary)
    _harvest_config_repo(state_dir, window, h, dictionary)
    read_denylist(denylist_path, h)
    # Event terms are matched as phrases; a fatal one that is also a name stays fatal.
    for word in list(h.place_words):
        if word in GENERIC_TOKENS:
            del h.place_words[word]
    return h

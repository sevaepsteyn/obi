"""The fictional naming scheme (feature 134, research R2): tree words for places, generic labels
for people / mesh nodes / SSIDs, and role tokens kept verbatim.

Names are rewritten at the WORD level: a hostname, UISP site name or dash label is split into
alphabetic runs; every *place word* (a non-generic token harvested from a real name) becomes
its tree word, while role/vendor/direction tokens (``gw``, ``ap``, ``core``, ``af60``, ``east``)
and digits are kept. ``northfield-gw-01`` and the UISP site ``Northfield`` therefore both read
``birch``
— consistent everywhere — and the device role cue survives (FR-006).

Words that are also ordinary English (``office``, ``hotel``, ``mountain``) are *dictionary-word*
names: rewritten only in identifier context, never in prose (FR-008, analyze C1).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from functools import lru_cache
from pathlib import Path

#: Place replacements. Vendor-colliding words (juniper, cisco, arista) are deliberately absent.
# fmt: off
TREES: tuple[str, ...] = (
    "aspen", "birch", "cedar", "cottonwood", "alder", "willow", "pinyon", "sage", "tamarack",
    "larch", "hemlock", "spruce", "maple", "oak", "elm", "beech", "hazel", "holly", "laurel",
    "linden", "poplar", "redwood", "sequoia", "cypress", "fir", "pine", "yew", "hickory", "walnut",
    "chestnut", "sycamore", "magnolia", "dogwood", "hawthorn", "rowan", "buckeye", "catalpa",
    "locust", "mesquite", "ironwood", "ocotillo", "manzanita", "madrone", "buckthorn",
    "chokecherry", "elderberry", "serviceberry", "sumac", "sassafras", "tupelo", "basswood",
    "boxelder", "bristlecone", "foxtail", "ponderosa", "lodgepole", "whitebark", "bigtooth",
    "blackgum", "bluebeech", "butternut", "hornbeam", "ironbark", "mulberry", "persimmon",
    "quaking", "smoketree", "sweetgum", "tanoak", "torreya", "wingnut", "yellowwood", "zelkova",
    "paloverde", "bitterbrush", "greasewood", "rabbitbrush", "saltbush", "creosote", "joshua",
)
# fmt: on

#: Tokens that identify nothing on their own: device roles, vendors/models, directions, protocol
#: and infrastructure vocabulary. Kept verbatim in renamed identifiers, never harvested.
# fmt: off
GENERIC_TOKENS: frozenset[str] = frozenset({
    # function words and plain adjectives a site name may contain ("old-office", "not-connected"
    # in prose): mapping them garbles replayed text and identifies nothing (2026-11-17 review).
    "not", "where", "was", "new", "old", "back", "available", "missing", "update", "inside",
    "outside", "short", "way", "common", "front", "middle", "rear", "offline", "online",
    "outdoor", "indoor", "inventory",
    "ac", "accept", "access", "active", "address", "addresses", "admin", "af", "air", "airfiber",
    "airmax", "all", "and", "any", "ap", "api", "aps", "area", "auto", "aux", "ax", "backbone",
    "backup", "bgp", "bh", "board", "bond", "bottom", "bridge", "cambium", "cap", "capacity", "obi",
    "cli", "cron", "daemon", "launchd", "com", "org", "edu", "gov", "io", "arpa", "int",
    "ccr", "cell", "channel", "client", "clients", "collector", "collectors", "combo", "console",
    "controller", "core", "cpe", "cpu", "critical", "crs", "debug", "default", "device", "devices",
    "dhcp", "disabled", "disk", "distance", "dns", "down", "downlink", "drop", "dstnat", "dummy",
    "dynamic", "east", "edge", "eh", "enabled", "eoip", "error", "eth", "ether", "etherhaul",
    "ethernet", "false", "fan", "fiber", "fibre", "filter", "firmware", "forward", "frequency",
    "from", "ftp", "gateway", "gen", "gps", "grafana", "gre", "guest", "gw", "hap", "hd", "health",
    "hex", "host", "hosts", "http", "https", "hub", "identity", "iface", "imap", "inactive",
    "influx", "info", "inner", "input", "instance", "interface", "interfaces", "internet", "iot",
    "ipsec", "it", "lab", "lan", "leaf", "left", "lhg", "link", "links", "list", "lite",
    "litebeam", "lo", "local", "log", "logs", "long", "loop", "loopback", "lower", "lr", "lsa",
    "lsdb", "lte", "ltu", "mail", "main", "management", "mangle", "masquerade", "mast", "master",
    "memory", "mesh", "metrics", "mgmt", "mikrotik", "mimosa", "model", "monitor", "mpls", "nano",
    "nanobeam", "nanostation", "nat", "neighbor", "neighbors", "net", "netbox", "network",
    "networks", "nginx", "nms", "no", "noc", "node", "nodes", "none", "north", "ntp", "of", "ok",
    "ops", "optics", "ospf", "outer", "output", "overview", "ovpn", "package", "platform", "poe",
    "pole", "pool", "port", "portal", "ports", "power", "powerbeam", "pppoe", "prefix", "primary",
    "prism", "private", "pro", "prometheus", "psu", "ptmp", "ptp", "public", "rack", "radio",
    "radios", "raw", "rb", "relay", "remote", "repeater", "resource", "right", "rocket", "roof",
    "route", "router", "routerboard", "routeros", "routers", "routes", "rtr", "running",
    "secondary", "sector", "serial", "server", "sfp", "sfpplus", "signal", "siklu", "site",
    "sites", "slave", "smtp", "snmpd", "south", "spine", "spoke", "srcnat", "srv", "ssh", "ssid",
    "sta", "stable", "state", "static", "station", "status", "storage", "sw", "switch", "switches",
    "sxt", "syslog", "syslogd", "system", "temp", "temperature", "term", "test", "testing", "the",
    "to", "top", "tower", "true", "trunk", "uap", "ubiquiti", "ubnt", "udm", "ui", "uisp", "unifi",
    "unknown", "up", "upgrade", "uplink", "upper", "uptime", "user", "usg", "usw", "version",
    "via", "victoria", "vlan", "vm", "vms", "voltage", "vpn", "vrrp", "wan", "wap", "warning",
    "wave", "web", "west", "width", "wifi", "wireless", "wlan", "www", "xr", "yes",
})
# fmt: on

_WORD_RE = re.compile(r"[A-Za-z]+")
_MIN_PLACE_WORD = 3

#: A minimal vendored dictionary for hosts without ``/usr/share/dict/words``.
_VENDORED_WORDS = Path(__file__).with_name("data") / "common_words.txt"


def is_generic(token: str) -> bool:
    return token.casefold() in GENERIC_TOKENS


def place_words(name: str) -> list[str]:
    """The casefolded place words of a real name (generic tokens, digits, short runs dropped)."""

    out: list[str] = []
    for match in _WORD_RE.finditer(name):
        word = match.group(0).casefold()
        if len(word) >= _MIN_PLACE_WORD and word not in GENERIC_TOKENS and word not in out:
            out.append(word)
    return out


def _match_case(template: str, word: str) -> str:
    if template.isupper() and len(template) > 1:
        return word.upper()
    if template[:1].isupper():
        return word[:1].upper() + word[1:]
    return word


_CAMEL_PART = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+")


def camel_parts(run: str) -> list[str]:
    """``NorthfieldWillow`` → ``["Northfield", "Willow"]``; ``frontTV`` → ``["front", "TV"]``."""

    return _CAMEL_PART.findall(run)


def rewrite_words(text: str, lookup: Callable[[str], str | None]) -> str:
    """Replace each alphabetic run whose casefold ``lookup`` maps, matching the run's case.

    A run that does not map as a whole is tried part by part on its CamelCase boundaries, so
    a DHCP client name like ``NorthfieldWillow`` rewrites both of its places.
    """

    def sub(match: re.Match[str]) -> str:
        run = match.group(0)
        mapped = lookup(run.casefold())
        if mapped is not None:
            return _match_case(run, mapped)
        parts = camel_parts(run)
        if len(parts) < 2 or "".join(parts) != run:
            return run
        out = []
        for part in parts:
            hit = lookup(part.casefold())
            out.append(part if hit is None else _match_case(part, hit))
        return "".join(out)

    return _WORD_RE.sub(sub, text)


#: Words the system list only has capitalised (as names) that are everyday technical or role
#: vocabulary in obi's data: ``max``/``min`` fields, a "greeter mod" role, a title. A full name
#: containing one is still caught by its other words.
TECHNICAL_WORDS: frozenset[str] = frozenset({"max", "min", "mod", "mrs", "avg"})


@lru_cache(maxsize=4)
def load_dictionary(path: str = "/usr/share/dict/words") -> frozenset[str]:
    """Ordinary English words: the system list, else the vendored fallback.

    Only entries the list spells in lower case count. The system list also carries proper
    nouns (``Mabel``, ``Oswald``, ``Agnes``); casefolding those made a person's first name look
    like an ordinary word, so it was never treated as identifying (the 136 publish check).
    """

    for candidate in (Path(path), _VENDORED_WORDS):
        try:
            text = candidate.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        return TECHNICAL_WORDS | frozenset(
            word for word in (line.strip() for line in text.splitlines()) if word and word.islower()
        )
    return TECHNICAL_WORDS


def is_dictionary_word(word: str, dictionary: Iterable[str] | frozenset[str]) -> bool:
    """True for an ordinary English word, plurals included (``showers`` → ``shower``)."""

    words = dictionary if isinstance(dictionary, frozenset) else frozenset(dictionary)
    w = word.casefold()
    if w in words:
        return True
    if w.endswith("es") and w[:-2] in words:
        return True
    return w.endswith("s") and w[:-1] in words


def within_one_edit(a: str, b: str) -> bool:
    """Damerau–Levenshtein distance ≤ 1 (substitution, insertion, deletion, transposition)."""

    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        diffs = [i for i in range(la) if a[i] != b[i]]
        if len(diffs) == 1:
            return True
        return (
            len(diffs) == 2
            and diffs[1] == diffs[0] + 1
            and a[diffs[0]] == b[diffs[1]]
            and a[diffs[1]] == b[diffs[0]]
        )
    short, long_ = (a, b) if la < lb else (b, a)
    return any(long_[:i] + long_[i + 1 :] == short for i in range(len(long_)))


def person_label(index: int) -> str:
    """``admin`` for the first person, then ``user-1``, ``user-2``… — built only from generic
    tokens, which are never harvested, so a persona can never collide with a real word (the first
    live run found ``staff`` among the real organisation's own vocabulary)."""

    return "admin" if index == 0 else f"user-{index}"


def node_label(index: int) -> str:
    return f"node-{index + 1:02d}"


def ssid_label(index: int) -> str:
    return f"evn-net-{index + 1:02d}"


def domain_label(index: int) -> str:
    fixed = ("example.net", "example.org", "example.com")
    return fixed[index] if index < len(fixed) else f"d{index}.example.net"


def place_label(index: int) -> str:
    """The ``index``-th place word: tree names, then numbered trees once they run out."""

    base = TREES[index % len(TREES)]
    rnd = index // len(TREES)
    return base if rnd == 0 else f"{base}{rnd + 1}"

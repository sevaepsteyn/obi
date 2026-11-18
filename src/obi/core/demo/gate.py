"""The fail-closed leak gate (feature 134, FR-011, research R6).

An INDEPENDENT scanner over a staged dataset: it compiles its own matchers from the
:class:`~obi.core.demo.harvest.Harvest` and shares no code with the rewriter, so a rewriter bug
cannot also blind it. It reads every container obi writes — every SQLite cell plus each FTS5
table's vocabulary, every JSON key and value, every object in every git repository (reachable or
not) plus the repo's plain files, text files line by line, and raw bytes of everything else — and
reports each finding as ``(severity, category, check, file, locator, offset)``. A finding NEVER
carries the token itself, so a report is safe to paste anywhere (US2-3).

Severity: FATAL by default. Event-identifying denylist/playa terms are WARNINGS unless marked
``!`` or ``strict_event_terms`` is set; a dictionary-word name (``office``) is FATAL in
identifier context (a hostname-shaped token or a name field) and, in prose, when capitalised or
one of two or more places in a comma list (``names_in_prose``, off for the 137 source guard);
plain lower-case prose use is a WARNING.
"""

from __future__ import annotations

import ipaddress
import json
import re
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from obi.core import configstore
from obi.core.demo.harvest import Harvest
from obi.core.demo.ipremap import IPV4_LITERAL, IPV6_CANDIDATE, is_allowed_v4, is_allowed_v6
from obi.core.demo.naming import (
    GENERIC_TOKENS,
    camel_parts,
    is_dictionary_word,
    load_dictionary,
    within_one_edit,
)
from obi.core.demo.relocate import COORD_PAIR, haversine_m
from obi.core.demo.trie import trie_pattern

GATE_VERSION = 2
FATAL = "FATAL"
WARN = "WARN"

_COORD_FENCE_M = 200_000.0
_ALLOWED_EMAIL_DOMAINS = ("example.net", "example.org", "example.com")

_HOSTLIKE = re.compile(r"[A-Za-z0-9]+(?:[-_]+[A-Za-z0-9]+)+")
_ALPHA = re.compile(r"[a-z]+")
_MAC_SEP = re.compile(
    r"(?<![0-9A-Fa-f:-])([0-9A-Fa-f]{2})([:-])(?:[0-9A-Fa-f]{2}\2){4}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:-])"
)
_MAC_DOT = re.compile(
    r"(?<![0-9A-Fa-f.])([0-9A-Fa-f]{2})[0-9A-Fa-f]{2}(?:\.[0-9A-Fa-f]{4}){2}(?![0-9A-Fa-f.])"
)
_MAC_BARE = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{12}(?![0-9A-Fa-f])")
_SECRET_SHAPE = re.compile(
    r"sk-ant-[A-Za-z0-9_-]{4,}|xox[abprs]-[A-Za-z0-9-]{4,}|xapp-[A-Za-z0-9-]{4,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)
#: A WireGuard key (32 bytes, base64) as a RouterOS ``*-key=`` value, quoted or escaped in JSON.
_WG_KEY_SHAPE = re.compile(
    r"(?i)(?:public|private|preshared)-key=\\?\"?[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]="
)
#: An IANA zone region right before a name ("America/" + "New_York"): a setting, not a place.
_TZ_REGION = re.compile(
    r"(?:Africa|America|Antarctica|Asia|Atlantic|Australia|Europe|Indian|Pacific|US|Etc)"
    r"/(?:[A-Za-z_]+/)?\Z"
)
#: The rewriter's own event wording (own copy, pinned equal to the rewriter's by a test): a
#: capitalised word inside one of these phrases is generated text, never a real name.
EQUIVALENT_PHRASES: tuple[str, ...] = (
    "overflow camping", "valley floor", "back country", "survey marker", "town center",
    "main road", "the valley", "the event", "the town", "landmark",
)  # fmt: skip
_EQUIV = re.compile(
    r"(?i)(?<![\w])(?:" + "|".join(re.escape(p) for p in EQUIVALENT_PHRASES) + r")(?![\w])"
)
_CAPITALISED = re.compile(r"(?<![A-Za-z])[A-Z][A-Za-z]*(?![A-Za-z])")
#: A comma list of short items (up to four words each), "and"/"or" allowed before an item.
_COMMA_LIST = re.compile(
    r"[A-Za-z][\w-]*(?:[ \t]+[A-Za-z][\w-]*){0,3}"
    r"(?:[ \t]*,[ \t]*(?:(?:and|or)[ \t]+)?[A-Za-z][\w-]*(?:[ \t]+[A-Za-z][\w-]*){0,3})+"
)
_COMMA_ITEM = re.compile(r"[^,]+")
#: An AS number; only the documentation/private ranges may appear in a dataset.
_ASN_SHAPE = re.compile(r"(?<![\w-])ASN?\s?(\d{1,10})(?![\d\w])")


def is_reserved_asn(number: int) -> bool:
    """RFC 5398 documentation + RFC 6996 private ASNs (own copy: the gate shares no code with
    the rewriter)."""

    return 64496 <= number <= 65534 or 4_200_000_000 <= number <= 4_294_967_294


#: A UUID used as a hostname label (an account-scoped cloud console, e.g. UniFi's hosted one).
_UUID_HOST_SHAPE = re.compile(
    r"(?i)(?<![0-9a-f-])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.[a-z]"
)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
_SLACK_SHAPE = re.compile(
    r"(?<![A-Za-z0-9])[UW](?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{8,10}(?![A-Za-z0-9])"
)

#: SQLite columns and JSON keys whose values are identifiers (dictionary words are FATAL there).
_ID_FIELDS = frozenset(
    {
        "host", "hostname", "device", "identity", "target", "site", "node_id", "address",
        "label", "name", "router_id", "source_router", "controller", "console", "a", "b",
        "location",
    }
)  # fmt: skip
#: A name field serialized INSIDE a text value (a tool result replayed in a chat recording).
_QUOTED_ID_KV = re.compile(
    r'"(host|hostname|device|identity|target|site|label|name|location)"\s*:\s*"((?:[^"\\]|\\.)*)"',
    re.IGNORECASE,
)
_FTS_SHADOW = ("_data", "_idx", "_docsize", "_config", "_content")
_SQLITE_MAGIC = b"SQLite format 3\x00"
_BINARY_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".wav", ".pdf", ".bin"})


@dataclass(frozen=True)
class LeakFinding:
    """One redacted finding — deliberately has no field that could hold the token."""

    severity: str
    category: str
    check: str
    file: str
    locator: str
    offset: int


@dataclass
class GateResult:
    findings: list[LeakFinding] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def fatal(self) -> list[LeakFinding]:
        return [f for f in self.findings if f.severity == FATAL]

    @property
    def warnings(self) -> list[LeakFinding]:
        return [f for f in self.findings if f.severity == WARN]


def format_report(result: GateResult, *, limit: int = 200) -> str:
    """The redacted report: one line per finding, never a token (safe to paste publicly)."""

    lines = [
        f"leak gate v{GATE_VERSION}: {len(result.fatal)} fatal, {len(result.warnings)} warning(s)"
    ]
    # The whole picture first — counts per (severity, category/check, file) — then examples.
    totals: dict[tuple[str, str, str], int] = {}
    for f in result.findings:
        key = (f.severity, f"{f.category}/{f.check}", f.file)
        totals[key] = totals.get(key, 0) + 1
    for (severity, kind, file), n in sorted(
        totals.items(), key=lambda kv: (kv[0][0] != FATAL, -kv[1], kv[0])
    ):
        lines.append(f"  {n:>9}  [{severity}] {kind} {file}")
    lines.append("examples:")
    ordered = sorted(result.findings, key=lambda f: (f.severity != FATAL, f.file, f.locator))
    for finding in ordered[:limit]:
        lines.append(
            f"[{finding.severity}] {finding.category}/{finding.check} {finding.file} "
            f"{finding.locator} @{finding.offset}"
        )
    if len(ordered) > limit:
        lines.append(f"… {len(ordered) - limit} more")
    return "\n".join(lines)


def tree_names(body: bytes) -> list[str]:
    """The entry names of a raw git tree object (``<mode> <name>\\0<20-byte hash>`` repeated)."""

    names: list[str] = []
    pos = 0
    while pos < len(body):
        space = body.index(b" ", pos)
        nul = body.index(b"\0", space)
        names.append(body[space + 1 : nul].decode("utf-8", errors="replace"))
        pos = nul + 1 + 20
    return names


class LeakGate:
    """Scan a dataset tree for any surviving real identifier (fail-closed)."""

    def __init__(
        self,
        harvest: Harvest,
        *,
        strict_event_terms: bool = False,
        dictionary: frozenset[str] | None = None,
        exempt_values: frozenset[str] = frozenset(),
        names_in_prose: bool = True,
    ) -> None:
        self.strict = strict_event_terms
        self._names_in_prose = names_in_prose
        self._dictionary = load_dictionary() if dictionary is None else dictionary
        self._token_category: dict[str, str] = {}
        for word, is_dict in harvest.place_words.items():
            if not is_dict:
                self._token_category[word] = "place"
        for word, is_dict in harvest.person_words.items():
            if not is_dict:
                self._token_category.setdefault(word, "person")
        dictionary_ = load_dictionary() if dictionary is None else dictionary
        for person in harvest.persons:
            words = _ALPHA.findall(person)
            # A name made only of ordinary/generic words ("Dash") is not a fatal token: it would
            # fire on every `[dash]` section and "dash" in prose.
            if words and not all(
                w in GENERIC_TOKENS or is_dictionary_word(w, dictionary_) for w in words
            ):
                self._token_category.setdefault(person, "person")
        for value, category in (
            *((n.casefold(), "node") for n in harvest.node_ids),
            *((s.casefold(), "ssid") for s in harvest.ssids if len(s) >= 4),
            *((s.casefold(), "serial") for s in harvest.serials),
            *((s.casefold(), "slack_id") for s in harvest.slack_ids),
            *((s.casefold(), "secret") for s in harvest.secrets),
            *((d, "domain") for d in harvest.domains),
            *((p.casefold(), "phone") for p in harvest.phones),
        ):
            self._token_category[value] = category
        for term, fatal in harvest.event_terms.items():
            if fatal:  # a more specific category (person, place…) wins over "denylist"
                self._token_category.setdefault(term, "denylist")
        self._token_re = self._alternation(self._token_category)
        self._secrets = sorted(s for s in harvest.secrets if len(s) >= 6)
        # Only dictionary-word PLACES are enforced in identifier context. People are matched by
        # their full name; a nickname that is an ordinary word ("peer", "fence") would otherwise
        # make that word fatal in every interface name (the first live run).
        self._dict_words = frozenset(w for w, d in harvest.place_words.items() if d)
        self._exempt = frozenset(v.casefold() for v in exempt_values)
        self._warn_terms = {
            t: f
            for t, f in harvest.event_terms.items()
            if not f and not all(w in GENERIC_TOKENS for w in _ALPHA.findall(t))
        }
        self._warn_re = self._alternation(self._warn_terms)
        self._real_ips = {ip for ip in harvest.ips}
        self._real_macs = frozenset(harvest.macs)
        self._fuzzy_targets = sorted(
            w
            for w, cat in self._token_category.items()
            if cat in ("place", "person") and len(w) >= 5 and w.isalpha()
        )
        coords = harvest.site_coords or harvest.coords
        self._centroid = (
            (sum(c[0] for c in coords) / len(coords), sum(c[1] for c in coords) / len(coords))
            if coords
            else None
        )
        self._words_seen: dict[str, tuple[str, str]] = {}
        self.harvested_tokens = harvest.token_count()
        self.denylist_terms = len(harvest.event_terms)

    @staticmethod
    def _alternation(tokens: dict[str, Any]) -> re.Pattern[str] | None:
        # Long tokens match as substrings (catches `northfieldgw`); short ones need edges. An
        # underscore is an edge-blocker too, as in the rewriter's person pass: a short first name
        # ("max") inside a snake_case key (`max_window_events`) is code, not a person.
        long_src = trie_pattern(t for t in tokens if len(t) >= 6)
        short_src = trie_pattern(t for t in tokens if len(t) < 6)
        parts = []
        if long_src:
            parts.append(long_src)
        if short_src:
            parts.append(rf"(?<![a-z0-9_]){short_src}(?![a-z0-9_])")
        return re.compile("|".join(parts)) if parts else None

    # -- the text checks ------------------------------------------------------------------------

    def check_text(
        self, text: str, file: str, locator: str, *, identifier: bool = False
    ) -> list[LeakFinding]:
        """Every finding in one string (feature 137 reuses this per tracked-file line)."""

        out: list[LeakFinding] = []
        self._check_text(text, file, locator, identifier=identifier, out=out)
        return out

    def _check_text(
        self, text: str, file: str, locator: str, *, identifier: bool, out: list[LeakFinding]
    ) -> None:
        if not text:
            return
        folded = text.casefold()
        if folded in self._exempt:
            return  # a value the run declared public, e.g. a destination summit name

        def add(severity: str, category: str, check: str, offset: int) -> None:
            out.append(LeakFinding(severity, category, check, file, locator, offset))

        if self._token_re is not None:
            for m in self._token_re.finditer(folded):
                if self._inside_ordinary_word(folded, m.start(), m.end(), original=text):
                    continue
                add(FATAL, self._token_category.get(m.group(0), "token"), "token", m.start())
        if self._warn_re is not None:
            for m in self._warn_re.finditer(folded):
                add(FATAL if self.strict else WARN, "event_term", "denylist", m.start())
        if self._dict_words:
            spans = (
                [(0, folded)]
                if identifier
                else [
                    (m.start(), m.group(0).casefold())
                    for m in _HOSTLIKE.finditer(text)
                    if not _TZ_REGION.search(text, 0, m.start())  # "America/New_York"
                ]
                + [(m.start(2), m.group(2).casefold()) for m in _QUOTED_ID_KV.finditer(text)]
            )
            flagged: set[int] = set()
            for base, chunk in spans:
                for w in _ALPHA.finditer(chunk):
                    if w.group(0) in self._dict_words:
                        add(FATAL, "dictionary_word", "identifier", base + w.start())
                        flagged.add(base + w.start())
            if not identifier and self._names_in_prose:
                # A dictionary-word place is a NAME in prose when it is capitalised or one of
                # two or more places in a comma list (2026-11-17: the first published demo
                # leaked both shapes as mere warnings).
                generated = [(m.start(), m.end()) for m in _EQUIV.finditer(text)]
                for w in _CAPITALISED.finditer(text):
                    pos = w.start()
                    if (
                        w.group(0).casefold() in self._dict_words
                        and pos not in flagged
                        and not any(lo <= pos < hi for lo, hi in generated)
                    ):
                        add(FATAL, "dictionary_word", "capitalised", pos)
                        flagged.add(pos)
                if "," in text:
                    for lst in _COMMA_LIST.finditer(folded):
                        hits = [
                            [lst.start() + it.start() + w.start() for w in _ALPHA.finditer(
                                it.group(0)) if w.group(0) in self._dict_words]
                            for it in _COMMA_ITEM.finditer(lst.group(0))
                        ]  # fmt: skip
                        if sum(1 for h in hits if h) >= 2:
                            for pos in (p for h in hits for p in h if p not in flagged):
                                add(FATAL, "dictionary_word", "listed", pos)
                                flagged.add(pos)
                for w in _ALPHA.finditer(folded):
                    if w.group(0) in self._dict_words and w.start() not in flagged:
                        add(WARN, "dictionary_word", "prose", w.start())
        if any(ch.isdigit() for ch in text):
            for m in IPV4_LITERAL.finditer(text):
                try:
                    addr = ipaddress.IPv4Address(m.group(0).split("/")[0])
                except ValueError:
                    continue
                if not is_allowed_v4(addr):
                    add(FATAL, "ip", "ip", m.start())
            if self._centroid is not None and "." in text:
                for m in COORD_PAIR.finditer(text):
                    lat, lon = float(m.group(1)), float(m.group(3))
                    if self._near(lat, lon):
                        add(FATAL, "coord", "coord", m.start())
        if ":" in text:
            for m in IPV6_CANDIDATE.finditer(text):
                candidate, _, plen = m.group(0).partition("/")
                if candidate.count(":") < 2 or (plen.isdigit() and int(plen) < 32):
                    continue
                try:
                    addr6 = ipaddress.IPv6Address(candidate)
                except ValueError:
                    continue
                if not is_allowed_v6(addr6):
                    add(FATAL, "ip", "ip", m.start())
        for m in _MAC_SEP.finditer(text):
            if not int(m.group(1), 16) & 0b10:
                add(FATAL, "mac", "mac", m.start())
        for m in _MAC_DOT.finditer(text):
            if not int(m.group(1), 16) & 0b10:
                add(FATAL, "mac", "mac", m.start())
        if self._real_macs:
            for m in _MAC_BARE.finditer(text):
                if m.group(0).casefold() in self._real_macs:
                    add(FATAL, "mac", "mac", m.start())
        for m in _SECRET_SHAPE.finditer(text):
            add(FATAL, "secret", "secret", m.start())
        if "AS" in text:
            for m in _ASN_SHAPE.finditer(text):
                if not is_reserved_asn(int(m.group(1))):
                    add(FATAL, "asn", "shape", m.start())
        if "-key" in text:
            for m in _WG_KEY_SHAPE.finditer(text):
                add(FATAL, "secret", "key-shape", m.start())
        if "-" in text and "." in text:
            for m in _UUID_HOST_SHAPE.finditer(text):
                add(FATAL, "secret", "uuid-host", m.start())
        if "@" in text:
            for m in _EMAIL.finditer(text):
                if not m.group(1).casefold().endswith(
                    _ALLOWED_EMAIL_DOMAINS
                ) and "localhost" not in m.group(1):
                    add(FATAL, "email", "shape", m.start())
        for m in _SLACK_SHAPE.finditer(text):
            add(WARN, "slack_id", "shape", m.start())
        for w in _ALPHA.finditer(folded):
            word = w.group(0)
            if len(word) >= 5 and word not in self._words_seen:
                self._words_seen[word] = (file, locator)

    def _inside_ordinary_word(
        self, folded: str, start: int, end: int, *, original: str | None = None
    ) -> bool:
        """A long token matched as a substring of a LONGER ordinary word (``transpo`` inside
        ``transport``, or inside the CamelCase part ``Commissary`` of ``RanchCommissary``) is not
        a leak; inside a compound name (``northfieldgw``) it is."""

        lo, hi = start, end
        while lo > 0 and folded[lo - 1].isalnum():
            lo -= 1
        while hi < len(folded) and folded[hi].isalnum():
            hi += 1
        if (lo, hi) == (start, end):
            return False
        if self._ordinary(folded[lo:hi]):
            return True
        if original is not None and len(original) == len(folded):
            offset = lo
            for part in camel_parts(original[lo:hi]):
                p_lo = original.find(part, offset)
                p_hi = p_lo + len(part)
                offset = p_hi
                if p_lo <= start and end <= p_hi:
                    return (p_lo, p_hi) != (start, end) and self._ordinary(part.casefold())
        return False

    def _ordinary(self, word: str) -> bool:
        return word in GENERIC_TOKENS or is_dictionary_word(word, self._dictionary)

    def _near(self, lat: float, lon: float) -> bool:
        if self._centroid is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return False
        return haversine_m(self._centroid[0], self._centroid[1], lat, lon) <= _COORD_FENCE_M

    def _check_pair(
        self, lat: Any, lon: Any, file: str, locator: str, out: list[LeakFinding]
    ) -> None:
        if isinstance(lat, int | float) and isinstance(lon, int | float):
            if abs(lat) > 1000 or abs(lon) > 1000:  # microdegree integers (meshstore)
                lat, lon = lat / 1e7, lon / 1e7
            if self._near(float(lat), float(lon)):
                out.append(LeakFinding(FATAL, "coord", "coord", file, locator, 0))

    # -- containers ------------------------------------------------------------------------------

    def _scan_json(self, obj: Any, file: str, path: str, out: list[LeakFinding]) -> None:
        if isinstance(obj, dict):
            lat = obj.get("latitude", obj.get("lat"))
            lon = obj.get("longitude", obj.get("lon"))
            if lat is not None and lon is not None:
                self._check_pair(lat, lon, file, path, out)
            for key, value in obj.items():
                child = f"{path}.{key}" if len(str(key)) < 40 else f"{path}.<key>"
                # Keys are schema words or identifiers: judged as prose, so a dictionary-word
                # name only counts inside a hostname-shaped key (``fleet|northfield-gw``).
                self._check_text(str(key), file, f"{path}<key>", identifier=False, out=out)
                if isinstance(value, str):
                    ident = str(key).casefold() in _ID_FIELDS
                    self._check_text(value, file, child, identifier=ident, out=out)
                else:
                    self._scan_json(value, file, child, out)
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                if isinstance(item, str):
                    self._check_text(item, file, f"{path}[{i}]", identifier=False, out=out)
                else:
                    self._scan_json(item, file, f"{path}[{i}]", out)
        elif isinstance(obj, str):
            self._check_text(obj, file, path, identifier=False, out=out)

    def _scan_sqlite(self, path: Path, rel: str, out: list[LeakFinding]) -> int:
        cells = 0
        conn = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
        try:
            tables = [
                (row[0], row[1] or "")
                for row in conn.execute("SELECT name, sql FROM sqlite_master WHERE type='table'")
            ]
            fts = [name for name, sql in tables if "fts5" in sql.casefold()]
            # External-content FTS (content='logs') indexes base-table cells already scanned
            # with their original casing; only a standalone FTS table holds text of its own.
            standalone_fts = [
                name
                for name, sql in tables
                if "fts5" in sql.casefold() and not re.search(r"content\s*=\s*'[^']+'", sql)
            ]
            for name, sql in tables:
                if "virtual table" in sql.casefold():
                    continue
                if any(name.startswith(f) and name[len(f) :] in _FTS_SHADOW for f in fts):
                    continue
                cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{name}")')]
                lat_cols = [c for c in cols if c.casefold().startswith("lat")]
                lon_cols = [c for c in cols if c.casefold().startswith("lon")]
                for row in conn.execute(f'SELECT rowid, * FROM "{name}"'):
                    rowid, values = row[0], row[1:]
                    record = dict(zip(cols, values, strict=False))
                    for col, value in record.items():
                        if isinstance(value, bytes):
                            value = value.decode("utf-8", errors="ignore")
                        if isinstance(value, str):
                            cells += 1
                            self._check_text(
                                value,
                                rel,
                                f"{name}:{rowid}:{col}",
                                identifier=col.casefold() in _ID_FIELDS,
                                out=out,
                            )
                    if lat_cols and lon_cols:
                        self._check_pair(
                            record[lat_cols[0]], record[lon_cols[0]], rel, f"{name}:{rowid}", out
                        )
            for name in standalone_fts:
                conn.execute(
                    f'CREATE VIRTUAL TABLE temp."v_{name}" USING fts5vocab(main, "{name}", row)'
                )
                for (term,) in conn.execute(f'SELECT term FROM temp."v_{name}"'):
                    self._check_text(str(term), rel, f"{name}:vocab", identifier=False, out=out)
        finally:
            conn.close()
        return cells

    def _scan_git(self, repo: Path, rel: str, out: list[LeakFinding]) -> None:
        data = configstore.dump_objects(repo)
        pos = 0
        while pos < len(data):
            header_end = data.index(b"\n", pos)
            sha, kind, size_text = data[pos:header_end].decode().split(" ")
            size = int(size_text)
            body = data[header_end + 1 : header_end + 1 + size]
            pos = header_end + 1 + size + 1
            # A tree is binary — "<mode> <name>\0" then the child's 20 RAW hash bytes. Only the
            # names are data; decoding the hashes as text let random bytes spell an email shape
            # (the ~1-in-20 feature 138 flake: "email/shape … git:tree").
            text = (
                "\n".join(tree_names(body))
                if kind == "tree"
                else body.decode("utf-8", errors="ignore")
            )
            for n, line in enumerate(text.splitlines(), 1):
                self._check_text(
                    line, rel, f"git:{kind}:{sha[:12]}:{n}", identifier=kind == "tree", out=out
                )
        git_dir = repo / ".git"
        for path in sorted(git_dir.rglob("*")):
            if path.is_file() and "objects" not in path.relative_to(git_dir).parts:
                self._scan_raw(path, f"{rel}/.git/{path.relative_to(git_dir)}", out)

    def _scan_text_file(self, path: Path, rel: str, out: list[LeakFinding]) -> None:
        text = path.read_text(encoding="utf-8", errors="ignore")
        for n, line in enumerate(text.splitlines(), 1):
            self._check_text(line, rel, f"line {n}", identifier=False, out=out)

    def _scan_raw(
        self, path: Path, rel: str, out: list[LeakFinding], *, secrets_only: bool = False
    ) -> None:
        """Raw-bytes backstop. Binary payloads (compressed images, FTS5 index pages that store
        terms prefix-compressed) match short tokens by chance, so only LONG distinctive tokens
        count here — and for SQLite files, whose every cell and FTS vocabulary was already
        scanned structurally, only secrets."""

        text = path.read_bytes().decode("latin-1")
        if not secrets_only and self._token_re is not None:
            folded = text.casefold()
            for m in self._token_re.finditer(folded):
                token = m.group(0)
                if len(token) < 6 or self._inside_ordinary_word(folded, m.start(), m.end()):
                    continue
                category = self._token_category.get(token, "token")
                out.append(LeakFinding(FATAL, category, "token", rel, "raw-bytes", m.start()))
        for secret in self._secrets:
            start = text.find(secret)
            if start >= 0:
                out.append(LeakFinding(FATAL, "secret", "secret", rel, "raw-bytes", start))
        for m in _SECRET_SHAPE.finditer(text):
            out.append(LeakFinding(FATAL, "secret", "secret", rel, "raw-bytes", m.start()))

    def _files(self, root: Path) -> Iterator[Path]:
        for path in sorted(root.rglob("*")):
            if any(part == ".git" for part in path.relative_to(root).parts):
                continue
            if path.is_file():
                yield path

    # -- entry point ----------------------------------------------------------------------------

    def scan(self, root: Path, *, skip: frozenset[str] = frozenset()) -> GateResult:
        """Scan every file under ``root`` except the relative paths in ``skip``.

        ``skip`` exists for feature 136's bundle: obi's own web source files, copied verbatim,
        are application code — published (and scrubbed) by the public-repo export — not data.
        """

        out: list[LeakFinding] = []
        self._words_seen = {}
        files = cells = 0
        for path in self._files(root):
            rel = str(path.relative_to(root))
            if rel in skip:
                continue
            files += 1
            cells += self._dispatch(path, rel, out, json_as_text=False)
        for git_dir in sorted(root.rglob(".git")):
            if git_dir.is_dir():
                self._scan_git(git_dir.parent, str(git_dir.parent.relative_to(root)), out)
        self._fuzzy(out)
        return GateResult(
            findings=out,
            counts={
                "files": files,
                "cells": cells,
                "harvested_tokens": self.harvested_tokens,
                "denylist_terms": self.denylist_terms,
            },
        )

    def _dispatch(self, path: Path, rel: str, out: list[LeakFinding], *, json_as_text: bool) -> int:
        """Scan one file by its container type; returns the SQLite cells scanned."""

        with path.open("rb") as handle:
            head = handle.read(16)
        if head == _SQLITE_MAGIC:
            cells = self._scan_sqlite(path, rel, out)
            self._scan_raw(path, rel, out, secrets_only=True)
            return cells
        if path.suffix == ".json" and not json_as_text:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                self._scan_text_file(path, rel, out)
            else:
                self._scan_json(data, rel, "$", out)
        elif path.suffix.casefold() in _BINARY_SUFFIXES:
            self._scan_raw(path, rel, out)
        else:
            self._scan_text_file(path, rel, out)
        return 0

    def scan_file(self, path: Path, rel: str, *, json_as_text: bool = False) -> list[LeakFinding]:
        """One file's findings (no fuzzy pass — callers run :meth:`fuzzy` once at the end).

        ``json_as_text`` scans JSON line by line so findings carry ``line N`` locators, which is
        what the 137 identity guard reports for tracked fixtures."""

        out: list[LeakFinding] = []
        self._dispatch(path, rel, out, json_as_text=json_as_text)
        return out

    def fuzzy(self) -> list[LeakFinding]:
        """The near-miss pass over every word seen since the last :meth:`reset_words`."""

        out: list[LeakFinding] = []
        self._fuzzy(out)
        return out

    def reset_words(self) -> None:
        self._words_seen = {}

    def _fuzzy(self, out: list[LeakFinding]) -> None:
        """Near-misses of distinctive names (``eastmor``) anywhere in the dataset (FR-011d)."""

        if not self._fuzzy_targets:
            return
        for word, (file, locator) in self._words_seen.items():
            if word in self._token_category or word in GENERIC_TOKENS:
                continue
            if is_dictionary_word(word, self._dictionary):
                continue  # ordinary English is never a near-miss of a name (`comment`/`commetn`)
            for target in self._fuzzy_targets:
                if abs(len(word) - len(target)) <= 1 and within_one_edit(word, target):
                    out.append(LeakFinding(FATAL, "fuzzy", "fuzzy", file, locator, 0))
                    break

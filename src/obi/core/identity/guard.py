"""The identity guard (feature 137, US3, research D4): scan tracked files for real identifiers.

Built on the 134 leak gate's per-text checks (:meth:`LeakGate.check_text` — value-free findings,
trie matchers, shape checks hardened by the 136 publish check), plus what a SOURCE tree needs:

* ``line N`` locators for every file (JSON fixtures are scanned as text lines);
* the repo's own scope file, ``identity-guard.toml`` — exclusions and allowances, each with a
  reason, printed on every run (contracts/guard-config.md);
* tree-specific shape checks — tailnet hostnames, chat IDs (fatal, unless fake-marked), and an
  address policy for code: RFC 1918 is ordinary in tests, so only REAL blocks (from the private
  mapping) and public addresses outside the documentation pools are fatal;
* the FR-022 link check: no markdown link into, and no runtime path literal reading from, an
  excluded directory (``specs/`` is private-only).

A finding never carries the matched value, so a report is safe to paste anywhere.
"""

from __future__ import annotations

import ipaddress
import json
import posixpath
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from obi.core.config import ConfigError
from obi.core.demo.gate import FATAL, WARN, LeakFinding, LeakGate
from obi.core.demo.ipremap import IPV4_LITERAL, is_allowed_v4
from obi.core.identity.tokens import (
    LOADED,
    IdentityTokens,
    identity_dictionary,
    is_public_service_address,
)

GUARD_FILE = "identity-guard.toml"

_TAILNET = re.compile(r"(?i)(?<![\w.-])(?:[a-z0-9-]+\.)+ts\.net(?![\w-])")
_CHAT_ID = re.compile(r"(?<![A-Za-z0-9_])[UCWGD]\d(?=[A-Z0-9]*[A-Z])[A-Z0-9]{7,9}(?![A-Za-z0-9_])")
#: A secret shape inside a value that announces itself as fake is test data, not a credential.
_FAKE_MARKER = re.compile(
    r"(?i)fake|dummy|example|placeholder|test|secret|abc\d|x{4,}|0{6,}|1-2-3|\bAAA\b"
)
_MD_LINK = re.compile(r"\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_EMAIL_DOMAIN_OK = re.compile(
    r"(?i)(?:^|\.)(?:example\.(?:net|org|com)|test|invalid|localhost|example)$"
)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})(?![\w.-])")
_DEVICE_SPAN = re.compile(r"[A-Za-z0-9]+(?:[-.][A-Za-z0-9]+)+")
#: A dotted address PREFIX ("10.1.", "10.1.4.") — test code asserting on a real range.
IPV4_PREFIX = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(?:(\d{1,3})\.)?(?![\d.])")
#: A latitude/longitude written as a key and a number (JSON, Python, TOML, JS) — the pair is
#: usually split across lines, which the one-line pair check cannot see.
COORD_KEY = re.compile(
    r"""(?i)(?<![a-z_])["']?(lat|latitude|lon|lng|long|longitude)["']?\s*[:=]\s*(-?\d{1,3}\.\d+)"""
)
#: Integer microdegrees (Meshtastic ``latitudeI`` / ``latitude_i``: degrees × 1e7).
COORD_KEY_I = re.compile(
    r"""(?<![A-Za-z_])["']?(latitude_?[iI]|longitude_?[iI])["']?\s*[:=]\s*(-?\d{7,10})(?!\d)"""
)
_COORD_WINDOW = 6  # lines between a latitude and its longitude
#: A GeoJSON position — ``[lon, lat]`` (longitude FIRST), the order the one-line pair check misses.
LONLAT = re.compile(r"\[\s*(-?\d{1,3}\.\d{3,})\s*,\s*(-?\d{1,2}\.\d{3,})")
#: A pipe/semicolon-delimited ``lat|lon`` row (GNIS ``DomesticNames``, CSV-ish exports).
DELIM_PAIR = re.compile(r"(?<![\d.])(-?\d{1,2}\.\d{3,})(\s*[|;]\s*)(-?\d{1,3}\.\d{3,})(?![\d.])")
#: Role tokens of device/link names (``<site>-gw-01``, ``<site>-ptp-<far>-60``). An ORDINARY word
#: that is also a real site name is fatal only inside a name carrying one of these — in code,
#: ``row-count`` or ``not-configured`` is not a device.
ROLE_TOKENS = frozenset(
    {"gw", "ptp", "ptmp", "ap", "sw", "vms", "internet", "rtr", "router", "switch", "bridge",
     "sector", "relay", "tower", "gateway", "core", "edge", "link", "wan", "uplink", "nvr"}
)  # fmt: skip
#: Public event vocabulary (the 134 rewriter's EVENT_EQUIVALENTS, single-token forms): naming the
#: event is acceptable per the 134 operator decision, so these are WARNINGS even when the private
#: mapping also lists them as place words.
EVENT_WORDS = frozenset({"playa", "brc", "burningman", "esplanade"})


# -- scope ---------------------------------------------------------------------------------------


def _expand_braces(glob: str) -> list[str]:
    m = re.search(r"\{([^{}]*)\}", glob)
    if not m:
        return [glob]
    head, tail = glob[: m.start()], glob[m.end() :]
    return [g for alt in m.group(1).split(",") for g in _expand_braces(head + alt + tail)]


def _glob_regex(glob: str) -> re.Pattern[str]:
    out = []
    i = 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def glob_match(rel: str, glob: str) -> bool:
    """Path-glob match with ``**`` (any depth) and ``{a,b}`` alternatives."""

    return any(_glob_regex(g).match(rel) for g in _expand_braces(glob))


@dataclass(frozen=True)
class Exclusion:
    glob: str
    reason: str


@dataclass(frozen=True)
class Allowance:
    category: str
    reason: str
    pattern: str | None = None
    path: str | None = None
    check: str | None = None  # narrow to one check (e.g. "mac" shape, never "token")

    def applies(self, finding: LeakFinding, text: str) -> bool:
        if finding.category != self.category:
            return False
        if self.check is not None and finding.check != self.check:
            return False
        if self.path is not None and not glob_match(finding.file, self.path):
            return False
        if self.pattern is not None:
            return re.match(self.pattern, text[finding.offset :]) is not None
        return True


@dataclass(frozen=True)
class GuardConfig:
    exclusions: tuple[Exclusion, ...] = ()
    allowances: tuple[Allowance, ...] = ()

    def excluded(self, rel: str) -> Exclusion | None:
        return next((e for e in self.exclusions if glob_match(rel, e.glob)), None)

    def excluded_dirs(self) -> list[str]:
        """Top-level directories excluded wholesale (``specs/**`` → ``specs``)."""

        return [e.glob[:-3] for e in self.exclusions if re.fullmatch(r"[\w.-]+/\*\*", e.glob)]


def load_guard_config(root: Path) -> GuardConfig:
    path = root / GUARD_FILE
    if not path.exists():
        return GuardConfig()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"could not read {GUARD_FILE}: {exc}") from exc
    exclusions, allowances = [], []
    for i, entry in enumerate(data.get("exclude", [])):
        if not str(entry.get("reason", "")).strip() or not entry.get("glob"):
            raise ConfigError(f"{GUARD_FILE}: exclude #{i + 1} needs a glob and a reason")
        exclusions.append(Exclusion(str(entry["glob"]), str(entry["reason"])))
    for i, entry in enumerate(data.get("allow", [])):
        if not str(entry.get("reason", "")).strip() or not entry.get("category"):
            raise ConfigError(f"{GUARD_FILE}: allow #{i + 1} needs a category and a reason")
        if entry.get("pattern") is not None:
            try:
                re.compile(str(entry["pattern"]))
            except re.error as exc:
                raise ConfigError(f"{GUARD_FILE}: allow #{i + 1} pattern is invalid") from exc
        allowances.append(
            Allowance(
                category=str(entry["category"]),
                reason=str(entry["reason"]),
                pattern=entry.get("pattern"),
                path=entry.get("path"),
                check=entry.get("check"),
            )
        )
    return GuardConfig(tuple(exclusions), tuple(allowances))


def _check_allowances(config: GuardConfig, tokens: IdentityTokens) -> None:
    """An allowance may never let a PRIVATE token through (it would hide a real value)."""

    h = tokens.harvest
    private = [
        *h.place_words, *h.persons, *h.person_words, *h.node_ids, *h.ssids, *h.serials,
        *h.slack_ids, *h.domains, *h.ips, *(t for t, fatal in h.event_terms.items() if fatal),
    ]  # fmt: skip
    for i, allow in enumerate(config.allowances):
        if allow.pattern is None:
            continue
        rx = re.compile(allow.pattern, re.IGNORECASE)
        # A token that announces itself as fake (``U0FAKE0001`` in a fixture config) is not real.
        if any(rx.search(token) for token in private if not _FAKE_MARKER.search(token)):
            raise ConfigError(
                f"{GUARD_FILE}: allow #{i + 1} ({allow.category}) shadows a private token — "
                "an allowance may only name public, fictional values"
            )


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"], check=True, capture_output=True
    ).stdout
    return sorted(p for p in out.decode("utf-8", errors="replace").split("\0") if p)


# -- result --------------------------------------------------------------------------------------


@dataclass
class GuardResult:
    findings: list[LeakFinding] = field(default_factory=list)
    files: int = 0
    skipped: list[str] = field(default_factory=list)
    excluded: dict[str, int] = field(default_factory=dict)  # glob → files skipped by it
    config: GuardConfig = field(default_factory=GuardConfig)
    inputs: dict[str, str] = field(default_factory=dict)

    @property
    def fatal(self) -> list[LeakFinding]:
        return [f for f in self.findings if f.severity == FATAL]

    @property
    def warnings(self) -> list[LeakFinding]:
        return [f for f in self.findings if f.severity == WARN]

    def counts(self, severity: str = FATAL) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.findings:
            if f.severity == severity:
                out[f.category] = out.get(f.category, 0) + 1
        return dict(sorted(out.items()))

    def header(self) -> str:
        marks = "  ".join(
            f"{name} {'✓' if state == LOADED else 'skipped'}" for name, state in self.inputs.items()
        )
        lines = [f"identity guard: {self.files} file(s) scanned; private inputs: {marks}"]
        for e in self.config.exclusions:
            lines.append(f"  exclude {e.glob} ({self.excluded.get(e.glob, 0)}) — {e.reason}")
        lines.append(f"  {len(self.config.allowances)} allowance(s)")
        if self.skipped:
            lines.append(f"  {len(self.skipped)} binary/undecodable file(s) skipped")
        return "\n".join(lines)

    def report(self, *, limit: int = 200, include_warnings: bool = False) -> str:
        shown = self.findings if include_warnings else self.fatal
        lines = [
            self.header(),
            f"{len(self.fatal)} fatal, {len(self.warnings)} warning(s)",
        ]
        totals: dict[tuple[str, str, str], int] = {}
        for f in shown:
            key = (f.severity, f"{f.category}/{f.check}", f.file)
            totals[key] = totals.get(key, 0) + 1
        for (severity, kind, file), n in sorted(
            totals.items(), key=lambda kv: (kv[0][0] != FATAL, kv[0][2], kv[0][1])
        ):
            lines.append(f"  {n:>6}  [{severity}] {kind} {file}")
        ordered = sorted(shown, key=lambda f: (f.severity != FATAL, f.file, _line_no(f.locator)))
        if ordered:
            lines.append("findings:")
        for f in ordered[:limit]:
            lines.append(f"[{f.severity}] {f.category}/{f.check} {f.file} {f.locator} @{f.offset}")
        if len(ordered) > limit:
            lines.append(f"… {len(ordered) - limit} more")
        return "\n".join(lines)

    def to_json(self) -> str:
        return json.dumps(
            {
                "inputs": self.inputs,
                "files": self.files,
                "exclusions": [
                    {"glob": e.glob, "reason": e.reason, "files": self.excluded.get(e.glob, 0)}
                    for e in self.config.exclusions
                ],
                "allowances": len(self.config.allowances),
                "counts": {"fatal": self.counts(FATAL), "warning": self.counts(WARN)},
                "findings": [
                    {
                        "severity": f.severity,
                        "category": f.category,
                        "check": f.check,
                        "file": f.file,
                        "locator": f.locator,
                        "offset": f.offset,
                    }  # fmt: skip
                    for f in self.findings
                ],
            },
            indent=1,
        )


def _domain_at(text: str, offset: int) -> bool:
    """True when the word at ``offset`` is a DNS label (``…event-name.org``): a hostname, which
    stays fatal even though the bare word is public event vocabulary."""

    m = re.match(r"[A-Za-z0-9-]+\.(?:org|com|net|io|dev|us|info|edu|gov|app)\b", text[offset:])
    return m is not None


def _line_no(locator: str) -> int:
    m = re.search(r"\d+", locator)
    return int(m.group(0)) if m else 0


# -- the scanner ---------------------------------------------------------------------------------


class IdentityGuard:
    """Per-line identity checks over source text (value-free findings)."""

    def __init__(
        self,
        tokens: IdentityTokens,
        config: GuardConfig,
        *,
        dictionary: frozenset[str] | None = None,
    ) -> None:
        _check_allowances(config, tokens)
        self.tokens = tokens
        self.config = config
        self.gate = LeakGate(
            tokens.harvest,
            dictionary=identity_dictionary() if dictionary is None else dictionary,
            # Source and docs are English: a capitalised "House" there is a word, not a site
            # (the dataset gate's names-in-prose rule is for replayed DATA).
            names_in_prose=False,
        )
        self._real_ips = frozenset(tokens.harvest.ips)
        self._excluded_dirs = config.excluded_dirs()
        dirs = "|".join(re.escape(d) for d in self._excluded_dirs)
        self._path_literal = (
            re.compile(rf"""["']{dirs and f"(?:{dirs})"}(?:["']|/)""") if dirs else None
        )

    # -- per line ---------------------------------------------------------------------------

    def check_line(self, text: str, rel: str, locator: str) -> list[LeakFinding]:
        raw = self.gate.check_text(text, rel, locator)
        out: list[LeakFinding] = []
        for f in raw:
            if f.category == "ip" and IPV4_LITERAL.match(text, f.offset):
                continue  # IPv4 is judged below with the source-tree address policy
            if f.category == "ip":
                groups = re.match(r"[0-9A-Fa-f:]*", text[f.offset :])
                if groups is None or len([g for g in groups.group(0).split(":") if g]) < 3:
                    continue  # ``[Console]::Error`` parses as IPv6; a real address has groups
            if f.category == "slack_id" and f.check == "shape":
                continue  # replaced by the fatal chat-ID shape below
            if f.category == "email":
                continue  # replaced below (RFC 2606 test/example domains are fine)
            if (
                f.category == "dictionary_word"
                and f.check == "identifier"
                and not self._device_shaped(text, f.offset)
            ):
                continue
            if f.severity == FATAL and f.check in ("token", "identifier"):
                word = re.match(r"[a-z]+", text[f.offset :].casefold())
                if word and word.group(0) in EVENT_WORDS and not _domain_at(text, f.offset):
                    out.append(LeakFinding(WARN, "event_term", "event", rel, locator, f.offset))
                    continue
            if f.category == "secret" and f.check == "secret" and _FAKE_MARKER.search(text):
                continue  # a shape on a line that announces fake/test data (real values are
                # caught by exact match regardless — that check is "token", not "secret")
            out.append(f)

        def add(severity: str, category: str, check: str, offset: int) -> None:
            out.append(LeakFinding(severity, category, check, rel, locator, offset))

        if any(ch.isdigit() for ch in text):
            for m in IPV4_LITERAL.finditer(text):
                verdict = self._ipv4(m.group(0))
                if verdict:
                    add(FATAL, "ip", verdict, m.start())
        if "[" in text and self.gate._centroid is not None:  # noqa: SLF001
            for m in LONLAT.finditer(text):
                if self.gate._near(float(m.group(2)), float(m.group(1))):  # noqa: SLF001
                    add(FATAL, "coord", "lonlat", m.start())
        if ("|" in text or ";" in text) and self.gate._centroid is not None:  # noqa: SLF001
            for m in DELIM_PAIR.finditer(text):
                if self.gate._near(float(m.group(1)), float(m.group(3))):  # noqa: SLF001
                    add(FATAL, "coord", "delimited", m.start())
        if self.tokens.real_blocks and "." in text:
            for m in IPV4_PREFIX.finditer(text):
                if self._prefix_is_real(m):
                    add(FATAL, "ip", "prefix", m.start())
        if "ts.net" in text.casefold():
            for m in _TAILNET.finditer(text):
                add(FATAL, "hostname", "tailnet", m.start())
        for m in _CHAT_ID.finditer(text):
            add(FATAL, "chat_id", "shape", m.start())
        if "@" in text:
            for m in _EMAIL.finditer(text):
                if not _EMAIL_DOMAIN_OK.search(m.group(1)):
                    add(FATAL, "email", "shape", m.start())
        out.extend(self._links(text, rel, locator))
        return [f for f in out if not any(a.applies(f, text) for a in self.config.allowances)]

    @staticmethod
    def _device_shaped(text: str, offset: int) -> bool:
        """A hyphen/dot name (``site-gw-01``, ``a-ptp-b-60``) with a role token, or a
        ``<word>-NN`` name. Underscored code identifiers (``parse_router_body``) never count."""

        for m in _DEVICE_SPAN.finditer(text):
            if m.start() <= offset < m.end():
                parts = [p.casefold() for p in re.split(r"[-.]+", m.group(0))]
                if any(p in ROLE_TOKENS for p in parts):
                    return True
                word = re.match(r"[a-z]+", text[offset:].casefold())
                return (
                    word is not None
                    and parts[0] == word.group(0)
                    and parts[-1].isdigit()
                    and len(parts) <= 3
                )
        return False

    def _prefix_is_real(self, m: re.Match[str]) -> bool:
        parts = [int(g) for g in m.groups() if g is not None]
        if any(p > 255 for p in parts):
            return False
        probe = ipaddress.IPv4Address(".".join(str(p) for p in [*parts, 0, 0][:4]))
        return any(probe in net for net in self.tokens.real_blocks)

    def _ipv4(self, literal: str) -> str | None:
        bare, _, plen = literal.partition("/")
        if plen.isdigit() and int(plen) < 16:  # a generic range (10.0.0.0/8), not a host
            return None
        try:
            addr = ipaddress.IPv4Address(bare)
        except ValueError:
            return None
        if bare in self._real_ips:
            return "token"
        if any(addr in net for net in (*self.tokens.real_blocks, *self.tokens.public24)):
            return "range"
        if addr.is_private or addr in ipaddress.IPv4Network("100.64.0.0/10"):
            return None  # RFC 1918 / CGNAT: ordinary in code and tests unless a REAL block
        if is_allowed_v4(addr) or is_public_service_address(addr):
            return None
        return "public"

    def _links(self, text: str, rel: str, locator: str) -> list[LeakFinding]:
        if not self._excluded_dirs:
            return []
        out: list[LeakFinding] = []
        if rel.endswith((".md", ".markdown")) and "](" in text:
            base = posixpath.dirname(rel)
            for m in _MD_LINK.finditer(text):
                target = m.group(1).split("#", 1)[0]
                if not target or re.match(r"(?i)[a-z][a-z0-9+.-]*:", target):
                    continue
                resolved = posixpath.normpath(posixpath.join(base, target))
                if any(resolved == d or resolved.startswith(d + "/") for d in self._excluded_dirs):
                    out.append(LeakFinding(FATAL, "link", "markdown", rel, locator, m.start(1)))
        elif rel.endswith(".py") and self._path_literal is not None:
            for m in self._path_literal.finditer(text):
                out.append(LeakFinding(FATAL, "link", "path-literal", rel, locator, m.start()))
        return out

    # -- per file ---------------------------------------------------------------------------

    def scan_file(self, root: Path, rel: str) -> list[LeakFinding] | None:
        """Findings for one file, or ``None`` when it is binary/undecodable (skipped)."""

        try:
            data = (root / rel).read_bytes()
        except OSError:
            return None
        if b"\x00" in data[:8192]:
            return None
        text = data.decode("utf-8", errors="replace")
        # A file NAME can carry identity too (``<real-device>-ospf.json``).
        out: list[LeakFinding] = [
            f for f in self.check_line(rel, rel, "path") if f.category != "link"
        ]
        lines = text.splitlines()
        for n, line in enumerate(lines, 1):
            if line.strip():
                out.extend(self.check_line(line, rel, f"line {n}"))
        out.extend(
            f
            for f in self.coord_key_findings(lines, rel)
            if not any(a.applies(f, lines[_line_no(f.locator) - 1]) for a in self.config.allowances)
        )
        return out

    def coord_key_findings(self, lines: list[str], rel: str) -> list[LeakFinding]:
        """Key-written coordinate pairs near the real deployment (fatal ``coord/key``)."""

        if self.gate._centroid is None:  # noqa: SLF001 - the gate owns the fence
            return []
        out: list[LeakFinding] = []
        for (lat_line, lat_off, lat), (_lon_line, _lon_off, lon) in coord_key_pairs(lines):
            if self.gate._near(lat, lon):  # noqa: SLF001
                out.append(LeakFinding(FATAL, "coord", "key", rel, f"line {lat_line}", lat_off))
        return out


def coord_key_pairs(
    lines: list[str],
) -> list[tuple[tuple[int, int, float], tuple[int, int, float]]]:
    """Every (latitude, longitude) written as keyed numbers within a few lines of each other:
    ``((lat_line, lat_value_offset, lat), (lon_line, lon_value_offset, lon))``, 1-based lines."""

    seen: list[tuple[str, int, int, float]] = []
    for n, line in enumerate(lines, 1):
        for m in COORD_KEY.finditer(line):
            kind = "lat" if m.group(1).lower().startswith("lat") else "lon"
            seen.append((kind, n, m.start(2), float(m.group(2))))
        for m in COORD_KEY_I.finditer(line):
            kind = "lat" if m.group(1).lower().startswith("lat") else "lon"
            seen.append((kind, n, m.start(2), int(m.group(2)) / 1e7))
    seen.sort(key=lambda item: (item[1], item[2]))
    pairs = []
    used: set[int] = set()
    for i, (kind, n, off, value) in enumerate(seen):
        if kind != "lat" or i in used:
            continue
        for j in range(i + 1, len(seen)):
            k2, n2, off2, v2 = seen[j]
            if n2 - n > _COORD_WINDOW:
                break
            if k2 == "lon" and j not in used:
                pairs.append(((n, off, value), (n2, off2, v2)))
                used.update({i, j})
                break
    return pairs


def _select(root: Path, paths: list[str] | None) -> list[str]:
    tracked = tracked_files(root)
    if not paths:
        return tracked
    chosen: list[str] = []
    for raw in paths:
        p = raw.rstrip("/")
        if p in ("", "."):
            return tracked
        under = [t for t in tracked if t == p or t.startswith(p + "/")]
        if under:
            chosen.extend(under)
        elif (root / p).is_file():
            chosen.append(p)
    return sorted(dict.fromkeys(chosen))


def scan_paths(
    paths: list[str] | None,
    root: Path,
    tokens: IdentityTokens,
    *,
    dictionary: frozenset[str] | None = None,
) -> GuardResult:
    """Scan tracked files under ``paths`` (all tracked files when ``None``) in ``root``."""

    config = load_guard_config(root)
    guard = IdentityGuard(tokens, config, dictionary=dictionary)
    result = GuardResult(config=config, inputs=dict(tokens.inputs))
    guard.gate.reset_words()
    for rel in _select(root, paths):
        exclusion = config.excluded(rel)
        if exclusion is not None:
            result.excluded[exclusion.glob] = result.excluded.get(exclusion.glob, 0) + 1
            continue
        findings = guard.scan_file(root, rel)
        if findings is None:
            result.skipped.append(rel)
            continue
        result.files += 1
        result.findings.extend(findings)
    result.findings.extend(f for f in guard.gate.fuzzy() if config.excluded(f.file) is None)
    return result


def scan_text(
    text: str,
    label: str,
    tokens: IdentityTokens,
    *,
    config: GuardConfig | None = None,
    dictionary: frozenset[str] | None = None,
) -> list[LeakFinding]:
    """Findings in a RENDERED string (a prompt, a tool description, ``--help`` output).

    ``label`` stands in for the file name in each finding. Exclusions do not apply (the text
    is not a tracked path); the repo's allowances do."""

    guard = IdentityGuard(tokens, config or GuardConfig(), dictionary=dictionary)
    out: list[LeakFinding] = []
    for n, line in enumerate(text.splitlines(), 1):
        if line.strip():
            out.extend(guard.check_line(line, label, f"line {n}"))
    return out

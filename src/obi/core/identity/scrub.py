"""Replace real identity in tracked files with the 134 mapping's fakes (feature 137, research D5).

Token-exact and guard-driven: the identity guard's FATAL findings say WHERE a real value is and of
which kind; the token at that spot is looked up (never guessed) in the private mapping, and every
occurrence of it in the file is replaced — case style, separators and structure preserved:

* places        → the mapping's tree words (``northfield-gw-01`` → ``birch-gw-01``); an ordinary
                  word that is also a site name is replaced only inside device-shaped names;
* addresses     → ``mapping.ip.map_ip`` (host octet and /24 membership kept);
* MACs / serials → mapping fakes, in the original spelling style;
* callsigns     → the CURATED ``callsign`` class only — an uncurated callsign fails the file
                  (radio fuzzy-matching tests depend on hand-picked spellings, research D6);
* people        → the mapping's ``user-N`` labels; nodes → stable ``!xxxxxxxx`` ids;
* coordinates   → longitude shifted by the private ``fixtures.dlon`` (an exact isometry);
* chat IDs, tailnet hosts, emails, secret values → fixed fictional forms.

Each original is archived first (owner-only, never overwritten); afterwards the file is scanned
again and anything still fatal is reported, never silently left. Output never carries a value.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from obi.core.demo.gate import FATAL, LeakFinding
from obi.core.demo.ipremap import IPV4_LITERAL
from obi.core.demo.mapping import PseudonymMap
from obi.core.demo.naming import GENERIC_TOKENS
from obi.core.identity.guard import (
    COORD_KEY,
    DELIM_PAIR,
    IPV4_PREFIX,
    LONLAT,
    IdentityGuard,
    coord_key_pairs,
    load_guard_config,
    tracked_files,
)
from obi.core.identity.tokens import IdentityTokens

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*")
_MAC = re.compile(
    r"[0-9A-Fa-f]{2}([:-])(?:[0-9A-Fa-f]{2}\1){4}[0-9A-Fa-f]{2}|(?<![0-9A-Fa-f])[0-9A-Fa-f]{12}(?![0-9A-Fa-f])"
)
_NODE = re.compile(r"![0-9a-fA-F]{8}")
_CHAT = re.compile(r"[UCWGD]\d[A-Z0-9]{7,9}")
_TAILNET = re.compile(r"(?i)(?:[a-z0-9-]+\.)+ts\.net")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}")
#: Categories the scrubber cannot fix mechanically: reported for a hand edit.
MANUAL = frozenset({"link", "fuzzy"})


class ScrubError(Exception):
    pass


def archive_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(Path.home(), ".local", "state")
    return Path(base) / "obi" / "identity" / "archive"


@dataclass
class FileResult:
    path: str
    replaced: dict[str, int] = field(default_factory=dict)  # category → occurrences
    remaining: dict[str, int] = field(default_factory=dict)  # category → fatal left after
    uncurated_callsigns: int = 0


@dataclass
class ScrubResult:
    files: list[FileResult] = field(default_factory=list)

    @property
    def failed(self) -> list[FileResult]:
        return [f for f in self.files if f.remaining or f.uncurated_callsigns]

    def report(self) -> str:
        lines = []
        for f in self.files:
            if not f.replaced and not f.remaining and not f.uncurated_callsigns:
                continue
            done = ", ".join(f"{c} {n}" for c, n in sorted(f.replaced.items())) or "nothing"
            line = f"{f.path}: replaced {done}"
            if f.remaining:
                left = ", ".join(f"{c} {n}" for c, n in sorted(f.remaining.items()))
                line += f"; STILL FATAL {left}"
            if f.uncurated_callsigns:
                line += f"; {f.uncurated_callsigns} callsign(s) need `obi identity callsign set`"
            lines.append(line)
        lines.append(f"{len(self.files)} file(s) examined, {len(self.failed)} need attention")
        return "\n".join(lines)


def _match_case(real: str, fake: str) -> str:
    real_parts, fake_parts = re.split(r"([-.])", real), re.split(r"([-.])", fake)
    if len(real_parts) == len(fake_parts) > 1:  # per-part case for names like ``BM-site-05``
        return "".join(_match_case(r, f) for r, f in zip(real_parts, fake_parts, strict=True))
    if real.isupper() and len(real) > 1:
        return fake.upper()
    if real[:1].isupper():
        return fake[:1].upper() + fake[1:]
    if real.islower():
        return fake.lower()
    return fake


def _fake_mac(mapping: PseudonymMap, real: str) -> str:
    digits = mapping.mac(real)
    sep = ":" if ":" in real else "-" if "-" in real else ""
    pairs = [digits[i : i + 2] for i in range(0, 12, 2)]
    text = sep.join(pairs) if sep else digits
    return text.upper() if real.upper() == real and any(c.isalpha() for c in real) else text


def _stable_hex(mapping: PseudonymMap, kind: str, real: str, width: int) -> str:
    return mapping._hexdigest(kind, real.casefold())[:width]  # noqa: SLF001 - seeded HMAC


class _Planner:
    """Turns one file's findings into a real→fake replacement plan."""

    def __init__(self, tokens: IdentityTokens, mapping: PseudonymMap, guard: IdentityGuard):
        self.tokens = tokens
        self.mapping = mapping
        self.guard = guard
        self.words: dict[str, tuple[str, str]] = {}  # casefolded real word → (fake, category)
        self.device_words: dict[str, str] = {}  # ordinary words replaced only in device names
        self.literals: dict[str, tuple[str, str]] = {}  # exact real text → (fake, category)
        self.uncurated: set[str] = set()

    def _token_at(self, text: str, offset: int) -> str | None:
        gate_re = self.guard.gate._token_re  # noqa: SLF001 - the gate's own matcher
        if gate_re is None:
            return None
        m = gate_re.match(text.casefold(), offset)
        return m.group(0) if m else None

    def _person_fake(self, real: str) -> str | None:
        folded = real.casefold()
        if folded in self.tokens.callsigns:
            try:
                return self.mapping.callsign(folded)
            except KeyError:
                self.uncurated.add(folded)
                return None
        return self.mapping.person(folded)

    def plan(self, finding: LeakFinding, text: str) -> None:
        off, cat = finding.offset, finding.category
        if cat == "ip" and finding.check == "prefix":
            m = IPV4_PREFIX.match(text, off)
            if m:
                parts = m.group(0).rstrip(".").split(".")
                full = ".".join([*parts, "1", "1"][:4])
                mapped = self.mapping.ip.map_ip(full).split(".")[: len(parts)]
                self.literals[m.group(0)] = (".".join(mapped) + ".", "ip-prefix")
        elif cat == "ip":
            m = IPV4_LITERAL.match(text, off)
            if m:
                self.literals[m.group(0)] = (self.mapping.ip.map_ip(m.group(0)), "ip")
        elif cat == "mac":
            m = _MAC.match(text, off)
            if m:
                self.literals[m.group(0)] = (_fake_mac(self.mapping, m.group(0)), "mac")
        elif cat == "serial":
            m = re.compile(r"[A-Za-z0-9]+").match(text, off)
            if m:
                self.literals[m.group(0)] = (self.mapping.serial(m.group(0)), "serial")
        elif cat == "node":
            m = _NODE.match(text, off)
            if m:
                node_id = "!" + _stable_hex(self.mapping, "nodeid", m.group(0), 8)
                self.literals[m.group(0)] = (node_id, "node")
        elif cat == "chat_id":
            m = _CHAT.match(text, off)
            if m:
                real = m.group(0)
                chat = f"{real[0]}0FAKE{_stable_hex(self.mapping, 'chat', real, 5).upper()}"
                self.literals[real] = (chat, "chat_id")
        elif cat == "hostname":
            m = _TAILNET.match(text, off)
            if m:
                label = m.group(0).split(".")[0]
                self.literals[m.group(0)] = (f"{label}.tailnet.example.net", "hostname")
        elif cat == "email":
            m = _EMAIL.match(text, off)
            if m:
                self.literals[m.group(0)] = ("ops@example.net", "email")
        elif cat == "domain":
            token = self._token_at(text, off)
            if token:
                self.words[token] = (self.mapping.domain(token), "domain")
        elif cat == "asn":
            m = re.compile(r"(AS ?N? ?)(\d+)").match(text, off)
            if m:
                asn = 64500 + int(_stable_hex(self.mapping, "asn", m.group(2), 4), 16) % 10
                self.literals[m.group(0)] = (f"{m.group(1)}{asn}", "asn")
        elif cat == "secret" and finding.check in ("token", "secret"):
            for secret in self.tokens.harvest.secrets:
                if text.find(secret, off, off + len(secret)) == off or secret in text:
                    fake = f"fakesecret{_stable_hex(self.mapping, 'secret', secret, 6)}"
                    self.literals[secret] = (fake, "secret")
        elif cat == "dictionary_word":
            m = _WORD.match(text, off)
            if m:
                word = m.group(0).casefold()
                self.device_words[word] = self.mapping.place(word)
        elif cat in ("place", "person", "denylist", "ssid"):
            token = self._token_at(text, off)
            if token is None:
                return
            replacement: str | None
            if cat == "place":
                replacement = self.mapping.place(token)
            elif cat == "ssid" and re.search(r"[-.]", token):
                # A link/AP name filed as an SSID (``siteA-ptp-siteB-60``): keep its structure —
                # each site word maps like a place, role tokens and digits stay.
                parts = re.split(r"([-.])", token)
                replacement = "".join(
                    p
                    if (
                        not p.isalpha()
                        or p in ("ptp", "ptmp", "gw", "ap")
                        or len(p) < 3
                        or p.casefold() in GENERIC_TOKENS
                    )
                    else self.mapping.place(p)
                    for p in parts
                )
            elif cat == "ssid":
                replacement = self.mapping.ssid(token)
            else:
                replacement = self._person_fake(token)
            if replacement is not None:
                self.words[token] = (replacement, "person" if cat == "denylist" else cat)


def _replace_words(text: str, words: dict[str, tuple[str, str]], counts: dict[str, int]) -> str:
    for real in sorted(words, key=len, reverse=True):
        fake, cat = words[real]
        rx = re.compile(rf"(?i)(?<![A-Za-z]){re.escape(real)}(?![A-Za-z])")

        def sub(m: re.Match[str], fake: str = fake, cat: str = cat) -> str:
            counts[cat] = counts.get(cat, 0) + 1
            return _match_case(m.group(0), fake)

        text = rx.sub(sub, text)
    return text


def _replace_device_words(
    text: str, words: dict[str, str], guard: IdentityGuard, counts: dict[str, int]
) -> str:
    if not words:
        return text
    rx = re.compile(r"[A-Za-z0-9]+(?:[-.][A-Za-z0-9]+)+")
    out_lines = []
    for line in text.split("\n"):
        pieces: list[str] = []
        last = 0
        for m in rx.finditer(line):
            span = m.group(0)
            if not guard._device_shaped(line, m.start()):  # noqa: SLF001
                continue
            parts = re.split(r"([-.])", span)
            changed = False
            for i, part in enumerate(parts):
                fake = words.get(part.casefold())
                if fake is not None:
                    parts[i] = _match_case(part, fake)
                    counts["dictionary_word"] = counts.get("dictionary_word", 0) + 1
                    changed = True
            if changed:
                pieces.append(line[last : m.start()])
                pieces.append("".join(parts))
                last = m.end()
        pieces.append(line[last:])
        out_lines.append("".join(pieces))
    return "\n".join(out_lines)


def _shift_coords(
    text: str, dlon: float | None, guard: IdentityGuard, counts: dict[str, int]
) -> str:
    """Shift each key-written longitude (and one-line pair) near the real site by ``dlon``."""

    if dlon is None:
        return text
    lines = text.split("\n")
    for (lat_line, _lo, lat), (lon_line, lon_off, lon) in coord_key_pairs(lines):
        if not guard.gate._near(lat, lon):  # noqa: SLF001
            continue
        line = lines[lon_line - 1]
        m = re.compile(r"-?\d{1,3}\.\d+|-?\d{7,10}").match(line, lon_off)
        if m is None:
            continue
        if "." in m.group(0):
            decimals = len(m.group(0).split(".")[1])
            new = f"{float(m.group(0)) + dlon:.{decimals}f}"
        else:  # integer microdegrees
            new = str(int(m.group(0)) + round(dlon * 1e7))
        lines[lon_line - 1] = line[: m.start()] + new + line[m.end() :]
        counts["coord"] = counts.get("coord", 0) + 1
        del lat_line
    from obi.core.demo.relocate import COORD_PAIR

    def pair(m: re.Match[str]) -> str:
        lat, lon = float(m.group(1)), float(m.group(3))
        if not guard.gate._near(lat, lon):  # noqa: SLF001
            return m.group(0)
        counts["coord"] = counts.get("coord", 0) + 1
        decimals = len(m.group(3).split(".")[1])
        return f"{m.group(1)}{m.group(2)}{lon + dlon:.{decimals}f}"

    def lonlat(m: re.Match[str]) -> str:
        lon, lat = float(m.group(1)), float(m.group(2))
        if not guard.gate._near(lat, lon):  # noqa: SLF001
            return m.group(0)
        counts["coord"] = counts.get("coord", 0) + 1
        decimals = len(m.group(1).split(".")[1])
        return m.group(0).replace(m.group(1), f"{lon + dlon:.{decimals}f}", 1)

    return "\n".join(
        LONLAT.sub(lonlat, DELIM_PAIR.sub(pair, COORD_PAIR.sub(pair, line))) for line in lines
    )


def _rename(
    root: Path, rel: str, tokens: IdentityTokens, mapping: PseudonymMap, guard: IdentityGuard
) -> str | None:
    """``git mv`` a file whose NAME carries identity to the same fakes its content uses."""

    planner = _Planner(tokens, mapping, guard)
    for f in guard.check_line(rel, rel, "path"):
        if f.severity == FATAL and f.category not in MANUAL:
            planner.plan(f, rel)
    counts: dict[str, int] = {}
    new = _replace_words(rel, planner.words, counts)
    new = _replace_device_words(new, planner.device_words, guard, counts)
    if new == rel or (root / new).exists():
        return None
    subprocess.run(["git", "-C", str(root), "mv", rel, new], check=True, capture_output=True)
    return new


def _archive(root: Path, rel: str, data: bytes) -> None:
    base = archive_dir()
    target = base / rel
    if target.exists():
        return  # never overwrite: the first archived copy is the real original
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    for parent in [target.parent, *target.parent.parents]:
        if parent == base.parent:
            break
        os.chmod(parent, 0o700)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    index = base / "index.json"
    entries = json.loads(index.read_text(encoding="utf-8")) if index.exists() else []
    try:
        commit = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip()
    except OSError:
        commit = ""
    entries.append(
        {
            "path": rel,
            "sha256": hashlib.sha256(data).hexdigest(),
            "archived_at": datetime.now(UTC).isoformat(),
            "commit": commit,
        }
    )
    tmp = index.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(entries, handle, indent=1)
    os.replace(tmp, index)


def scrub_paths(
    paths: list[str],
    root: Path,
    tokens: IdentityTokens,
    *,
    dry_run: bool = False,
    mapping_path: Path | None = None,
) -> ScrubResult:
    """Scrub the tracked files under ``paths``; see the module docstring."""

    if tokens.mapping is None:
        raise ScrubError("the private 134 mapping is required (run `obi demo snapshot` once)")
    mapping = tokens.mapping
    config = load_guard_config(root)
    guard = IdentityGuard(tokens, config)
    tracked = tracked_files(root)
    chosen = sorted(
        {
            t
            for p in paths
            for t in tracked
            if t == p.rstrip("/") or t.startswith(p.rstrip("/") + "/")
        }
    )
    result = ScrubResult()
    for rel in chosen:
        if config.excluded(rel):
            continue
        findings = guard.scan_file(root, rel)
        if findings is None:
            continue
        fatal = [f for f in findings if f.severity == FATAL]
        file_result = FileResult(rel)
        result.files.append(file_result)
        if not fatal:
            continue
        raw = (root / rel).read_bytes()
        text = raw.decode("utf-8")
        lines = text.split("\n")
        planner = _Planner(tokens, mapping, guard)
        for f in fatal:
            if not f.locator.startswith("line "):
                continue  # a file-NAME finding: renamed by hand (git mv), reported as remaining
            n = int(f.locator.split()[1])
            if f.category not in MANUAL and f.category != "coord":
                planner.plan(f, lines[n - 1])
        counts: dict[str, int] = {}
        new = _replace_words(text, planner.words, counts)
        ips = {r: f for r, (f, c) in planner.literals.items() if c == "ip"}
        if ips:  # whole-literal only: 10.0.0.1 must never rewrite inside 10.0.0.15

            def ip_sub(
                m: re.Match[str], ips: dict[str, str] = ips, counts: dict[str, int] = counts
            ) -> str:
                fake = ips.get(m.group(0))
                if fake is None:
                    return m.group(0)
                counts["ip"] = counts.get("ip", 0) + 1
                return fake

            new = IPV4_LITERAL.sub(ip_sub, new)
        for real, (fake, cat) in sorted(planner.literals.items(), key=lambda kv: -len(kv[0])):
            if cat == "ip":
                continue
            occurrences = new.count(real)
            if occurrences:
                counts[cat] = counts.get(cat, 0) + occurrences
                new = new.replace(real, fake)
        device_counts: dict[str, int] = {}
        new = _replace_device_words(new, planner.device_words, guard, device_counts)
        counts.update(device_counts)
        if device_counts:
            # The same file names the site on its own too (``sites = ["<site>"]``, a dict key,
            # ``"<Site>"``): rename those whole-word occurrences so the file stays consistent.
            for word, fake in planner.device_words.items():
                rx = re.compile(rf"(?i)(?<![A-Za-z0-9_]){re.escape(word)}(?![A-Za-z0-9_])")

                def standalone(
                    m: re.Match[str], fake: str = fake, counts: dict[str, int] = counts
                ) -> str:
                    counts["dictionary_word"] = counts.get("dictionary_word", 0) + 1
                    return _match_case(m.group(0), fake)

                new = rx.sub(standalone, new)
        new = _shift_coords(new, mapping.fixture_dlon, guard, counts)
        file_result.replaced = counts
        file_result.uncurated_callsigns = len(planner.uncurated)
        if dry_run or new == text:
            remaining = fatal if new == text else []
        else:
            _archive(root, rel, raw)
            (root / rel).write_text(new, encoding="utf-8")
            after = guard.scan_file(root, rel) or []
            remaining = [f for f in after if f.severity == FATAL]
        if not dry_run and any(f.locator == "path" for f in remaining):
            new_rel = _rename(root, rel, tokens, mapping, guard)
            if new_rel is not None:
                file_result.path = new_rel
                remaining = [f for f in remaining if f.locator != "path"]
        for f in remaining:
            file_result.remaining[f.category] = file_result.remaining.get(f.category, 0) + 1
    if not dry_run and mapping_path is not None:
        mapping.save(mapping_path)
    return result


def init_fixture_offset(mapping: PseudonymMap, *, rng: os.PathLike[str] | None = None) -> float:
    """Assign the private fixture longitude offset once (3°–10°, random sign); idempotent."""

    del rng
    if mapping.fixture_dlon is not None:
        return mapping.fixture_dlon
    raw = int.from_bytes(os.urandom(4), "big") / 2**32
    magnitude = 3.0 + 7.0 * raw
    sign = 1.0 if os.urandom(1)[0] & 1 else -1.0
    mapping.fixture_dlon = round(sign * magnitude, 6)
    return mapping.fixture_dlon


__all__ = [
    "COORD_KEY",
    "FileResult",
    "ScrubError",
    "ScrubResult",
    "archive_dir",
    "init_fixture_offset",
    "scrub_paths",
]

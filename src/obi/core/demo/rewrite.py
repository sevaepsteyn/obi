"""The free-text / JSON rewriter (feature 134, FR-007/FR-008, research R5).

One :class:`Rewriter` applies the private mapping to everything a source writes. Passes run in a
fixed order so no pass can re-expose what an earlier one removed:

1. **secrets** — never pseudonymized, only redacted: known secret literals, PEM blocks, API/bot
   token shapes, ``password=``-style key/value pairs (FR-007);
2. **contact data** — emails and phone numbers become fixed markers;
3. **network identity** — harvested domains, every MAC (any spelling), every IP literal (OIDs
   and version strings untouched), coordinate pairs near the network, and embedded timestamps;
4. **people and things** — harvested serials, Slack IDs, mesh node ids/names and people become
   their mapped values / personas;
5. **names** — hostname-shaped tokens and ``host=``-style values are rewritten word-by-word with
   EVERY place word (identifier context); event terms become generic equivalents; finally bare
   *distinctive* place words are rewritten in prose. Dictionary-word places (``office``) are
   left alone in prose (analyze C1).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from obi.core.configstore import redact_export
from obi.core.demo.harvest import Harvest
from obi.core.demo.mapping import PseudonymMap, normalize_mac
from obi.core.demo.naming import (
    GENERIC_TOKENS,
    is_dictionary_word,
    load_dictionary,
    rewrite_words,
    within_one_edit,
)
from obi.core.demo.relocate import Relocator
from obi.core.demo.timeshift import TimeShift
from obi.core.demo.trie import trie_pattern

REDACTED = "<redacted-by-obi>"
EMAIL_MARK = "<email>"
PHONE_MARK = "<phone>"

#: Event vocabulary → generic wording (generic words only; the private denylist adds real terms).
EVENT_EQUIVALENTS: dict[str, str] = {
    "playa": "valley floor",
    "deep playa": "back country",
    "esplanade": "main road",
    "center camp": "town center",
    "black rock city": "the town",
    "black rock": "the valley",
    "brc": "the town",
    "burning man": "the event",
    "burningman": "the event",
    "golden spike": "survey marker",
    "walk-in camping": "overflow camping",
}
_DEFAULT_EVENT_EQUIVALENT = "landmark"

#: JSON keys whose string values are identifiers (rewritten with every place word).
ID_KEYS = frozenset(
    {
        "identity", "name", "site", "label", "device", "host", "hostname", "target", "key",
        "router_id", "source_router", "a", "b", "end_a", "end_b", "console", "controller",
        "address", "node_id", "collectors",
    }
)  # fmt: skip
_SENSITIVE_KEY = re.compile(
    r"(pass(word|wd|phrase)?|secret|passphrase|(^|[-_])key$|(^|[-_])psk$|pre-shared-key|token"
    r"|api[-_]?key|community|private-key|auth.*password|enc.*password)",
    re.IGNORECASE,
)

_PEM = re.compile(r"-----BEGIN [A-Z ]+-----.*?-----END [A-Z ]+-----", re.DOTALL)
_TOKEN_SHAPE = re.compile(r"sk-ant-[A-Za-z0-9_-]+|xox[abprs]-[A-Za-z0-9-]+|xapp-[A-Za-z0-9-]+")
_SECRET_KV = re.compile(
    r"(?i)\b((?:[\w.-]*?)(?:password|passphrase|secret|psk|pre-shared-key|auth-key|priv-key"
    r"|public-key|private-key|preshared-key|api[-_]?key|token|community))"
    r"(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;\"']+)"
)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_PHONE = re.compile(r"(?<![\w+.])(?:\+?1[\s.-])?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?![\w.])")
_MAC_SEP = re.compile(
    r"(?<![0-9A-Fa-f:-])[0-9A-Fa-f]{2}([:-])(?:[0-9A-Fa-f]{2}\1){4}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:-])"
)
_MAC_DOT = re.compile(r"(?<![0-9A-Fa-f.])[0-9A-Fa-f]{4}(?:\.[0-9A-Fa-f]{4}){2}(?![0-9A-Fa-f.])")
_MAC_BARE = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{12}(?![0-9A-Fa-f])")
#: The RouterOS license key header line of an ``/export`` (identifies the physical device).
_SOFTWARE_ID = re.compile(r"(?m)^(#\s*software id\s*=\s*).*$")
_HOSTLIKE = re.compile(r"(?<![\w-])[A-Za-z0-9]+(?:[-_]+[A-Za-z0-9]+)+(?![\w-])")
#: A RouterOS ``comment=`` value: it names things (sites, people, peers), so it is rewritten in
#: identifier context — "cottonwood ranch relay" must not keep a dictionary-word place.
_COMMENT_KV = re.compile(r'(?<![\w-])(comment=)("(?:[^"\\]|\\.)*"|[^\s"]+)')
#: A DHCP lease event: the trailing hostname is whatever a visitor's device calls itself
#: ("Kenny-s-S23-Ultra"), so it becomes an opaque ``client-…`` whatever it is.
_DHCP_LEASE = re.compile(
    r"(?i)(\b(?:de)?assigned\s+\S+\s+(?:for|from|to)\s+"
    r"(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2})(\s+)(?!\()([^\s,;\"]+)"
)
#: An autonomous-system number ("AS64500", "ASN 64500") names the operator's network: every one
#: is mapped into the private range 64512–65534 (stable per number; private ones kept).
_ASN = re.compile(r"(?<![\w-])(AS|ASN)(\s?)(\d{1,10})(?![\d\w])")
_PRIVATE_ASN = (range(64496, 65535), range(4_200_000_000, 4_294_967_295))


def is_reserved_asn(number: int) -> bool:
    return any(number in r for r in _PRIVATE_ASN)


#: The region part of an IANA zone name, immediately before a ``_HOSTLIKE`` match.
_TZ_PREFIX = re.compile(
    r"(?:Africa|America|Antarctica|Asia|Atlantic|Australia|Europe|Indian|Pacific|US|Etc)"
    r"/(?:[A-Za-z_]+/)?\Z"
)
#: A chat-style handle ("@quill"), never part of an email address.
_HANDLE = re.compile(r"(?<![\w.@/])@([A-Za-z][\w.-]{2,})(?![\w@])")
_ID_KV = re.compile(
    r"(?i)(?<![\w-])(host|site|name|identity|device|target|label|from|to|neighbor)(=)(\"?)"
    r"([^\s\",;]+)"
)


def _phrase_re(terms: list[str], *, flags: int = re.IGNORECASE) -> re.Pattern[str] | None:
    source = trie_pattern(t.casefold() if flags & re.IGNORECASE else t for t in terms)
    if source is None:
        return None
    return re.compile(rf"(?<![\w]){source}(?![\w])", flags)


def _match_case_run(template: str, word: str) -> str:
    if template.isupper() and len(template) > 1:
        return word.upper()
    if template[:1].isupper():
        return word[:1].upper() + word[1:]
    return word


def _mac_like(template: str, hex12: str) -> str:
    """Re-spell ``hex12`` the way ``template`` spells a MAC (separator + case)."""

    upper = any(c.isupper() for c in template)
    out = hex12.upper() if upper else hex12
    if "." in template:
        return ".".join(out[i : i + 4] for i in range(0, 12, 4))
    sep = ":" if ":" in template else "-" if "-" in template else ""
    return sep.join(out[i : i + 2] for i in range(0, 12, 2))


class Rewriter:
    """Apply the mapping to text and JSON (one instance per snapshot run)."""

    def __init__(
        self,
        mapping: PseudonymMap,
        harvest: Harvest,
        shift: TimeShift,
        relocator: Relocator,
        dictionary: frozenset[str] | None = None,
    ) -> None:
        self.m = mapping
        self.h = harvest
        self.shift = shift
        self.relocator = relocator
        self.secrets = sorted(harvest.secrets, key=len, reverse=True)
        self._domain_re = _phrase_re(sorted(harvest.domains))
        serial_src = trie_pattern(x.casefold() for x in harvest.serials)
        self._serial_re = re.compile(serial_src, re.IGNORECASE) if serial_src else None
        self._slack_re = _phrase_re(sorted(harvest.slack_ids), flags=0)
        self._ssid_re = _phrase_re(sorted(s for s in harvest.ssids if len(s) >= 4), flags=0)
        node_src = trie_pattern(sorted(harvest.node_ids))
        self._node_id_re = re.compile(node_src) if node_src else None
        self._node_name_re = _phrase_re(sorted(harvest.node_names))
        fatal_terms = [t for t, fatal in harvest.event_terms.items() if fatal]
        words = load_dictionary() if dictionary is None else dictionary
        # A name made only of ordinary words ("Dash") is left alone: rewriting it would rename
        # the `[dash]` section and every "dash" in prose (the gate does not enforce it either).
        self._person_terms = sorted(
            p
            for p in set(harvest.persons) | set(fatal_terms)
            if not all(is_dictionary_word(w, words) for w in re.findall(r"[a-z]+", p))
        )
        person_src = trie_pattern(self._person_terms)
        # Like the event pass: never inside a dotted name or URL (a one-word name "com" must not
        # turn `example.com` into `example.user-2`); hostname-shaped tokens go through the
        # identifier pass, which knows each person word's owner.
        self._person_re = (
            re.compile(rf"(?<![\w.\-/:@]){person_src}(?![\w\-]|\.\w)", re.IGNORECASE)
            if person_src
            else None
        )
        self._event_terms = sorted(
            {
                t
                for t, fatal in harvest.event_terms.items()
                if not fatal and not all(w in GENERIC_TOKENS for w in re.findall(r"[a-z]+", t))
            }
            | set(EVENT_EQUIVALENTS)
        )
        source = trie_pattern(self._event_terms)
        # Never inside a hostname, URL or dotted name: there a label is a NAME and goes through
        # the place mapping (the first live run turned `x.event-name.org` into `x.the event.org`).
        self._event_re = (
            re.compile(rf"(?<![\w.\-/:@]){source}(?![\w\-]|\.\w)", re.IGNORECASE)
            if source
            else None
        )
        self._person_word_owner: dict[str, str] = {}
        for person in self._person_terms:
            for word in re.findall(r"[a-z]+", person):
                if len(word) >= 3 and not harvest.person_words.get(word, True):
                    self._person_word_owner.setdefault(word, person)
        self.truncations = 0
        #: Site words to rewrite even in PROSE (chat recordings opt in: there "the hub at
        #: showers" names a site, not plumbing). Empty for every other source.
        self.prose_sites: frozenset[str] = frozenset()
        self._dictionary = load_dictionary() if dictionary is None else dictionary
        self._distinct_by_len: dict[int, list[str]] = {}
        for place, is_dict in sorted(harvest.place_words.items()):
            if not is_dict and len(place) >= 5:
                self._distinct_by_len.setdefault(len(place), []).append(place)
        for word in sorted(self._person_word_owner):
            if len(word) >= 5 and word not in harvest.place_words:
                self._distinct_by_len.setdefault(len(word), []).append(word)
        self._near_cache: dict[str, str | None] = {}
        embedded = [w for w, d in harvest.place_words.items() if not d and len(w) >= 6] + [
            w for w in self._person_word_owner if len(w) >= 6
        ]
        embedded_src = trie_pattern(embedded)
        self._embedded_re = re.compile(embedded_src, re.IGNORECASE) if embedded_src else None

    # -- lookups ------------------------------------------------------------------------------

    def _near_place(self, word: str) -> str | None:
        """A misspelling (``northfieldd``, ``ofice``) of a distinctive place or person word maps
        like
        the word itself — the gate's fuzzy check would otherwise (rightly) refuse it."""

        cached = self._near_cache.get(word, "")
        if cached != "":
            return cached
        found: str | None = None
        if len(word) >= 5 and word.isalpha() and not is_dictionary_word(word, self._dictionary):
            for place in self._distinct_by_len.get(len(word), ()):  # substitution/transposition
                if within_one_edit(word, place):
                    found = place
                    break
            for delta in (-1, 1):
                if found is not None:
                    break
                for place in self._distinct_by_len.get(len(word) + delta, ()):
                    if within_one_edit(word, place):
                        found = place
                        break
        self._near_cache[word] = found
        return found

    def _ident_lookup(self, word: str) -> str | None:
        if word in self.h.place_words:
            return self.m.place(word)
        owner = self._person_word_owner.get(word)
        if owner is not None:
            return self.m.person(owner)
        return self._near_mapped(word)

    def _near_mapped(self, word: str) -> str | None:
        near = self._near_place(word)
        if near is None:
            return None
        if near in self.h.place_words:
            return self.m.place(near)
        return self.m.person(self._person_word_owner[near])

    def _prose_lookup(self, word: str) -> str | None:
        if self.h.place_words.get(word) is False or word in self.prose_sites:
            return self.m.place(word)
        owner = self._person_word_owner.get(word)
        if owner is not None:
            return self.m.person(owner)
        return self._near_mapped(word)

    # -- passes -------------------------------------------------------------------------------

    def scrub_secrets(self, text: str) -> str:
        for secret in self.secrets:
            if secret in text:
                text = text.replace(secret, REDACTED)
        if "-----BEGIN" in text:
            text = _PEM.sub(REDACTED, text)
        text = _TOKEN_SHAPE.sub(REDACTED, text)
        return _SECRET_KV.sub(
            lambda m: (
                m.group(0) if m.group(3) == REDACTED else f"{m.group(1)}{m.group(2)}{REDACTED}"
            ),
            text,
        )

    def _mac_sub(self, match: re.Match[str]) -> str:
        normalized = normalize_mac(match.group(0))
        if normalized is None:
            return match.group(0)
        return _mac_like(match.group(0), self.m.mac(normalized))

    def _mac_bare_sub(self, match: re.Match[str]) -> str:
        # Bare 12-hex runs are also git short-shas and hashes: only a MAC already known (harvested,
        # or mapped from a separator spelling) is rewritten.
        bare = match.group(0).casefold()
        if bare not in self.h.macs and not self.m.has_mac(bare):
            return match.group(0)
        return _mac_like(match.group(0), self.m.mac(match.group(0)))

    def _sub(self, pattern: re.Pattern[str] | None, text: str, fn: Callable[[str], str]) -> str:
        if pattern is None:
            return text
        return pattern.sub(lambda m: fn(m.group(0)), text)

    def text(self, text: str, *, identifier: bool = False) -> str:
        """Rewrite one free-text value (``identifier``: the whole value is a name)."""

        if not text:
            return text
        text = self.scrub_secrets(text)
        if "assigned" in text:
            text = _DHCP_LEASE.sub(
                lambda m: m.group(1) + m.group(2) + self.m.opaque("client", m.group(3)), text
            )
        if "AS" in text:
            text = _ASN.sub(self._asn_sub, text)
        if "@" in text:
            text = _HANDLE.sub(lambda m: "@" + self.m.person(m.group(1).casefold()), text)
        if "@" in text:
            text = _EMAIL.sub(
                lambda m: (
                    m.group(0)
                    if m.group(0)
                    .casefold()
                    .endswith(("@example.net", "@example.org", "@example.com"))
                    else EMAIL_MARK
                ),
                text,
            )
        if any(ch.isdigit() for ch in text):
            for phone in self.h.phones:
                text = text.replace(phone, PHONE_MARK)
            text = _PHONE.sub(PHONE_MARK, text)
        text = self._sub(self._domain_re, text, self.m.domain)
        if ":" in text or "-" in text:
            text = _MAC_SEP.sub(self._mac_sub, text)
        if "." in text:
            text = _MAC_DOT.sub(self._mac_sub, text)
        text = _MAC_BARE.sub(self._mac_bare_sub, text)
        text = self.m.ip.rewrite_text(text)
        text = self.relocator.rewrite_text(text)
        text = self.shift.text(text)
        text = self._sub(self._serial_re, text, self.m.serial)
        text = self._sub(self._slack_re, text, self.m.person)
        text = self._sub(self._ssid_re, text, self.m.ssid)
        if "!" in text:
            text = self._sub(self._node_id_re, text, self.m.node)
        text = self._sub(
            self._node_name_re, text, lambda name: self.m.node(self.h.node_names[name.casefold()])
        )
        text = self._sub(self._person_re, text, self.m.person)
        if identifier:
            return rewrite_words(text, self._ident_lookup)
        text = _HOSTLIKE.sub(self._hostlike_sub, text)
        text = _ID_KV.sub(
            lambda m: (
                f"{m.group(1)}{m.group(2)}{m.group(3)}"
                + rewrite_words(m.group(4), self._ident_lookup)
            ),
            text,
        )
        text = self._sub(self._event_re, text, self._event_sub)
        text = rewrite_words(text, self._prose_lookup)
        return self._embedded(text)

    def _embedded(self, text: str) -> str:
        """Distinctive names glued inside a longer run (``NorthfieldSouthgateofficefrontTV``,
        ``northfieldbakery``) — the gate matches long tokens as substrings, so must we."""

        if self._embedded_re is None:
            return text

        def sub(m: re.Match[str]) -> str:
            lo, hi = m.start(), m.end()
            while lo > 0 and text[lo - 1].isalnum():
                lo -= 1
            while hi < len(text) and text[hi].isalnum():
                hi += 1
            run = text[lo:hi].casefold()
            if run == m.group(0).casefold() or is_dictionary_word(run, self._dictionary):
                return m.group(0)
            word = m.group(0).casefold()
            if word in self.h.place_words:
                mapped = self.m.place(word)
            else:
                mapped = self.m.person(self._person_word_owner.get(word, word))
            return _match_case_run(m.group(0), mapped)

        return self._embedded_re.sub(sub, text)

    def ident(self, text: str) -> str:
        return self.text(text, identifier=True)

    def _event_sub(self, term: str) -> str:
        replacement = EVENT_EQUIVALENTS.get(term.casefold(), _DEFAULT_EVENT_EQUIVALENT)
        return replacement.capitalize() if term[:1].isupper() else replacement

    def export(self, text: str) -> str:
        """A RouterOS ``/export``: structural secret scrub first, then the text rewrite."""

        scrubbed = _SOFTWARE_ID.sub(
            rf"\g<1>{REDACTED}", redact_export(text, extra_secrets=tuple(self.secrets))
        )
        return "\n".join(self._comments(self.text(line)) for line in scrubbed.split("\n"))

    def _hostlike_sub(self, m: re.Match[str]) -> str:
        # An IANA time zone ("America/New_York") is a setting, never a place we must hide.
        if _TZ_PREFIX.search(m.string, 0, m.start()):
            return m.group(0)
        return rewrite_words(m.group(0), self._ident_lookup)

    def _asn_sub(self, m: re.Match[str]) -> str:
        number = int(m.group(3))
        if is_reserved_asn(number):
            return m.group(0)
        digest = int(self.m.opaque("asn", str(number), width=8).split("-")[1], 16)
        return f"{m.group(1)}{m.group(2)}{64512 + digest % 1022}"

    def _comments(self, line: str) -> str:
        if "comment=" not in line:
            return line
        return _COMMENT_KV.sub(
            lambda m: m.group(1) + rewrite_words(m.group(2), self._ident_lookup), line
        )

    # -- JSON ---------------------------------------------------------------------------------

    def json(self, obj: Any, key: str | None = None) -> Any:
        """Rewrite a JSON value recursively: keys are identifiers, secrets by key are redacted."""

        if isinstance(obj, dict):
            out: dict[str, Any] = {}
            relocated = self._relocate_pair(obj)
            for k, v in obj.items():
                new_key = self.ident(k) if isinstance(k, str) else k
                # A bare "key" field is an identifier (dash board items); only compound names
                # (api_key, ssh-key, psk…) are credentials.
                if (
                    isinstance(k, str)
                    and isinstance(v, str)
                    and k.casefold() != "key"
                    and _SENSITIVE_KEY.search(k)
                ):
                    out[new_key] = REDACTED
                elif k in relocated:
                    out[new_key] = relocated[k]
                else:
                    out[new_key] = self.json(v, k if isinstance(k, str) else None)
            return out
        if isinstance(obj, list):
            return [self.json(item, key) for item in obj]
        if isinstance(obj, str):
            if (key or "").casefold() == "ssid" and obj:
                return self.m.ssid(obj)
            return self.text(obj, identifier=(key or "").casefold() in ID_KEYS)
        if isinstance(obj, bool) or obj is None:
            return obj
        if isinstance(obj, int | float) and key is not None:
            return self._shift_number(key, obj)
        return obj

    def _relocate_pair(self, obj: dict[str, Any]) -> dict[str, Any]:
        """Relocated values for EVERY lat/lon pair in ``obj`` (a tower record carries two)."""

        moved: dict[str, Any] = {}
        for lat_key, lon_key in (
            ("latitude", "longitude"),
            ("lat", "lon"),
            ("device_latitude", "device_longitude"),
            ("site_latitude", "site_longitude"),
            ("gps_latitude", "gps_longitude"),
        ):
            lat, lon = obj.get(lat_key), obj.get(lon_key)
            if (
                isinstance(lat, int | float)
                and isinstance(lon, int | float)
                and not isinstance(lat, bool)
            ):
                new_lat, new_lon = self.relocator.point(float(lat), float(lon))
                moved[lat_key] = round(new_lat, 7)
                moved[lon_key] = round(new_lon, 7)
        return moved

    def _shift_number(self, key: str, value: int | float) -> int | float:
        k = key.casefold()
        if k.endswith("_us") and isinstance(value, int) and value > 10**15:
            return self.shift.us(value)
        if (k.endswith("_s") or k.endswith("_at")) and value > 10**9 and value < 10**11:
            shifted = self.shift.s(float(value))
            return int(shifted) if isinstance(value, int) else shifted
        return value

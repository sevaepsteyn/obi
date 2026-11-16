"""The private identifier set (feature 137, research D4): denylist + 134 mapping + config.

Every real value the guard looks for and the scrubber replaces comes from three PRIVATE files
on the operator's machine — never from source:

* the denylist (``$XDG_CONFIG_HOME/obi/demo-denylist.txt``): ``!term`` fatal, ``term`` warning,
  ``@word`` fatal place word;
* the 134 mapping (``$XDG_STATE_HOME/obi/demo/mapping.json``): every real key it has ever
  pseudonymised, the real /16 blocks and public /24s, and the real-site centroid;
* the live config (``$XDG_CONFIG_HOME/obi/config.toml``), via the config-only harvest (never the
  multi-GB stores).

Each input may be absent (a public clone has none): it is then reported ``skipped``, and only the
guard's built-in shape checks run. A present but group/world-readable file is refused.
"""

from __future__ import annotations

import ipaddress
import os
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from obi.core.config import ConfigError, default_config_path
from obi.core.demo import ProfileError
from obi.core.demo.harvest import PUBLIC_DOMAINS, Harvest, harvest_config, read_denylist
from obi.core.demo.mapping import PseudonymMap
from obi.core.demo.naming import GENERIC_TOKENS, is_dictionary_word, load_dictionary

LOADED = "loaded"
ABSENT = "skipped (absent)"
DISABLED = "skipped (disabled)"

_UNSET = object()


_EXTRA_WORDS = Path(__file__).resolve().parent / "data" / "common_words.txt"


def identity_dictionary() -> frozenset[str]:
    """Ordinary words: the 134 dictionary plus the vendored networking/English extras that the
    system word list lacks (``offline``, ``uplink``) — a real site named with one of these is then
    fatal only as an identifier, never in prose."""

    from obi.core.demo.naming import _VENDORED_WORDS

    extra: set[str] = set()
    for path in (_EXTRA_WORDS, _VENDORED_WORDS):  # the 134 generic place-word list too
        extra.update(
            line.strip().casefold()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        )
    return load_dictionary() | frozenset(extra)


def default_denylist_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(Path.home(), ".config")
    return Path(base) / "obi" / "demo-denylist.txt"


def default_mapping_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(Path.home(), ".local", "state")
    return Path(base) / "obi" / "demo" / "mapping.json"


@dataclass
class IdentityTokens:
    """What is real, by category. Never serialised, never printed."""

    harvest: Harvest
    real_blocks: list[ipaddress.IPv4Network] = field(default_factory=list)
    public24: list[ipaddress.IPv4Network] = field(default_factory=list)
    centroid: tuple[float, float] | None = None
    mapping: PseudonymMap | None = None
    inputs: dict[str, str] = field(default_factory=dict)
    #: Casefolded radio/mesh call names (config roster + aliases + curated mapping class): the
    #: scrubber only ever replaces these with CURATED fakes (research D6).
    callsigns: set[str] = field(default_factory=set)

    @property
    def has_private(self) -> bool:
        return any(v == LOADED for v in self.inputs.values())


#: Public service domains obi's own drivers and docs name (vendor APIs, data providers). A
#: deployment's config naturally contains them, but they identify no deployment (137).
_SERVICE_DOMAINS = (
    *PUBLIC_DOMAINS, "ui.com", "ts.net", "open-meteo.com", "weather.gov", "github.com",
    "githubusercontent.com", "slack.com", "anthropic.com", "amazonaws.com", "usgs.gov",
)  # fmt: skip


#: Well-known public resolver /24s a network naturally talks to — never deployment identity.
_PUBLIC_SERVICE_NETS = frozenset(
    ipaddress.IPv4Network(n)
    for n in ("8.8.8.0/24", "8.8.4.0/24", "1.1.1.0/24", "1.0.0.0/24", "9.9.9.0/24",
              "149.112.112.0/24", "208.67.222.0/24", "208.67.220.0/24")
)  # fmt: skip
#: Factory-default private /16s nearly every network has somewhere (home routers, RouterOS's
#: own defaults). A deployment that uses one gains no identity from it: its exact hosts stay
#: fatal as tokens, but the whole /16 is not a "real block" (generic examples live there).
_UBIQUITOUS_BLOCKS = frozenset(
    ipaddress.IPv4Network(n) for n in ("10.0.0.0/16", "192.168.0.0/16", "172.16.0.0/16")
)


def is_public_service_address(addr: ipaddress.IPv4Address) -> bool:
    return any(addr in net for net in _PUBLIC_SERVICE_NETS)


def _is_service_domain(domain: str) -> bool:
    d = domain.casefold()
    return any(d == p or d.endswith("." + p) for p in _SERVICE_DOMAINS)


def _check_private(path: Path) -> None:
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ConfigError(f"private file {path} must be owner-only (chmod 600)")


def harvest_from_mapping(mapping: PseudonymMap, dictionary: frozenset[str]) -> Harvest:
    """Every real key the mapping has pseudonymised, as a :class:`Harvest`."""

    h = Harvest()
    for word in mapping.assignments("place"):
        if word not in GENERIC_TOKENS:
            h.place_words.setdefault(word, is_dictionary_word(word, dictionary))
    for cls in ("person", "callsign"):
        for real in mapping.assignments(cls):
            h.add_person(real, dictionary)
    for real in mapping.assignments("node"):
        if real.startswith("!"):
            h.node_ids.add(real)
        else:
            h.add_person(real, dictionary)
    h.ssids.update(mapping.assignments("ssid"))
    h.domains.update(mapping.assignments("domain"))
    h.macs.update(mapping.assignments("mac"))
    h.serials.update(mapping.assignments("serial"))
    for real in mapping.ip.state().get("cgnat", {}):
        h.add_address(real)
    return h


def _merge(into: Harvest, other: Harvest) -> None:
    for word, is_dict in other.place_words.items():
        into.place_words.setdefault(word, is_dict)
    for word, is_dict in other.person_words.items():
        into.person_words.setdefault(word, is_dict)
    for term, fatal in other.event_terms.items():
        into.event_terms[term] = into.event_terms.get(term, False) or fatal
    for name in ("names", "persons", "node_ids", "ssids", "ips", "networks", "macs", "serials",
                 "slack_ids", "phones", "secrets", "domains"):  # fmt: skip
        getattr(into, name).update(getattr(other, name))
    into.node_names.update(other.node_names)
    into.coords.extend(other.coords)
    into.site_coords.extend(other.site_coords)


def _config_callsigns(raw: dict[str, object]) -> set[str]:
    out: set[str] = set()
    radio = raw.get("radio") if isinstance(raw.get("radio"), dict) else {}
    mesh = raw.get("meshtastic") if isinstance(raw.get("meshtastic"), dict) else {}
    assert isinstance(radio, dict) and isinstance(mesh, dict)
    for key in ("known_callers", "operator_handles"):
        out.update(str(v) for v in radio.get(key) or [] if isinstance(v, str))
    for alias, name in (radio.get("caller_aliases") or {}).items():
        out.update((str(alias), str(name)))
    out.update(str(v) for v in (mesh.get("names") or {}).values() if isinstance(v, str))
    for alias, target in (mesh.get("aliases") or {}).items():
        out.add(str(alias))
        out.update(str(t) for t in ([target] if isinstance(target, str) else target or []))
    return {c.strip().casefold() for c in out if c.strip() and not c.startswith("!")}


def build_tokens(
    *,
    denylist: Path | None | object = _UNSET,
    mapping: Path | None | object = _UNSET,
    config: Path | None | object = _UNSET,
    dictionary: frozenset[str] | None = None,
) -> IdentityTokens:
    """Load whichever private inputs exist. ``None`` disables an input; omitted = default path."""

    words = identity_dictionary() if dictionary is None else dictionary
    paths: dict[str, Path | None] = {
        "denylist": default_denylist_path() if denylist is _UNSET else denylist,  # type: ignore[dict-item]
        "mapping": default_mapping_path() if mapping is _UNSET else mapping,  # type: ignore[dict-item]
        "config": default_config_path() if config is _UNSET else config,  # type: ignore[dict-item]
    }
    inputs: dict[str, str] = {}
    present: dict[str, Path] = {}
    for name, path in paths.items():
        if path is None:
            inputs[name] = DISABLED
        elif not Path(path).exists():
            inputs[name] = ABSENT
        else:
            _check_private(Path(path))
            present[name] = Path(path)
            inputs[name] = LOADED

    h = Harvest()
    tokens = IdentityTokens(harvest=h, inputs=inputs)
    if "mapping" in present:
        try:
            pmap = PseudonymMap.load_or_create(present["mapping"])
        except ProfileError as exc:
            raise ConfigError(str(exc)) from exc
        tokens.mapping = pmap
        tokens.callsigns.update(pmap.assignments("callsign"))
        _merge(h, harvest_from_mapping(pmap, words))
        state = pmap.ip.state()
        tokens.real_blocks = [
            net
            for net in (ipaddress.IPv4Network(b) for b in state.get("block16", {}))
            if net not in _UBIQUITOUS_BLOCKS
        ]
        tokens.public24 = [
            net
            for net in (ipaddress.IPv4Network(b) for b in state.get("public24", {}))
            if net not in _PUBLIC_SERVICE_NETS
        ]
        centroid = pmap.geo.get("centroid")
        if isinstance(centroid, list) and len(centroid) == 2:
            tokens.centroid = (float(centroid[0]), float(centroid[1]))
            h.site_coords.append(tokens.centroid)
    if "denylist" in present:
        read_denylist(present["denylist"], h)
        # An ``@word`` that is ordinary English (a site called "box") is checked like any
        # dictionary-word place — fatal in a device name, not in every CSS ``box-sizing``.
        for word in list(h.place_words):
            if not h.place_words[word] and is_dictionary_word(word, words):
                h.place_words[word] = True
    if "config" in present:
        try:
            raw = tomllib.loads(present["config"].read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"could not read {present['config']}: {exc}") from exc
        _merge(h, harvest_config(raw, words))
        tokens.callsigns.update(_config_callsigns(raw))
    h.domains = {d for d in h.domains if not _is_service_domain(d)}
    # A service domain's own labels ("githubusercontent") are not place words either.
    service_labels = {label for d in _SERVICE_DOMAINS for label in d.split(".")}
    h.place_words = {w: v for w, v in h.place_words.items() if w not in service_labels}
    return tokens

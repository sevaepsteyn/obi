"""Maintenance mode (feature 135): ONE effective set of things deliberately off.

Two sources, unioned: the operator's ``[maintenance]`` config declaration (sites, devices,
UISP site names, whole signals) and the UISP controller's per-device ``meta.maintenance``
flag. Every alarm path — dash evaluators, pager, status band, ``dash_status``, the watch
passes, the radio station's DMs — asks the same :class:`Maintenance` matcher, so no path can
silence something another still alarms on (research D1).

Matching rules (research D2):

- A name is normalised to its first whitespace-separated word, casefolded, with a domain
  suffix stripped (``sitea-ptp-siteb-05 MIDDLE`` → ``sitea-ptp-siteb-05``;
  ``sitec-ptp-sited-70.net.example.net`` → ``sitec-ptp-sited-70``), then split into
  tokens on ``-`` and ``→``.
- A **site** matches any name containing it as a WHOLE token — ``noc`` matches ``noc-gw``,
  ``siteb-ptp-oak-60`` and ``sitea-oak-11``, never ``oakmont-gw``.
- A **device** matches the normalised name exactly.
- A **UISP site** matches a UISP site name exactly (casefold, whitespace-collapsed).
- A **UISP-flagged** radio matches its own identity, and — for link/probe names only — its
  ``ptp``-less alias (``sitea-ptp-sitec-11`` ⇒ netwatch ``sitea-sitec-11``). A flag never
  spreads to the rest of its site.

Pure: no I/O except :func:`read_radio_maintenance`, which fails toward alarming.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from obi.core.config import MaintenanceSettings, parse_maintenance

UISP_LABEL = "uisp"

_TOKEN_SPLIT = re.compile(r"[-→]+")
_IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def normalise(name: str) -> tuple[str, tuple[str, ...]]:
    """``(full, tokens)`` for a device/link/host name (see module docstring)."""

    words = name.split()
    if not words:
        return "", ()
    full = words[0].casefold()
    if not _IPV4.match(full):
        full = full.split(".", 1)[0]
    tokens = tuple(t for t in _TOKEN_SPLIT.split(full) if t)
    return full, tokens


def _site_key(name: str) -> str:
    return " ".join(name.split()).casefold()


def _ptp_alias(full: str) -> str | None:
    tokens = [t for t in full.split("-") if t]
    if "ptp" not in tokens:
        return None
    return "-".join(t for t in tokens if t != "ptp")


@dataclass(frozen=True)
class MaintenanceSummary:
    """What is in maintenance this cycle — carried on the board for every renderer (FR-011)."""

    sites: list[str] = field(default_factory=list)
    devices: list[str] = field(default_factory=list)
    uisp_sites: list[str] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)
    reason: str = ""
    uisp_flagged: int = 0
    uisp_cached: bool = False
    unmatched: list[str] = field(default_factory=list)
    items: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "sites": list(self.sites),
            "devices": list(self.devices),
            "uisp_sites": list(self.uisp_sites),
            "signals": list(self.signals),
            "reason": self.reason,
            "uisp_flagged": self.uisp_flagged,
            "uisp_cached": self.uisp_cached,
            "unmatched": list(self.unmatched),
            "items": self.items,
        }

    @classmethod
    def from_json(cls, payload: object) -> MaintenanceSummary | None:
        if not isinstance(payload, dict):
            return None

        def _strs(key: str) -> list[str]:
            value = payload.get(key)
            return [str(v) for v in value] if isinstance(value, list) else []

        return cls(
            sites=_strs("sites"),
            devices=_strs("devices"),
            uisp_sites=_strs("uisp_sites"),
            signals=_strs("signals"),
            reason=str(payload.get("reason") or ""),
            uisp_flagged=int(payload.get("uisp_flagged") or 0),
            uisp_cached=bool(payload.get("uisp_cached")),
            unmatched=_strs("unmatched"),
            items=int(payload.get("items") or 0),
        )

    def line(self) -> str:
        """One human line: ``sites a, b; signals mesh; 21 UISP-flagged radios — reason``."""

        parts: list[str] = []
        if self.sites:
            parts.append("sites " + ", ".join(self.sites))
        if self.devices:
            parts.append("devices " + ", ".join(self.devices))
        if self.uisp_sites:
            parts.append("UISP sites " + ", ".join(self.uisp_sites))
        if self.signals:
            parts.append("signals " + ", ".join(self.signals))
        if self.uisp_flagged:
            noun = "radio" if self.uisp_flagged == 1 else "radios"
            cached = " (from cache)" if self.uisp_cached else ""
            parts.append(f"{self.uisp_flagged} UISP-flagged {noun}{cached}")
        text = "; ".join(parts) or "nothing"
        if self.reason:
            text += f" — {self.reason}"
        if self.unmatched:
            text += f" [matches nothing: {', '.join(self.unmatched)}]"
        return text


@dataclass(frozen=True)
class Maintenance:
    """The effective maintenance set: config declaration ∪ UISP-flagged identities."""

    settings: MaintenanceSettings = field(default_factory=MaintenanceSettings)
    uisp_flagged: frozenset[str] = frozenset()  # normalised full names
    uisp_aliases: frozenset[str] = frozenset()  # ptp-less aliases (link/probe names only)
    uisp_cached: bool = False
    uisp_count: int = 0  # distinct RAW flagged identities (two may normalise alike)

    @classmethod
    def build(
        cls,
        settings: MaintenanceSettings | None = None,
        uisp_flagged: Iterable[str] = (),
        *,
        uisp_cached: bool = False,
    ) -> Maintenance:
        raw = {name.strip() for name in uisp_flagged if name.strip()}
        flagged = {normalise(name)[0] for name in raw}
        flagged.discard("")
        aliases = {alias for full in flagged if (alias := _ptp_alias(full))}
        return cls(
            settings=settings or MaintenanceSettings(),
            uisp_flagged=frozenset(flagged),
            uisp_aliases=frozenset(aliases),
            uisp_cached=uisp_cached,
            uisp_count=len(raw),
        )

    @property
    def is_empty(self) -> bool:
        return self.settings.is_empty and not self.uisp_flagged

    @property
    def config_label(self) -> str:
        reason = self.settings.reason
        return f"config: {reason}" if reason else "config"

    def _config_name(self, name: str) -> bool:
        full, tokens = normalise(name)
        if not full:
            return False
        if full in self.settings.devices:
            return True
        sites = self.settings.sites
        return bool(sites) and any(token in sites for token in tokens)

    def device(self, name: str | None) -> str | None:
        """Label when a device identity is in maintenance, else ``None``."""

        if not name:
            return None
        if self._config_name(name):
            return self.config_label
        if normalise(name)[0] in self.uisp_flagged:
            return UISP_LABEL
        return None

    def link(self, *names: str | None) -> str | None:
        """Label when ANY of a probe/link's names (router, target, endpoints, label) matches.

        Also honors a UISP-flagged radio's ``ptp``-less alias, which is how router-side
        netwatch probes name the same link.
        """

        present = [n for n in names if n]
        for name in present:
            if self._config_name(name):
                return self.config_label
        for name in present:
            full = normalise(name)[0]
            if full in self.uisp_flagged or full in self.uisp_aliases:
                return UISP_LABEL
        return None

    def uisp_site(self, name: str | None) -> str | None:
        if name and _site_key(name) in self.settings.uisp_sites:
            return self.config_label
        return None

    def log_host_terms(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """``(sites, exact_hosts)`` for log-row hosts — the SQL-expressible rule set.

        The watch window is filtered twice, in SQL (before the LIMIT) and in Python (the
        backstop), and the two must admit EXACTLY the same rows (the 013 parity gate). So log
        hosts use a deliberately simple rule both can express: lowercase; a site matches a whole
        ``-``-delimited token; devices, UISP-flagged identities and declared UISP site names
        match the whole host exactly.
        """

        s = self.settings
        exact = set(s.devices) | set(s.uisp_sites) | set(self.uisp_flagged)
        return tuple(s.sites), tuple(sorted(exact))

    def log_host(self, host: str | None) -> str | None:
        """Label when a log row's host is in maintenance (see :meth:`log_host_terms`)."""

        if not host:
            return None
        h = host.lower()
        sites, exact = self.log_host_terms()
        if h in exact:
            if h in self.settings.devices or h in self.settings.uisp_sites:
                return self.config_label
            return UISP_LABEL
        for site in sites:
            if h == site or h.startswith(f"{site}-") or h.endswith(f"-{site}") or f"-{site}-" in h:
                return self.config_label
        return None

    def signal(self, name: str) -> str | None:
        return self.config_label if name in self.settings.signals else None

    def unmatched(
        self, seen_names: Iterable[str], seen_uisp_sites: Iterable[str] = ()
    ) -> list[str]:
        """Config entries (sites/devices/UISP sites) that matched nothing seen — likely typos."""

        fulls: set[str] = set()
        tokens: set[str] = set()
        for name in seen_names:
            full, toks = normalise(name)
            if full:
                fulls.add(full)
                tokens.update(toks)
        sites_seen = {_site_key(s) for s in seen_uisp_sites}
        out = [f"site {s}" for s in self.settings.sites if s not in tokens]
        out += [f"device {d}" for d in self.settings.devices if d not in fulls]
        if sites_seen:  # no site list to check against ⇒ unverifiable, never a false alarm
            out += [f"UISP site {u}" for u in self.settings.uisp_sites if u not in sites_seen]
        return out

    def entries(self) -> list[str]:
        """Every effective entry, sorted — the unit of the FR-023 change audit."""

        s = self.settings
        out = [f"site:{x}" for x in s.sites]
        out += [f"device:{x}" for x in s.devices]
        out += [f"uisp_site:{x}" for x in s.uisp_sites]
        out += [f"signal:{x}" for x in s.signals]
        out += [f"uisp:{x}" for x in self.uisp_flagged]
        return sorted(out)

    def fingerprint(self) -> str:
        return json.dumps({"entries": self.entries(), "reason": self.settings.reason})

    def summary(self, *, items: int = 0, unmatched: Iterable[str] = ()) -> MaintenanceSummary:
        s = self.settings
        return MaintenanceSummary(
            sites=list(s.sites),
            devices=list(s.devices),
            uisp_sites=list(s.uisp_sites),
            signals=list(s.signals),
            reason=s.reason,
            uisp_flagged=self.uisp_count,
            uisp_cached=self.uisp_cached,
            unmatched=list(unmatched),
            items=items,
        )


def read_radio_maintenance(config_path: Path) -> str | None:
    """The radio station's runtime check (research D8): is ``radio`` declared in maintenance?

    Re-reads ONLY the ``[maintenance]`` section of the config file on every call, so entering
    or leaving maintenance needs no restart of the long-running station. ANY failure — missing
    file, bad TOML, invalid section — returns ``None``: the DM goes out (fail toward alarming).
    """

    try:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
        settings = parse_maintenance(data.get("maintenance"))
    except Exception:  # noqa: BLE001 — fail toward alarming, whatever went wrong
        return None
    return Maintenance.build(settings).signal("radio")

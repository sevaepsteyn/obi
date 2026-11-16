"""Curated SNMP metric profiles (feature 012).

A *profile* is a small, named, code-defined set of standard metrics obi knows how to read and
label — not an arbitrary-OID interface and not a compiled MIB (research.md D2). OIDs are numeric
and stable (standard MIB-2 / HOST-RESOURCES-MIB / MIKROTIK-MIB), so no MIB files are needed at
query time.

This module is shared with the future historical-collector feature (it polls the same profiles),
so it imports only :mod:`obi.core.models` and stays free of any I/O or vendor adapter.

v1 ships two profiles — ``system`` and ``interfaces``; environmental metrics beyond device/CPU
temperature are deferred (spec FR-015). CPU load and memory come from HOST-RESOURCES-MIB (supported
by Mikrotik RouterOS and net-snmp alike); CPU/board temperature come from the MIKROTIK-MIB
(``mtxrHealth``, enterprise 14988) and are absent-with-note on non-Mikrotik devices.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from obi.core.models import (
    METRIC_CPU_LOAD_PERCENT,
    METRIC_SYS_NAME,
    METRIC_SYS_UPTIME,
    MetricKind,
)


class Reduce(StrEnum):
    """How a walked column collapses to one scalar :class:`AggregateDef` sample."""

    AVG = "avg"
    SUM = "sum"
    MIN = "min"
    MAX = "max"


@dataclass(frozen=True)
class MetricDef:
    """One scalar metric: its logical ``name``, the base object ``oid`` (no instance suffix — the
    adapter appends ``.0`` for the scalar GET), and its :class:`MetricKind`."""

    name: str
    oid: str
    kind: MetricKind


@dataclass(frozen=True)
class AggregateDef:
    """A scalar derived by walking the table column ``oid`` and reducing its numeric values (e.g.
    the average per-core ``hrProcessorLoad`` → one overall CPU-load percentage)."""

    name: str
    oid: str
    kind: MetricKind
    reduce: Reduce = Reduce.AVG


@dataclass(frozen=True)
class TableColumn:
    """One column of a walked table. ``oid`` is the column subtree the adapter walks (rows keyed by
    the index suffix). ``fallback_oid`` is walked only to fill rows the primary column leaves empty
    (e.g. a 64-bit counter falling back to its 32-bit sibling), noted on the sample."""

    name: str
    oid: str
    kind: MetricKind
    fallback_oid: str | None = None
    fallback_kind: MetricKind | None = None


@dataclass(frozen=True)
class MetricProfile:
    """A curated set of scalars, aggregates, an optional walked table, and an optional
    host-resources memory read. Pure data; the adapter does the I/O."""

    name: str
    scalars: tuple[MetricDef, ...] = ()
    aggregates: tuple[AggregateDef, ...] = ()
    table: tuple[TableColumn, ...] = ()
    table_index_label: str | None = None  # the column whose value labels each row (e.g. ifName)
    memory_from_hr_storage: bool = False  # read RAM total/used/free from the hrStorage table
    # Feature 096 bespoke readers (the hrStorage-flag style): each is ONE whole-subtree walk
    # with cross-column logic the generic table mechanism cannot express (per-row unit
    # normalization / the DDM-absence rule).
    health_sensor_table: bool = False  # RouterOS v7 mtxrHealth name/value/unit gauge table
    optical_table: bool = False  # mtxrOptical DDM table (skips non-diagnostics modules)


# MIKROTIK-MIB mtxrHealth (1.3.6.1.4.1.14988.1.1.3) scalar leaves; absent on non-Mikrotik devices.
# Both temperatures are reported in TENTHS of a degree Celsius (deci-°C) — 430 means 43.0 °C. The
# store keeps the raw wire value; metricstore._divisor_for scales it to whole °C at query time.
_MTXR_CPU_TEMPERATURE = "1.3.6.1.4.1.14988.1.1.3.11"  # mtxrHlProcessorTemperature
_MTXR_TEMPERATURE = "1.3.6.1.4.1.14988.1.1.3.10"  # mtxrHlTemperature (board)

# The ``system`` profile: device identity + uptime (universal MIB-2 scalars), CPU load and memory
# from HOST-RESOURCES-MIB (Mikrotik + net-snmp), and Mikrotik CPU/board temperature. A device that
# does not expose an OID reports it absent-with-note (FR-004).
SYSTEM_PROFILE = MetricProfile(
    name="system",
    scalars=(
        MetricDef("sysDescr", "1.3.6.1.2.1.1.1", MetricKind.STRING),
        MetricDef(METRIC_SYS_NAME, "1.3.6.1.2.1.1.5", MetricKind.STRING),
        MetricDef(METRIC_SYS_UPTIME, "1.3.6.1.2.1.1.3", MetricKind.TIMETICKS),
        MetricDef("cpuTemperature", _MTXR_CPU_TEMPERATURE, MetricKind.INTEGER),
        MetricDef("boardTemperature", _MTXR_TEMPERATURE, MetricKind.INTEGER),
    ),
    aggregates=(
        # hrProcessorLoad (per-core %) averaged into one overall CPU-load percentage.
        AggregateDef(METRIC_CPU_LOAD_PERCENT, "1.3.6.1.2.1.25.3.3.1.2", MetricKind.GAUGE),
    ),
    memory_from_hr_storage=True,
)

# The ``interfaces`` profile: one row per interface (indexed by ifIndex), labeled by ifName, with
# operational status, 64-bit in/out octet counters (32-bit fallback), and the 32-bit in/out
# error/discard counters (IF-MIB has no 64-bit variant of these — feature 021, research.md D7).
# RAW counters only — rate math over time is the historical collector's job (feature 021 / D4).
INTERFACES_PROFILE = MetricProfile(
    name="interfaces",
    table=(
        TableColumn("ifName", "1.3.6.1.2.1.31.1.1.1.1", MetricKind.STRING),
        TableColumn("ifOperStatus", "1.3.6.1.2.1.2.2.1.8", MetricKind.INTEGER),
        TableColumn(
            "ifHCInOctets",
            "1.3.6.1.2.1.31.1.1.1.6",
            MetricKind.COUNTER64,
            fallback_oid="1.3.6.1.2.1.2.2.1.10",
            fallback_kind=MetricKind.COUNTER,
        ),
        TableColumn(
            "ifHCOutOctets",
            "1.3.6.1.2.1.31.1.1.1.10",
            MetricKind.COUNTER64,
            fallback_oid="1.3.6.1.2.1.2.2.1.16",
            fallback_kind=MetricKind.COUNTER,
        ),
        # Error/discard counters (feature 021) — 32-bit IF-MIB Counter32; no HC sibling exists.
        TableColumn("ifInErrors", "1.3.6.1.2.1.2.2.1.14", MetricKind.COUNTER),
        TableColumn("ifOutErrors", "1.3.6.1.2.1.2.2.1.20", MetricKind.COUNTER),
        TableColumn("ifInDiscards", "1.3.6.1.2.1.2.2.1.13", MetricKind.COUNTER),
        TableColumn("ifOutDiscards", "1.3.6.1.2.1.2.2.1.19", MetricKind.COUNTER),
    ),
    table_index_label="ifName",
)

# The ``health`` profile (feature 096): the RouterOS v7 named-sensor gauge table
# (mtxrHealth …3.100.1 — name/value/unit columns). Sensor sets vary per model (a CCR2004
# reports temps+fans+PSU states, an RB5009 temps+voltages), so the reader walks the table
# ONCE and keys each series by sensor name; devices without the table (pre-v7) report it
# absent-with-note. Values are normalized at ingest to the store's raw conventions
# (°C ×10 deci-degrees; dV/dA/dW/RPM/status as-is) — see specs/096 data-model.md.
HEALTH_PROFILE = MetricProfile(name="health", health_sensor_table=True)

# The ``optics`` profile (feature 096): per-module SFP DDM readings from mtxrOptical
# (…19.1.1), one whole-subtree walk. A module whose temperature AND supply voltage are both
# zero answers no diagnostics — it yields a note and NO samples (never stored zeros).
# Power values are signed milli-dBm (T007-verified against the live monitor).
OPTICS_PROFILE = MetricProfile(name="optics", optical_table=True)

PROFILES: dict[str, MetricProfile] = {
    SYSTEM_PROFILE.name: SYSTEM_PROFILE,
    INTERFACES_PROFILE.name: INTERFACES_PROFILE,
    HEALTH_PROFILE.name: HEALTH_PROFILE,
    OPTICS_PROFILE.name: OPTICS_PROFILE,
}

#: Profile names, for CLI/chat option enums and the default selection.
PROFILE_NAMES: tuple[str, ...] = tuple(PROFILES)
DEFAULT_PROFILE = SYSTEM_PROFILE.name


def get_profile(name: str) -> MetricProfile:
    """Return the named profile, or raise :class:`KeyError` for an unknown name."""

    return PROFILES[name]

"""SNMP metrics adapter — strictly read-only (Constitution Principles I, IV; feature 012).

Reads curated metric *profiles* (``snmp_profiles``) from any device obi already knows. SNMP is a
cross-vendor protocol, so this single adapter implements the :class:`MetricsSource` contract for
every vendor; vendor differences live in the profiles (OID sets), not here.

Read-only **by construction**: the only SNMP operations are GET / GETNEXT / GETBULK — there is no
SET path anywhere in this module (Principle I). ``pysnmp`` is the only place it is imported, and it
is imported **lazily** inside the real transport so the module loads (and tests run) without it; the
transport is **injectable** (``transport_factory=``) so the read/assembly logic is exercised offline
against a fake, exactly as ``mikrotik.py`` injects its ``connect`` (research.md D3, D5).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from obi.core.audit import record_device_command
from obi.core.config import Secret, SnmpSettings
from obi.core.drivers.snmp_profiles import MetricProfile, Reduce
from obi.core.models import (
    METRIC_MEM_FREE_KB,
    METRIC_MEM_TOTAL_KB,
    METRIC_MEM_USED_KB,
    MetricKind,
    MetricReport,
    MetricSample,
    MetricTarget,
    SnmpOutcome,
    _utcnow,
)

# A bounded ceiling on table rows so one huge interface table cannot produce an unbounded render;
# the excess is dropped with an explicit note (never silently truncated).
_MAX_TABLE_ROWS = 512

# HOST-RESOURCES-MIB hrStorage table columns, for the RAM (main-memory) row.
_HR_STORAGE_TYPE = "1.3.6.1.2.1.25.2.3.1.2"
_HR_STORAGE_DESCR = "1.3.6.1.2.1.25.2.3.1.3"
_HR_STORAGE_ALLOC = "1.3.6.1.2.1.25.2.3.1.4"
_HR_STORAGE_SIZE = "1.3.6.1.2.1.25.2.3.1.5"
_HR_STORAGE_USED = "1.3.6.1.2.1.25.2.3.1.6"
_HR_TYPE_RAM = "1.3.6.1.2.1.25.2.1.2"  # hrStorageRam

# Feature 096: the RouterOS v7 named-sensor table (mtxrHealth) — columns name/value/unit.
_MTXR_HEALTH_TABLE = "1.3.6.1.4.1.14988.1.1.3.100.1"
_HEALTH_NAME_COLUMN = "2"
_HEALTH_VALUE_COLUMN = "3"
_HEALTH_UNIT_COLUMN = "4"
_HEALTH_UNIT_CELSIUS = 1  # value is plain °C on the wire → stored ×10 (deci-°C convention)
_HEALTH_KNOWN_UNITS = {1, 2, 3, 4, 5, 6}  # °C, RPM, dV, dA, dW, status — dV/dA/dW stay raw

# Feature 096: the mtxrOptical DDM table. Column id → stored metric name (wavelength,
# vendor, and serial columns are deliberately not stored).
_MTXR_OPTICAL_TABLE = "1.3.6.1.4.1.14988.1.1.19.1.1"
_OPTICS_NAME_COLUMN = "2"
_OPTICS_TEMP_COLUMN = "6"
_OPTICS_SUPPLY_COLUMN = "7"
_OPTICS_TX_POWER_COLUMN = "9"
_OPTICS_RX_POWER_COLUMN = "10"
_OPTICS_COLUMNS: dict[str, str] = {
    "3": "optics:rxLoss",
    "4": "optics:txFault",
    _OPTICS_TEMP_COLUMN: "optics:temperature",
    _OPTICS_SUPPLY_COLUMN: "optics:supplyVoltageMv",
    "8": "optics:txBiasMa",
    _OPTICS_TX_POWER_COLUMN: "optics:txPowerMdbm",
    _OPTICS_RX_POWER_COLUMN: "optics:rxPowerMdbm",
}
# When BOTH power columns read exactly 0 mdBm the module measures no power (partial DDM);
# the bias current is equally meaningless then and is dropped with them.
_OPTICS_POWER_COLUMNS = ("8", _OPTICS_TX_POWER_COLUMN, _OPTICS_RX_POWER_COLUMN)


@dataclass
class RawVar:
    """One raw SNMP varbind a transport returns: the responding OID, a best-effort kind, and the
    converted scalar value (``None`` for noSuchObject/noSuchInstance/endOfMibView)."""

    oid: str
    kind: MetricKind
    value: int | float | str | None


@dataclass
class ResolvedAuth:
    """The effective SNMP credentials for one target (defaults + any per-address override).

    Secrets stay wrapped in :class:`Secret` here and are revealed only at the moment the real
    transport builds its request — never logged, audited, or rendered (FR-007).
    """

    version: str
    port: int
    timeout: float
    retries: int
    community: Secret | None = None
    username: str | None = None
    auth_proto: str = "SHA"
    auth_key: Secret | None = None
    priv_proto: str = "AES"
    priv_key: Secret | None = None


class SnmpTransportError(Exception):
    """A classified per-device SNMP failure carrying the obi :class:`SnmpOutcome`."""

    def __init__(self, outcome: SnmpOutcome, detail: str) -> None:
        super().__init__(detail)
        self.outcome = outcome
        self.detail = detail


class SnmpTransport(Protocol):
    """Per-target SNMP primitive I/O. Implementations issue ONLY GET/GETNEXT/GETBULK.

    Both methods raise :class:`SnmpTransportError` (with the right outcome) on a whole-device
    failure; a per-OID ``noSuchObject`` is returned as a :class:`RawVar` with ``value=None``.
    """

    def get(self, oids: Sequence[str]) -> dict[str, RawVar]:
        """GET the given instance OIDs; return ``requested-oid → RawVar`` (missing keys allowed)."""
        ...

    def walk(self, base_oid: str) -> list[RawVar]:
        """GETBULK-walk the subtree under ``base_oid``; return one :class:`RawVar` per row."""
        ...


TransportFactory = Callable[[MetricTarget, ResolvedAuth, MetricProfile], SnmpTransport]


def _resolve_auth(settings: SnmpSettings, target: MetricTarget) -> ResolvedAuth:
    """Apply any per-address override on top of the defaults."""

    override = settings.overrides.get(target.address)
    version = override.version if override and override.version else settings.version
    community = override.community if override and override.community else settings.community
    return ResolvedAuth(
        version=version,
        port=settings.port,
        timeout=settings.timeout,
        retries=settings.retries,
        community=community,
        username=settings.v3_username,
        auth_proto=settings.v3_auth_proto,
        auth_key=settings.v3_auth_key,
        priv_proto=settings.v3_priv_proto,
        priv_key=settings.v3_priv_key,
    )


def _suffix(base: str, full: str) -> str:
    """The table-row index: the part of ``full`` after ``base.`` (or ``full`` if not a child)."""

    prefix = base + "."
    return full[len(prefix):] if full.startswith(prefix) else full


def _index_sort_key(index: str) -> tuple[int, ...]:
    """Numeric sort key for a (possibly multi-part) table index; non-numeric parts sort first."""

    return tuple(int(part) if part.isdigit() else -1 for part in index.split("."))


class SnmpMetricsSource:
    """Read-only SNMP adapter implementing the :class:`MetricsSource` protocol (feature 012)."""

    name = "snmp"

    def __init__(
        self, settings: SnmpSettings, *, transport_factory: TransportFactory | None = None
    ) -> None:
        self._settings = settings
        self._factory: TransportFactory = transport_factory or _default_transport_factory

    def fetch_metrics(
        self, target: MetricTarget, *, profile: MetricProfile, timeout: float
    ) -> MetricReport:
        """READ-ONLY. Read ``profile`` from ``target``; total (never raises), bounded by timeout."""

        auth = _resolve_auth(self._settings, target)
        auth.timeout = timeout
        try:
            transport = self._factory(target, auth, profile)
            samples: list[MetricSample] = []
            notes: list[str] = []
            if profile.scalars:
                samples.extend(self._read_scalars(transport, profile))
            if profile.aggregates:
                samples.extend(self._read_aggregates(transport, profile))
            if profile.memory_from_hr_storage:
                samples.extend(self._read_hr_memory(transport))
            if profile.table:
                table_samples, table_notes = self._read_table(transport, profile)
                samples.extend(table_samples)
                notes.extend(table_notes)
            if profile.health_sensor_table:
                samples.extend(self._read_health_sensors(transport))
            if profile.optical_table:
                optics_samples, optics_notes = self._read_optics(transport)
                samples.extend(optics_samples)
                notes.extend(optics_notes)
        except SnmpTransportError as exc:
            return MetricReport(
                target=target, outcome=exc.outcome, profile=profile.name, notes=[exc.detail]
            )
        except Exception as exc:  # noqa: BLE001 - the source MUST be total (FR-005)
            return MetricReport(
                target=target,
                outcome=SnmpOutcome.UNREACHABLE,
                profile=profile.name,
                notes=[f"unexpected SNMP error: {exc}"],
            )
        return MetricReport(
            target=target,
            outcome=SnmpOutcome.REACHABLE,
            samples=samples,
            gathered_at=_utcnow(),
            notes=notes,
            profile=profile.name,
        )

    @staticmethod
    def _read_scalars(transport: SnmpTransport, profile: MetricProfile) -> list[MetricSample]:
        instances = {f"{definition.oid}.0": definition for definition in profile.scalars}
        got = transport.get(list(instances))
        samples: list[MetricSample] = []
        for oid, definition in instances.items():
            raw = got.get(oid)
            if raw is not None and raw.value is not None:
                samples.append(
                    MetricSample(name=definition.name, oid=oid, kind=definition.kind,
                                 value=raw.value)
                )
            else:
                samples.append(
                    MetricSample(name=definition.name, oid=oid, kind=definition.kind,
                                 value=None, note="unavailable")
                )
        return samples

    @staticmethod
    def _read_aggregates(transport: SnmpTransport, profile: MetricProfile) -> list[MetricSample]:
        """Walk each aggregate's column and reduce its numeric rows to one scalar sample."""

        samples: list[MetricSample] = []
        for agg in profile.aggregates:
            numbers = [
                float(var.value) for var in transport.walk(agg.oid)
                if isinstance(var.value, (int, float))
            ]
            if not numbers:
                samples.append(
                    MetricSample(name=agg.name, oid=agg.oid, kind=agg.kind, value=None,
                                 note="unavailable")
                )
                continue
            if agg.reduce is Reduce.AVG:
                value: float = round(sum(numbers, 0.0) / len(numbers), 1)
            elif agg.reduce is Reduce.SUM:
                value = sum(numbers, 0.0)
            elif agg.reduce is Reduce.MIN:
                value = min(numbers)
            else:
                value = max(numbers)
            samples.append(MetricSample(name=agg.name, oid=agg.oid, kind=agg.kind, value=value))
        return samples

    @staticmethod
    def _read_hr_memory(transport: SnmpTransport) -> list[MetricSample]:
        """Read RAM total/used/free (KB) from the HOST-RESOURCES-MIB main-memory row.

        Locates the RAM row by hrStorageType (or a "memory" description), then derives bytes from
        hrStorageSize/Used × hrStorageAllocationUnits. Absent rows ⇒ noted-absent (FR-004)."""

        names = (METRIC_MEM_TOTAL_KB, METRIC_MEM_USED_KB, METRIC_MEM_FREE_KB)

        def _indexed(base: str) -> dict[str, RawVar]:
            return {_suffix(base, var.oid): var for var in transport.walk(base)}

        types = _indexed(_HR_STORAGE_TYPE)
        ram_index = next(
            (index for index, var in types.items() if str(var.value) == _HR_TYPE_RAM), None
        )
        if ram_index is None:
            descrs = _indexed(_HR_STORAGE_DESCR)
            ram_index = next(
                (
                    index
                    for index, var in descrs.items()
                    if var.value is not None
                    and "memory" in str(var.value).lower()
                    and "virtual" not in str(var.value).lower()
                ),
                None,
            )

        def _cell(rows: dict[str, RawVar]) -> int | float | str | None:
            if ram_index is None:
                return None
            var = rows.get(ram_index)
            return var.value if var is not None else None

        size = _cell(_indexed(_HR_STORAGE_SIZE))
        used = _cell(_indexed(_HR_STORAGE_USED))
        unit_raw = _cell(_indexed(_HR_STORAGE_ALLOC))
        unit = unit_raw if isinstance(unit_raw, (int, float)) and unit_raw else 1024

        if not isinstance(size, (int, float)) or not isinstance(used, (int, float)):
            return [
                MetricSample(name=name, oid=_HR_STORAGE_SIZE, kind=MetricKind.GAUGE,
                             value=None, note="unavailable")
                for name in names
            ]

        unit_bytes = int(unit) if isinstance(unit, (int, float)) else 1024
        total_kb = int(size) * unit_bytes // 1024
        used_kb = int(used) * unit_bytes // 1024
        free_kb = max(total_kb - used_kb, 0)
        return [
            MetricSample(METRIC_MEM_TOTAL_KB, f"{_HR_STORAGE_SIZE}.{ram_index}",
                         MetricKind.GAUGE, total_kb),
            MetricSample(METRIC_MEM_USED_KB, f"{_HR_STORAGE_USED}.{ram_index}",
                         MetricKind.GAUGE, used_kb),
            MetricSample(METRIC_MEM_FREE_KB, f"{_HR_STORAGE_SIZE}.{ram_index}",
                         MetricKind.GAUGE, free_kb),
        ]

    @staticmethod
    def _read_table(
        transport: SnmpTransport, profile: MetricProfile
    ) -> tuple[list[MetricSample], list[str]]:
        notes: list[str] = []
        primary: dict[str, dict[str, RawVar]] = {}
        fallback: dict[str, dict[str, RawVar]] = {}
        indices: set[str] = set()

        for column in profile.table:
            rows = {_suffix(column.oid, var.oid): var for var in transport.walk(column.oid)}
            primary[column.name] = rows
            indices.update(rows)
            if column.fallback_oid is not None:
                fb = {_suffix(column.fallback_oid, var.oid): var
                      for var in transport.walk(column.fallback_oid)}
                fallback[column.name] = fb
                indices.update(fb)

        ordered = sorted(indices, key=_index_sort_key)
        if len(ordered) > _MAX_TABLE_ROWS:
            notes.append(
                f"interface table truncated to {_MAX_TABLE_ROWS} of {len(ordered)} rows"
            )
            ordered = ordered[:_MAX_TABLE_ROWS]

        label_column = profile.table_index_label
        labels: dict[str, str] = {}
        if label_column is not None and label_column in primary:
            for index, var in primary[label_column].items():
                if var.value is not None:
                    labels[index] = str(var.value)

        samples: list[MetricSample] = []
        for index in ordered:
            label = labels.get(index)
            for column in profile.table:
                raw = primary[column.name].get(index)
                value = raw.value if raw is not None else None
                oid = raw.oid if raw is not None else f"{column.oid}.{index}"
                kind = column.kind
                note: str | None = None
                if value is None and column.fallback_oid is not None:
                    fb_raw = fallback.get(column.name, {}).get(index)
                    if fb_raw is not None and fb_raw.value is not None:
                        value = fb_raw.value
                        oid = fb_raw.oid
                        kind = column.fallback_kind or column.kind
                        note = "32-bit fallback"
                if value is None and note is None:
                    note = "unavailable"
                samples.append(
                    MetricSample(name=column.name, oid=oid, kind=kind, value=value,
                                 index=index, label=label, note=note)
                )
        return samples, notes


    # --- feature 096: health-sensor and optics tables (one whole-subtree walk each) -----------

    @staticmethod
    def _split_table_walk(
        transport: SnmpTransport, base: str
    ) -> dict[str, dict[str, RawVar]]:
        """ONE walk of ``base``, rows regrouped as ``{column-id: {row-index: var}}``.

        The v7 health and optics tables need cross-column logic (unit normalization, the
        DDM-absence rule), so they are read with a single subtree walk — exactly one extra
        walk per profile per device — and split here instead of one walk per column."""

        columns: dict[str, dict[str, RawVar]] = {}
        for var in transport.walk(base):
            suffix = _suffix(base, var.oid)
            column, _, index = suffix.partition(".")
            if index:
                columns.setdefault(column, {})[index] = var
        return columns

    @staticmethod
    def _read_health_sensors(transport: SnmpTransport) -> list[MetricSample]:
        """Read the RouterOS v7 named-sensor table (mtxrHealth …3.100.1).

        Each row is (name, value, unit); the series key is the sensor NAME (sets vary per
        model). Normalization to the store's raw conventions happens here: unit 1 (plain °C
        on the wire) is stored ×10 as deci-°C; dV/dA/dW/RPM/status values stay raw. A device
        without the table (pre-v7 RouterOS, non-Mikrotik) reports absent-with-note (FR-003).
        """

        columns = SnmpMetricsSource._split_table_walk(transport, _MTXR_HEALTH_TABLE)
        names = {
            index: str(var.value)
            for index, var in columns.get(_HEALTH_NAME_COLUMN, {}).items()
            if var.value is not None
        }
        if not names:
            return [
                MetricSample(name="health", oid=_MTXR_HEALTH_TABLE, kind=MetricKind.GAUGE,
                             value=None, note="unavailable")
            ]
        values = columns.get(_HEALTH_VALUE_COLUMN, {})
        units = columns.get(_HEALTH_UNIT_COLUMN, {})
        samples: list[MetricSample] = []
        for index in sorted(names, key=_index_sort_key):
            sensor = names[index]
            oid = f"{_MTXR_HEALTH_TABLE}.{_HEALTH_VALUE_COLUMN}.{index}"
            raw = values.get(index)
            value = raw.value if raw is not None else None
            if not isinstance(value, (int, float)):
                samples.append(
                    MetricSample(name=f"health:{sensor}", oid=oid, kind=MetricKind.GAUGE,
                                 value=None, index=index, label=sensor, note="unavailable")
                )
                continue
            unit_var = units.get(index)
            unit = unit_var.value if unit_var is not None else None
            note: str | None = None
            if unit == _HEALTH_UNIT_CELSIUS:
                value = value * 10  # plain °C on the wire → the store's deci-°C convention
            elif not (isinstance(unit, int) and unit in _HEALTH_KNOWN_UNITS):
                note = f"unknown unit {unit}"  # stored raw, never scaled by a guess
            samples.append(
                MetricSample(name=f"health:{sensor}", oid=oid, kind=MetricKind.GAUGE,
                             value=value, index=index, label=sensor, note=note)
            )
        return samples

    @staticmethod
    def _read_optics(transport: SnmpTransport) -> tuple[list[MetricSample], list[str]]:
        """Read per-module SFP DDM readings from mtxrOptical (…19.1.1), one subtree walk.

        A module whose temperature AND supply voltage both read zero answers no diagnostics
        (0 mV supply is physically impossible on a live module — verified on core-gw-1):
        it yields a note and NO samples, so zeros are never stored as readings (FR-002/003).
        Power values are signed milli-dBm (T007-verified ÷1000 at query time)."""

        columns = SnmpMetricsSource._split_table_walk(transport, _MTXR_OPTICAL_TABLE)
        names = {
            index: str(var.value)
            for index, var in columns.get(_OPTICS_NAME_COLUMN, {}).items()
            if var.value is not None
        }
        if not names:
            marker = MetricSample(name="optics", oid=_MTXR_OPTICAL_TABLE,
                                  kind=MetricKind.GAUGE, value=None, note="unavailable")
            return [marker], []
        samples: list[MetricSample] = []
        notes: list[str] = []
        for index in sorted(names, key=_index_sort_key):
            port = names[index]
            row = {col: columns.get(col, {}).get(index) for col in _OPTICS_COLUMNS}
            cells = {
                col: var.value
                for col, var in row.items()
                if var is not None and isinstance(var.value, (int, float))
            }
            if not cells.get(_OPTICS_TEMP_COLUMN) and not cells.get(_OPTICS_SUPPLY_COLUMN):
                notes.append(f"{port}: no DDM diagnostics")
                continue
            # Partial DDM (seen live on a gateway's DACs): temp/voltage are real but BOTH
            # power columns read exactly 0 mdBm — the platform's "not measured" signature.
            # Storing those zeros would fabricate readings (FR-003), so the power metrics
            # (and the meaningless bias) are dropped for that module; the rest stay.
            unmeasured_power = (
                cells.get(_OPTICS_TX_POWER_COLUMN) == 0
                and cells.get(_OPTICS_RX_POWER_COLUMN) == 0
            )
            for col, metric in _OPTICS_COLUMNS.items():
                if col not in cells:
                    continue  # an absent cell yields no sample — never a zero-fill
                if unmeasured_power and col in _OPTICS_POWER_COLUMNS:
                    continue
                value = cells[col]
                if col == _OPTICS_TEMP_COLUMN:
                    value = value * 10  # plain °C → deci-°C, the store's convention
                samples.append(
                    MetricSample(name=metric, oid=f"{_MTXR_OPTICAL_TABLE}.{col}.{index}",
                                 kind=MetricKind.GAUGE, value=value, index=index, label=port)
                )
        return samples, notes


# --- real pysnmp-backed transport (exercised against a live agent; never in unit tests) -------


def _default_transport_factory(
    target: MetricTarget, auth: ResolvedAuth, profile: MetricProfile
) -> SnmpTransport:
    return _PysnmpTransport(target.address, auth)


def _classify_indication(indication: object) -> SnmpOutcome:
    text = str(indication).lower()
    if "timeout" in text or "timed out" in text:
        return SnmpOutcome.TIMED_OUT
    if any(marker in text for marker in ("auth", "digest", "username", "security", "decrypt")):
        return SnmpOutcome.AUTH_FAILED
    return SnmpOutcome.UNREACHABLE


def _convert(value: Any) -> int | float | str | None:
    """Convert one pysnmp varbind value to a plain scalar; ``None`` for the no-such markers."""

    if value.__class__.__name__ in ("NoSuchObject", "NoSuchInstance", "EndOfMibView"):
        return None
    try:
        return int(value)  # Integer/Counter/Gauge/TimeTicks rfc1902 types are int-convertible
    except (TypeError, ValueError):
        pretty = getattr(value, "prettyPrint", None)
        return str(pretty()) if callable(pretty) else str(value)


def _kind_of(value: int | float | str | None) -> MetricKind:
    return MetricKind.GAUGE if isinstance(value, (int, float)) else MetricKind.STRING


class _PysnmpTransport:
    """The real SNMP transport. ``pysnmp`` is imported lazily here so the module never requires it
    at import time, and a new event loop per call keeps it safe inside the engine's thread pool."""

    def __init__(self, address: str, auth: ResolvedAuth) -> None:
        self._address = address
        self._auth = auth

    def get(self, oids: Sequence[str]) -> dict[str, RawVar]:
        import asyncio

        # feature 042: one audited device_command per logical GET (never per PDU).
        start = time.monotonic()
        try:
            responses = asyncio.run(self._async_get(list(oids)))
        except Exception as exc:
            record_device_command(
                self._address,
                "snmp",
                f"get {' '.join(oids)}",
                duration_ms=(time.monotonic() - start) * 1000.0,
                outcome="error",
                error=str(exc),
            )
            raise
        out: dict[str, RawVar] = {}
        for requested, value in zip(oids, responses, strict=False):
            scalar = _convert(value)
            out[requested] = RawVar(oid=requested, kind=_kind_of(scalar), value=scalar)
        record_device_command(
            self._address,
            "snmp",
            f"get {' '.join(oids)}",
            response_items=len(out),
            duration_ms=(time.monotonic() - start) * 1000.0,
        )
        return out

    def walk(self, base_oid: str) -> list[RawVar]:
        import asyncio

        # feature 042: one audited device_command per logical walk (never per GETNEXT).
        start = time.monotonic()
        try:
            rows = asyncio.run(self._async_walk(base_oid))
        except Exception as exc:
            record_device_command(
                self._address,
                "snmp",
                f"walk {base_oid}",
                duration_ms=(time.monotonic() - start) * 1000.0,
                outcome="error",
                error=str(exc),
            )
            raise
        out: list[RawVar] = []
        for oid, value in rows:
            scalar = _convert(value)
            out.append(RawVar(oid=str(oid), kind=_kind_of(scalar), value=scalar))
        record_device_command(
            self._address,
            "snmp",
            f"walk {base_oid}",
            response_items=len(out),
            duration_ms=(time.monotonic() - start) * 1000.0,
        )
        return out

    def _auth_data(self) -> object:
        from pysnmp.hlapi.v3arch.asyncio import (
            CommunityData,
            UsmUserData,
            usmAesCfb128Protocol,
            usmHMACSHAAuthProtocol,
        )

        if self._auth.version == "v3":
            return UsmUserData(
                self._auth.username,
                authKey=self._auth.auth_key.reveal() if self._auth.auth_key else None,
                privKey=self._auth.priv_key.reveal() if self._auth.priv_key else None,
                authProtocol=usmHMACSHAAuthProtocol,
                privProtocol=usmAesCfb128Protocol,
            )
        community = self._auth.community.reveal() if self._auth.community else ""
        return CommunityData(community, mpModel=1)

    async def _async_get(self, oids: list[str]) -> list[object]:
        from pysnmp.hlapi.v3arch.asyncio import (
            ContextData,
            ObjectIdentity,
            ObjectType,
            SnmpEngine,
            UdpTransportTarget,
            get_cmd,
        )

        transport = await UdpTransportTarget.create(
            (self._address, self._auth.port),
            timeout=self._auth.timeout,
            retries=self._auth.retries,
        )
        indication, status, _index, var_binds = await get_cmd(
            SnmpEngine(),
            self._auth_data(),
            transport,
            ContextData(),
            *[ObjectType(ObjectIdentity(oid)) for oid in oids],
        )
        if indication:
            raise SnmpTransportError(_classify_indication(indication), str(indication))
        if status:
            raise SnmpTransportError(SnmpOutcome.UNREACHABLE, str(status.prettyPrint()))
        return [value for _name, value in var_binds]

    async def _async_walk(self, base_oid: str) -> list[tuple[object, object]]:
        from pysnmp.hlapi.v3arch.asyncio import (
            ContextData,
            ObjectIdentity,
            ObjectType,
            SnmpEngine,
            UdpTransportTarget,
            bulk_walk_cmd,
        )

        transport = await UdpTransportTarget.create(
            (self._address, self._auth.port),
            timeout=self._auth.timeout,
            retries=self._auth.retries,
        )
        rows: list[tuple[object, object]] = []
        walker = bulk_walk_cmd(
            SnmpEngine(),
            self._auth_data(),
            transport,
            ContextData(),
            0,
            25,
            ObjectType(ObjectIdentity(base_oid)),
            lexicographicMode=False,
        )
        async for indication, status, _index, var_binds in walker:
            if indication:
                raise SnmpTransportError(_classify_indication(indication), str(indication))
            if status:
                break
            rows.extend((name, value) for name, value in var_binds)
        return rows

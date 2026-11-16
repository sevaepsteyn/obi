"""Test doubles: an offline fake VendorDriver and a recording RouterOS connection.

The fake driver keeps core/engine tests fast and fully offline (research.md §8). The
recording connection lets the Mikrotik adapter's field-mapping be exercised against recorded
read-only fixtures without a real device, and records every path read so contract tests can
assert the adapter is strictly read-only.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from obi.chat.context import load_context
from obi.chat.session import ChatRuntime
from obi.core.audit import AuditRecorder, AuditTrail
from obi.core.config import (
    BrcSettings,
    ChatSettings,
    Config,
    ConfigBackupSettings,
    LogsSettings,
    MeshtasticSettings,
    NetboxSettings,
    RouterosSettings,
    Secret,
    SikluSettings,
    SiteSettings,
    SlackSettings,
    SnmpSettings,
    TrafficSettings,
    UispSettings,
    UnifiNetworkSettings,
    UnifiSiteManagerSettings,
    VictoriaSettings,
    WatchSettings,
    WeatherSettings,
    WledSettings,
)
from obi.core.drivers.base import (
    DocumentationReport,
    ProbeOutcome,
    ProbeResult,
    SourceReport,
    TsLabelValuesReport,
    TsQueryReport,
    TsSeriesReport,
)
from obi.core.drivers.mikrotik import MikrotikDriver
from obi.core.drivers.siklu import SikluDriver, SikluSession
from obi.core.drivers.snmp import (
    RawVar,
    SnmpMetricsSource,
    SnmpTransport,
    SnmpTransportError,
    TransportFactory,
)
from obi.core.drivers.snmp_profiles import MetricProfile
from obi.core.filter import name_matches
from obi.core.inventory import Inventory, InventoryEntry
from obi.core.llm import LLMResponse, TextBlock, ToolUseBlock, Usage
from obi.core.models import (
    Device,
    DeviceStatus,
    DiscoveryScope,
    LogEvent,
    LogReport,
    MetricKind,
    MetricReport,
    MetricSample,
    MetricTarget,
    NeighborLink,
    NetboxDevice,
    OspfNeighbor,
    Reachability,
    SnmpOutcome,
    SourceOutcome,
)
from obi.core.prompt import system_prompt
from obi.core.toolset import ToolContext, WledSource, build_chat_tools
from obi.core.watch import DeliveryResult, WatchFinding
from obi.core.watchstore import WatchStore
from obi.slack.handler import SlackHandler
from obi.slack.sessions import SessionStore

FIXTURES_DIR = Path(__file__).parent / "fixtures"
UISP_FIXTURES_DIR = FIXTURES_DIR / "uisp"
NETBOX_FIXTURES_DIR = FIXTURES_DIR / "netbox"
UNIFI_FIXTURES_DIR = FIXTURES_DIR / "unifi"
SIKLU_FIXTURES_DIR = FIXTURES_DIR / "siklu"
VICTORIA_FIXTURES_DIR = FIXTURES_DIR / "victoria"


def load_fixture(name: str) -> dict[str, Any]:
    """Load a recorded device fixture (``border-gw-01`` etc.)."""

    data = json.loads((FIXTURES_DIR / f"{name}.json").read_text())
    return {k: v for k, v in data.items() if not k.startswith("_")}


def load_uisp_devices(name: str = "devices") -> list[dict[str, Any]]:
    """Load a recorded UISP ``GET /devices`` fixture (token-scrubbed)."""

    return list(json.loads((UISP_FIXTURES_DIR / f"{name}.json").read_text()))


def uisp_settings(**overrides: Any) -> UispSettings:
    """Build :class:`UispSettings` for tests; override any field via kwargs."""

    settings: dict[str, Any] = {
        "base_url": "https://uisp.example.com",
        "token": Secret("UISP-TOKEN-supersecret"),
    }
    settings.update(overrides)
    return UispSettings(**settings)


def load_uisp_logs(name: str = "logs") -> dict[str, Any]:
    """Load a recorded UISP ``GET /logs`` envelope fixture (feature 019; token-free by nature)."""

    return dict(json.loads((UISP_FIXTURES_DIR / f"{name}.json").read_text()))


def load_uisp_outages(name: str = "outages") -> dict[str, Any]:
    """Load a recorded UISP ``GET /outages`` envelope fixture (feature 019)."""

    return dict(json.loads((UISP_FIXTURES_DIR / f"{name}.json").read_text()))


def uisp_log_transport(
    *,
    logs: dict[str, Any] | None = None,
    outages: dict[str, Any] | None = None,
    logs_error: Exception | None = None,
    outages_error: Exception | None = None,
) -> Any:
    """A fake ``UispController`` log transport serving recorded ``/logs`` and ``/outages`` pages.

    Routes by path; raise the matching ``*_error`` to model a per-source controller failure (used
    to prove one source fails while the other proceeds). Single-page fixtures
    (``pagination.pages == 1``) so the controller's page-walk stops after one request.
    """

    logs_env = load_uisp_logs() if logs is None else logs
    outages_env = load_uisp_outages() if outages is None else outages

    def _transport(path: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        if path.endswith("/outages"):
            if outages_error is not None:
                raise outages_error
            return outages_env
        if logs_error is not None:
            raise logs_error
        return logs_env

    return _transport


def load_unifi_official(name: str = "devices") -> list[dict[str, Any]]:
    """Load a recorded UniFi official-API ``/devices`` fixture (credential-scrubbed)."""

    return list(json.loads((UNIFI_FIXTURES_DIR / f"{name}.json").read_text()))


def load_unifi_controller(name: str = "stat_device") -> list[dict[str, Any]]:
    """Load a recorded UniFi controller-API ``stat/device`` fixture (credential-scrubbed)."""

    return list(json.loads((UNIFI_FIXTURES_DIR / f"{name}.json").read_text()))


def load_unifi_sitemanager(name: str = "sitemanager_devices") -> dict[str, Any]:
    """Load a recorded Site Manager ``/v1/devices`` envelope (synthetic, credential-free)."""

    return dict(json.loads((UNIFI_FIXTURES_DIR / f"{name}.json").read_text()))


def unifi_network_settings(**overrides: Any) -> UnifiNetworkSettings:
    """Build :class:`UnifiNetworkSettings` (both creds by default); override via kwargs."""

    settings: dict[str, Any] = {
        "base_url": "https://console.unifi-hosting.example.com",
        "api_key": Secret("UNIFI-KEY-supersecret"),
        "token": Secret("UNIFI-TOKEN-supersecret"),
    }
    settings.update(overrides)
    return UnifiNetworkSettings(**settings)


def unifi_sm_settings(**overrides: Any) -> UnifiSiteManagerSettings:
    """Build :class:`UnifiSiteManagerSettings` for tests; override via kwargs."""

    settings: dict[str, Any] = {"api_key": Secret("UNIFI-SM-KEY-supersecret")}
    settings.update(overrides)
    return UnifiSiteManagerSettings(**settings)


class FakeSource:
    """An in-memory :class:`InventorySource` for offline tests.

    Applies the name filter exactly like the real adapter so engine/CLI filter tests are honest;
    a non-REACHABLE ``outcome`` models a controller failure (empty devices).
    """

    name = "uisp"

    def __init__(
        self,
        statuses: list[DeviceStatus],
        *,
        outcome: SourceOutcome = SourceOutcome.REACHABLE,
        detail: str | None = None,
    ) -> None:
        self._statuses = statuses
        self._outcome = outcome
        self._detail = detail
        self.calls: list[tuple[str | None, float]] = []

    def fetch(self, *, name_filter: str | None, timeout: float) -> SourceReport:
        self.calls.append((name_filter, timeout))
        if self._outcome is not SourceOutcome.REACHABLE:
            return SourceReport([], self._outcome, self._detail)
        matched = [s for s in self._statuses if name_matches(s.device.identity, name_filter)]
        return SourceReport(matched, SourceOutcome.REACHABLE)


def victoria_settings(**overrides: Any) -> VictoriaSettings:
    """Build :class:`VictoriaSettings` for tests; override any field via kwargs."""

    settings: dict[str, Any] = {"base_url": "http://vm.example.org:8428"}
    settings.update(overrides)
    return VictoriaSettings(**settings)


_EMPTY_QUERY_REPORT = TsQueryReport(SourceOutcome.REACHABLE, [])
_EMPTY_VALUES_REPORT = TsLabelValuesReport(SourceOutcome.REACHABLE, [])
_EMPTY_SERIES_REPORT = TsSeriesReport(SourceOutcome.REACHABLE, [])


class FakeTimeseriesSource:
    """An in-memory :class:`TimeseriesQuerySource` for offline tests (feature 108).

    Each method pops the next canned report from its list (the last one is reused once
    exhausted; an empty list yields an empty REACHABLE report) and appends
    ``(method, kwargs)`` to :attr:`calls` — handler tests assert request shape, ordering,
    and short-circuiting off that log.
    """

    name = "victoria"

    def __init__(
        self,
        *,
        query: list[TsQueryReport] | None = None,
        query_range: list[TsQueryReport] | None = None,
        label_values: list[TsLabelValuesReport] | None = None,
        series: list[TsSeriesReport] | None = None,
    ) -> None:
        self._query = list(query or [])
        self._query_range = list(query_range or [])
        self._label_values = list(label_values or [])
        self._series = list(series or [])
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @staticmethod
    def _next(reports: list[Any], empty: Any) -> Any:
        if not reports:
            return empty
        return reports.pop(0) if len(reports) > 1 else reports[0]

    def query(self, promql: str, *, timeout: float) -> TsQueryReport:
        self.calls.append(("query", {"promql": promql, "timeout": timeout}))
        return self._next(self._query, _EMPTY_QUERY_REPORT)

    def query_range(
        self, promql: str, *, start: Any, end: Any, step_s: int, timeout: float
    ) -> TsQueryReport:
        args = {"promql": promql, "start": start, "end": end, "step_s": step_s, "timeout": timeout}
        self.calls.append(("query_range", args))
        return self._next(self._query_range, _EMPTY_QUERY_REPORT)

    def label_values(self, label: str, *, match: str | None, timeout: float) -> TsLabelValuesReport:
        self.calls.append(("label_values", {"label": label, "match": match, "timeout": timeout}))
        return self._next(self._label_values, _EMPTY_VALUES_REPORT)

    def series(self, match: str, *, timeout: float) -> TsSeriesReport:
        self.calls.append(("series", {"match": match, "timeout": timeout}))
        return self._next(self._series, _EMPTY_SERIES_REPORT)


def load_netbox_pages(name: str = "devices") -> list[dict[str, Any]]:
    """Load the recorded NetBox ``GET /api/dcim/devices/`` page envelopes (token-scrubbed)."""

    return list(json.loads((NETBOX_FIXTURES_DIR / f"{name}.json").read_text()))


def load_netbox_devices(name: str = "devices") -> list[dict[str, Any]]:
    """Flatten the recorded NetBox fixture's pages into the list of raw device dicts."""

    devices: list[dict[str, Any]] = []
    for page in load_netbox_pages(name):
        devices.extend(page.get("results", []))
    return devices


def netbox_settings(**overrides: Any) -> NetboxSettings:
    """Build :class:`NetboxSettings` for tests; override any field via kwargs."""

    settings: dict[str, Any] = {
        "base_url": "https://netbox.example.com",
        "token": Secret("NETBOX-TOKEN-supersecret"),
    }
    settings.update(overrides)
    return NetboxSettings(**settings)


class FakeDocSource:
    """An in-memory :class:`DocumentationSource` for offline tests (feature 007).

    Applies the name filter exactly like the real adapter so engine/CLI filter tests are honest;
    a non-REACHABLE ``outcome`` models a source failure (empty devices).
    """

    name = "netbox"

    def __init__(
        self,
        devices: list[NetboxDevice],
        *,
        outcome: SourceOutcome = SourceOutcome.REACHABLE,
        detail: str | None = None,
    ) -> None:
        self._devices = devices
        self._outcome = outcome
        self._detail = detail
        self.calls: list[tuple[str | None, float]] = []

    def fetch(self, *, name_filter: str | None, timeout: float) -> DocumentationReport:
        self.calls.append((name_filter, timeout))
        if self._outcome is not SourceOutcome.REACHABLE:
            return DocumentationReport([], self._outcome, self._detail)
        matched = [d for d in self._devices if name_matches(d.name, name_filter)]
        return DocumentationReport(matched, SourceOutcome.REACHABLE)


def snmp_settings(**overrides: Any) -> SnmpSettings:
    """Build :class:`SnmpSettings` for tests (community-based v2c by default)."""

    settings: dict[str, Any] = {"community": Secret("SNMP-COMMUNITY-supersecret")}
    settings.update(overrides)
    return SnmpSettings(**settings)


@dataclass
class FakeSnmpAgent:
    """Scripted SNMP responses for one address, consumed by :class:`FakeSnmpTransport`.

    ``scalars`` maps a full instance OID (e.g. ``1.3.6.1.2.1.1.5.0``) to a value (``None`` models a
    noSuchObject). ``table`` maps a column's base OID to ``{row index → value}``. ``fail`` makes
    every read raise :class:`SnmpTransportError` with that outcome (a whole-device failure).
    """

    scalars: dict[str, int | float | str | None] = field(default_factory=dict)
    table: dict[str, dict[str, int | float | str | None]] = field(default_factory=dict)
    fail: SnmpOutcome | None = None


def _raw(oid: str, value: int | float | str | None) -> RawVar:
    kind = MetricKind.GAUGE if isinstance(value, (int, float)) else MetricKind.STRING
    return RawVar(oid=oid, kind=kind, value=value)


class FakeSnmpTransport:
    """An offline :class:`SnmpTransport` serving one agent's scripted responses."""

    def __init__(self, agent: FakeSnmpAgent) -> None:
        self._agent = agent

    def get(self, oids: Sequence[str]) -> dict[str, RawVar]:
        if self._agent.fail is not None:
            raise SnmpTransportError(self._agent.fail, "fake agent failure")
        return {
            oid: _raw(oid, self._agent.scalars[oid]) for oid in oids if oid in self._agent.scalars
        }

    def walk(self, base_oid: str) -> list[RawVar]:
        if self._agent.fail is not None:
            raise SnmpTransportError(self._agent.fail, "fake agent failure")
        rows = self._agent.table.get(base_oid, {})
        return [_raw(f"{base_oid}.{index}", value) for index, value in rows.items()]


def fake_snmp_factory(agents: dict[str, FakeSnmpAgent]) -> TransportFactory:
    """A transport factory serving a :class:`FakeSnmpAgent` per address; unknown → unreachable."""

    def factory(target: MetricTarget, auth: object, profile: MetricProfile) -> SnmpTransport:
        agent = agents.get(target.address)
        if agent is None:
            raise SnmpTransportError(SnmpOutcome.UNREACHABLE, "no agent at this address")
        return FakeSnmpTransport(agent)

    return factory


def snmp_source(agents: dict[str, FakeSnmpAgent], **settings: Any) -> SnmpMetricsSource:
    """An :class:`SnmpMetricsSource` wired to a fake transport for the given agents (offline)."""

    return SnmpMetricsSource(snmp_settings(**settings), transport_factory=fake_snmp_factory(agents))


def snmp_agent_from_fixture(name: str) -> FakeSnmpAgent:
    """A FakeSnmpAgent serving a recorded 096 health/optics walk fixture
    (``tests/fixtures/snmp/<name>.json``, captured read-only by ``record_fixtures.py
    --snmp-health``). The fixture's ``walks`` maps each base OID to ``{row-suffix: value}`` —
    exactly the fake transport's table shape."""

    path = Path(__file__).parent / "fixtures" / "snmp" / f"{name}.json"
    data = json.loads(path.read_text())
    return FakeSnmpAgent(table={base: dict(rows) for base, rows in data["walks"].items()})


def system_agent(name: str = "core-gw-1", *, uptime: int = 123456) -> FakeSnmpAgent:
    """A FakeSnmpAgent answering the ``system`` profile: MIB-2 scalars + Mikrotik temperature, plus
    HOST-RESOURCES tables for the hrProcessorLoad CPU aggregate and the hrStorage RAM row."""

    return FakeSnmpAgent(
        scalars={
            "1.3.6.1.2.1.1.1.0": "RouterOS CCR2004",  # sysDescr
            "1.3.6.1.2.1.1.5.0": name,  # sysName
            "1.3.6.1.2.1.1.3.0": uptime,  # sysUpTime (timeticks)
            "1.3.6.1.4.1.14988.1.1.3.11.0": 47,  # mtxrHlProcessorTemperature (cpuTemperature)
            "1.3.6.1.4.1.14988.1.1.3.10.0": 41,  # mtxrHlTemperature (boardTemperature)
        },
        table={
            "1.3.6.1.2.1.25.3.3.1.2": {"1": 30, "2": 50},  # hrProcessorLoad (avg → 40)
            "1.3.6.1.2.1.25.2.3.1.2": {"65536": "1.3.6.1.2.1.25.2.1.2"},  # hrStorageType = RAM
            "1.3.6.1.2.1.25.2.3.1.4": {"65536": 1024},  # hrStorageAllocationUnits
            "1.3.6.1.2.1.25.2.3.1.5": {"65536": 262144},  # hrStorageSize (units) → 256 MB
            "1.3.6.1.2.1.25.2.3.1.6": {"65536": 131072},  # hrStorageUsed (units) → 128 MB
        },
    )


def interfaces_agent() -> FakeSnmpAgent:
    """A FakeSnmpAgent answering the ``interfaces`` profile's table columns (two interfaces)."""

    return FakeSnmpAgent(
        table={
            "1.3.6.1.2.1.31.1.1.1.1": {"1": "ether1", "2": "ether2"},  # ifName
            "1.3.6.1.2.1.2.2.1.8": {"1": 1, "2": 2},  # ifOperStatus (up, down)
            "1.3.6.1.2.1.31.1.1.1.6": {"1": 1_000_000, "2": 2_000_000},  # ifHCInOctets
            "1.3.6.1.2.1.31.1.1.1.10": {"1": 500_000, "2": 700_000},  # ifHCOutOctets
            "1.3.6.1.2.1.2.2.1.14": {"1": 3, "2": 7},  # ifInErrors (feature 021)
            "1.3.6.1.2.1.2.2.1.20": {"1": 1, "2": 2},  # ifOutErrors
            "1.3.6.1.2.1.2.2.1.13": {"1": 4, "2": 8},  # ifInDiscards
            "1.3.6.1.2.1.2.2.1.19": {"1": 0, "2": 5},  # ifOutDiscards
        }
    )


class FakeMetricsSource:
    """An in-memory :class:`MetricsSource` for chat-handler tests (feature 012).

    Returns a canned :class:`MetricReport` per address (or a default REACHABLE report with one
    sample), and records every call so tests can assert the engine consulted it.
    """

    name = "snmp"

    def __init__(
        self,
        reports: dict[str, MetricReport] | None = None,
        *,
        default_outcome: SnmpOutcome = SnmpOutcome.REACHABLE,
    ) -> None:
        self._reports = reports or {}
        self._default_outcome = default_outcome
        self.calls: list[tuple[str, str, float]] = []

    def fetch_metrics(
        self, target: MetricTarget, *, profile: MetricProfile, timeout: float
    ) -> MetricReport:
        self.calls.append((target.address, profile.name, timeout))
        canned = self._reports.get(target.address)
        if canned is not None:
            return canned
        samples = (
            [
                MetricSample(
                    name="sysName",
                    oid="1.3.6.1.2.1.1.5.0",
                    kind=MetricKind.STRING,
                    value=target.identity or target.address,
                )
            ]
            if self._default_outcome is SnmpOutcome.REACHABLE
            else []
        )
        return MetricReport(
            target=target, outcome=self._default_outcome, samples=samples, profile=profile.name
        )


def logs_settings(**overrides: Any) -> LogsSettings:
    """Build :class:`LogsSettings` for tests; override any field via kwargs.

    Defaults point at non-existent paths because chat tests drive a :class:`FakeLogSource` (no
    real DB); store-level tests pass explicit ``mirror_root``/``db_path`` tmp paths.
    """

    settings: dict[str, Any] = {
        "mirror_root": Path("/nonexistent/obi-logs"),
        "db_path": Path("/nonexistent/obi-logs.db"),
    }
    settings.update(overrides)
    return LogsSettings(**settings)


class FakeLogSource:
    """An in-memory :class:`LogSource` for offline chat tests (feature 013).

    Returns its preset events (bounded by ``limit``) and records every query's filters; a
    non-REACHABLE ``outcome`` models an unavailable store (empty events). It does not re-implement
    the SQL filtering — that is covered by the logstore tests; here it verifies the handler's
    rendering, bounding, and auditing.
    """

    name = "logs"

    def __init__(
        self,
        events: list[LogEvent],
        *,
        outcome: SourceOutcome = SourceOutcome.REACHABLE,
        detail: str | None = None,
        truncated: bool = False,
    ) -> None:
        self._events = events
        self._outcome = outcome
        self._detail = detail
        self._truncated = truncated
        self.calls: list[dict[str, Any]] = []

    def query(
        self,
        *,
        host: str | None,
        since: Any,
        until: Any,
        severity: str | None,
        contains: str | None,
        limit: int,
        timeout: float,
        source: str | None = None,
    ) -> LogReport:
        self.calls.append(
            {
                "host": host,
                "since": since,
                "until": until,
                "severity": severity,
                "contains": contains,
                "source": source,
                "limit": limit,
                "timeout": timeout,
            }
        )
        if self._outcome is not SourceOutcome.REACHABLE:
            return LogReport(
                [], self._outcome, self._detail, query_host=host, query_severity=severity
            )
        events = self._events[:limit]
        return LogReport(
            events,
            SourceOutcome.REACHABLE,
            truncated=self._truncated or len(self._events) > limit,
            query_host=host,
            query_severity=severity,
        )


@dataclass
class FakeBehavior:
    """How the fake driver should respond for one address."""

    outcome: ProbeOutcome = ProbeOutcome.IDENTIFIED
    device: Device | None = None
    status: DeviceStatus | None = None
    neighbors: list[NeighborLink] = field(default_factory=list)  # advertised by this address
    ospf_neighbors: list[OspfNeighbor] = field(default_factory=list)  # OSPF adjacencies (004)
    delay: float = 0.0  # seconds of simulated work in fetch_status
    probe_delay: float = 0.0  # seconds of simulated work in probe
    hang: bool = False  # if True, fetch_status ignores the timeout (simulate a wedged device)


class FakeDriver:
    """An in-memory :class:`VendorDriver` implementation for offline tests."""

    name = "fake"

    def __init__(self, behaviors: dict[str, FakeBehavior]) -> None:
        self.behaviors = behaviors
        self.probe_calls: list[tuple[str, float]] = []
        self.fetch_calls: list[tuple[str, float]] = []
        self.neighbor_calls: list[tuple[str, float]] = []
        self.ospf_calls: list[tuple[str, float]] = []  # records every fetch_ospf_neighbors call

    def probe(self, address: str, *, timeout: float) -> ProbeResult:
        self.probe_calls.append((address, timeout))
        behavior = self.behaviors.get(address)
        if behavior is None:
            return ProbeResult(address, ProbeOutcome.UNREACHABLE, detail="not configured")
        if behavior.probe_delay:
            time.sleep(min(behavior.probe_delay, timeout))
        if behavior.outcome is ProbeOutcome.IDENTIFIED and behavior.device is None:
            behavior = replace(behavior, device=Device(identity=address, address=address))
        return ProbeResult(address, behavior.outcome, device=behavior.device)

    def fetch_status(self, device: Device, *, timeout: float) -> DeviceStatus:
        self.fetch_calls.append((device.address, timeout))
        behavior = self.behaviors.get(device.address)
        if behavior is not None and behavior.hang:
            time.sleep(behavior.delay)  # deliberately ignore the timeout (tests the engine net)
        elif behavior is not None and behavior.delay:
            time.sleep(min(behavior.delay, timeout))
            if behavior.delay > timeout:
                return DeviceStatus(device, Reachability.TIMED_OUT, notes=["timed out"])
        if behavior is not None and behavior.status is not None:
            return replace(behavior.status, device=device)
        return DeviceStatus(
            device,
            Reachability.REACHABLE,
            uptime_seconds=12345,
            cpu_load_percent=3.0,
            free_memory_bytes=200_000_000,
            total_memory_bytes=268_435_456,
        )

    def fetch_neighbors(self, device: Device, *, timeout: float) -> list[NeighborLink]:
        self.neighbor_calls.append((device.address, timeout))
        behavior = self.behaviors.get(device.address)
        return list(behavior.neighbors) if behavior is not None else []

    def fetch_ospf_neighbors(self, device: Device, *, timeout: float) -> list[OspfNeighbor]:
        # Implements the OspfCapable capability (004). Records the call so flag-off tests can
        # assert it is NEVER invoked when resolution is disabled.
        self.ospf_calls.append((device.address, timeout))
        behavior = self.behaviors.get(device.address)
        return list(behavior.ospf_neighbors) if behavior is not None else []


class RecordingConnection:
    """A stand-in for a librouteros ``Api`` that serves recorded records and logs reads."""

    def __init__(self, records: dict[tuple[str, ...], list[dict[str, Any]]]) -> None:
        self._records = records
        self.reads: list[tuple[str, ...]] = []
        self.closed = False

    def path(self, *path: str) -> Iterable[dict[str, Any]]:
        self.reads.append(path)
        return list(self._records.get(path, []))

    def close(self) -> None:
        self.closed = True


def settings(use_tls: bool = True) -> RouterosSettings:
    return RouterosSettings(username="admin", password=Secret("fakesecret44028f"), use_tls=use_tls)


def mikrotik_driver_from_fixture(
    name: str,
) -> tuple[MikrotikDriver, list[RecordingConnection]]:
    """Build a Mikrotik driver whose connection serves a recorded fixture (read-only)."""

    fixture = load_fixture(name)
    records: dict[tuple[str, ...], list[dict[str, Any]]] = {
        ("system", "identity"): [fixture["identity"]] if fixture.get("identity") else [],
        ("system", "resource"): [fixture["resource"]] if fixture.get("resource") else [],
        ("system", "routerboard"): ([fixture["routerboard"]] if fixture.get("routerboard") else []),
        ("ip", "neighbor"): list(fixture.get("neighbors", [])),
        ("routing", "ospf", "neighbor"): list(fixture.get("ospf_neighbors", [])),
    }
    connections: list[RecordingConnection] = []
    base_serial = (
        str((fixture.get("routerboard") or {}).get("serial-number") or "SN").strip() or "SN"
    )

    def connect(address: str, timeout: float) -> RecordingConnection:
        # One recorded fixture is replayed for every probed address; in reality distinct addresses
        # are distinct boxes with distinct serials, so give each address a unique serial. Otherwise
        # serial-based collapse (feature 018) would (wrongly) fold every probed address into one.
        addr_records = dict(records)
        routerboard = records.get(("system", "routerboard")) or []
        if routerboard:
            addr_rb = dict(routerboard[0])
            addr_rb["serial-number"] = f"{base_serial}-{address}"
            addr_records[("system", "routerboard")] = [addr_rb]
        conn = RecordingConnection(addr_records)
        connections.append(conn)
        return conn

    return MikrotikDriver(settings(), connect=connect), connections


# --- Siklu (feature 020) test doubles ---------------------------------------------------------


def load_siklu_fixture(name: str) -> dict[str, str]:
    """Load a recorded Siklu fixture bundle: ``{show command -> raw output text}``.

    The bundle is the real read-only ``show`` capture from the named radio (sanitized, no password);
    :class:`RecordingSshSession` replays it so the parsers run against true device output offline.
    """

    data = json.loads((SIKLU_FIXTURES_DIR / f"{name}.json").read_text())
    return {str(k): str(v) for k, v in data["commands"].items()}


class RecordingSshSession:
    """An offline :class:`SikluSession` that serves recorded ``show`` output and records commands.

    Mirrors :class:`RecordingConnection`: ``run(cmd)`` returns the recorded text for ``cmd`` (or
    ``""`` for an unrecorded command) and appends to ``commands_sent``; ``close()`` flips
    ``closed``. Contract tests assert the driver issues ONLY the allowlist and closes the session.
    """

    def __init__(self, outputs: dict[str, str]) -> None:
        self._outputs = outputs
        self.commands_sent: list[str] = []
        self.closed = False

    def run(self, command: str) -> str:
        self.commands_sent.append(command)
        return self._outputs.get(command, "")

    def close(self) -> None:
        self.closed = True


def siklu_settings(**overrides: Any) -> SikluSettings:
    """Build :class:`SikluSettings` for tests (one host + creds by default); override via kwargs."""

    config: dict[str, Any] = {
        "hosts": ["172.18.89.130"],
        "username": "admin",
        "password": Secret("fakesecret44028f"),
    }
    config.update(overrides)
    return SikluSettings(**config)


def siklu_driver_from_fixture(
    *names: str, **fixtures: dict[str, str]
) -> tuple[SikluDriver, list[RecordingSshSession]]:
    """Build a :class:`SikluDriver` whose session replays recorded fixtures (read-only).

    ``names`` are fixture bundle names whose host is taken from each bundle's ``show ip`` (so the
    roster host maps to the right recording). For ad-hoc cases pass ``host=`` → ``{cmd: text}`` via
    ``**fixtures``. Returns the driver and the list of sessions opened (so contract tests can assert
    ``commands_sent`` and ``closed``).
    """

    bundles: dict[str, dict[str, str]] = {}
    for name in names:
        outputs = load_siklu_fixture(name)
        host = _siklu_fixture_host(outputs)
        bundles[host] = outputs
    bundles.update(fixtures)

    sessions: list[RecordingSshSession] = []

    def session_factory(host: str, timeout: float) -> SikluSession:
        outputs = bundles.get(host, {})
        session = RecordingSshSession(outputs)
        sessions.append(session)
        return session

    return SikluDriver(siklu_settings(), session=session_factory), sessions


def _siklu_fixture_host(outputs: dict[str, str]) -> str:
    """The management address from a fixture's ``show ip`` (``ip N ip-addr : dhcp <addr>``)."""

    for line in outputs.get("show ip", "").splitlines():
        if "ip-addr" in line and ":" in line:
            value = line.split(":", 1)[1].strip()
            return value.split()[-1] if value else value
    return "172.25.110.0"


def failing_siklu_driver(
    exc: Exception, *, host: str = "172.25.110.9"
) -> tuple[SikluDriver, list[str]]:
    """A driver whose session factory always raises ``exc`` (offline auth/timeout/refused tests).

    Returns the driver and a list that records the hosts the factory was asked to connect to.
    """

    attempted: list[str] = []

    def session_factory(h: str, timeout: float) -> SikluSession:
        attempted.append(h)
        raise exc

    return SikluDriver(siklu_settings(hosts=[host]), session=session_factory), attempted


# --- chat (feature 005) test doubles ----------------------------------------------------------


def text_response(text: str, *, usage: Usage | None = None) -> LLMResponse:
    """A final-answer LLM response (no tool use); ``usage`` scripts token figures (089)."""

    return LLMResponse(
        stop_reason="end_turn", content=[TextBlock(text=text)], usage=usage or Usage()
    )


def tool_response(
    name: str,
    tool_input: dict[str, Any],
    *,
    use_id: str = "tool_1",
    usage: Usage | None = None,
) -> LLMResponse:
    """An LLM response that asks to run one tool; ``usage`` scripts token figures (089)."""

    return LLMResponse(
        stop_reason="tool_use",
        content=[ToolUseBlock(id=use_id, name=name, input=tool_input)],
        usage=usage or Usage(),
    )


@dataclass
class StubLLMClient:
    """An offline ``LLMClient`` that returns scripted responses and records every call."""

    responses: list[LLMResponse]
    calls: list[dict[str, Any]] = field(default_factory=list)
    index: int = 0

    def create_message(
        self,
        *,
        system: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        model: str,
        max_tokens: int,
    ) -> LLMResponse:
        self.calls.append(
            {
                "system": system,
                "tools": list(tools),
                "messages": [dict(m) for m in messages],
                "model": model,
                "max_tokens": max_tokens,
            }
        )
        response = self.responses[min(self.index, len(self.responses) - 1)]
        self.index += 1
        return response


def config_backup_settings(tmp_path: Path, **overrides: Any) -> ConfigBackupSettings:
    """Build :class:`ConfigBackupSettings` for tests; repo_path defaults under tmp_path (026)."""

    settings: dict[str, Any] = {
        "username": "obi-readonly",
        "password": Secret("super-secret-pw"),
        "repo_path": tmp_path / "config-repo",
    }
    settings.update(overrides)
    return ConfigBackupSettings(**settings)


def chat_config(
    tmp_path: Path,
    *,
    routeros: bool = True,
    uisp: UispSettings | None = None,
    netbox: NetboxSettings | None = None,
    unifi_network: UnifiNetworkSettings | None = None,
    snmp: SnmpSettings | None = None,
    logs: LogsSettings | None = None,
    siklu: SikluSettings | None = None,
    configs: ConfigBackupSettings | None = None,
    wled: WledSettings | None = None,
    mesh: MeshtasticSettings | None = None,
    weather: WeatherSettings | None = None,
    traffic: TrafficSettings | None = None,
    victoria: VictoriaSettings | None = None,
    site: SiteSettings | None = None,
    brc: BrcSettings | None = None,
) -> Config:
    """A Config wired to tmp paths, suitable for offline chat tests.

    By default Mikrotik-only (feature 005). Pass ``routeros=False`` and/or ``uisp=`` / ``netbox=`` /
    ``unifi_network=`` / ``logs=`` to model the source combinations chat supports
    (006/010/011/013) — e.g. a UniFi-only host via
    ``chat_config(routeros=False, unifi_network=unifi_network_settings())`` or a logs-only host via
    ``chat_config(tmp_path, routeros=False, logs=logs_settings())``. ``snmp=`` adds the read-only
    SNMP capability (feature 012).
    """

    return Config(
        scope=DiscoveryScope(seeds=["172.25.110.1"]) if routeros else None,
        routeros=settings() if routeros else None,
        uisp=uisp,
        netbox=netbox,
        unifi_network=unifi_network,
        snmp=snmp,
        logs=logs,
        siklu=siklu,
        configs=configs,
        wled=wled,
        meshtastic=mesh,
        weather=weather,
        traffic=traffic,
        victoria=victoria,
        site=site or SiteSettings(),
        brc=brc,
        timeout=1.0,
        audit_path=tmp_path / "audit.jsonl",
        inventory_path=tmp_path / "inventory.json",
    )


def save_inventory(path: Path, entries: list[InventoryEntry]) -> Inventory:
    """Persist an inventory of ``entries`` at ``path`` and return it."""

    from obi.core import inventory as inv

    inventory = Inventory(scope=DiscoveryScope(seeds=["172.25.110.1"]), entries=entries)
    inv.save(inventory, path)
    return inventory


def chat_runtime(
    config: Config,
    *,
    responses: list[LLMResponse],
    behaviors: dict[str, FakeBehavior] | None = None,
    uisp_source: FakeSource | None = None,
    netbox_source: FakeDocSource | None = None,
    unifi_network_source: FakeSource | None = None,
    metrics_source: FakeMetricsSource | None = None,
    log_source: FakeLogSource | None = None,
    siklu_driver: SikluDriver | None = None,
    with_driver: bool = True,
) -> ChatRuntime:
    """A ChatRuntime backed by a scripted stub client and offline source doubles.

    ``with_driver=False`` models a host without Mikrotik (no driver); ``uisp_source`` injects a
    fake UISP controller (006), ``netbox_source`` a fake NetBox documented inventory (010),
    ``unifi_network_source`` a fake UniFi console (011), ``metrics_source`` a fake SNMP source
    (012), and ``log_source`` a fake syslog store (013). A UniFi-only or logs-only host uses
    ``with_driver=False`` plus the matching source.
    """

    ctx = ToolContext(
        config=config,
        driver=FakeDriver(behaviors or {}) if with_driver else None,
        uisp_source=uisp_source,
        netbox_source=netbox_source,
        unifi_network_source=unifi_network_source,
        metrics_source=metrics_source,
        log_source=log_source,
        siklu_driver=siklu_driver,
        wled_source=WledSource(config.wled) if config.wled is not None else None,
        audit=AuditTrail(config.audit_path),
    )
    # Mirror build_runtime's context composition so a chat_config with a context_file injects it.
    context = load_context(config.chat)
    return ChatRuntime(
        client=StubLLMClient(responses),
        ctx=ctx,
        # Mirror build_runtime (105 + 103): the conversational set — the operator toolset
        # (registry + wled writes) minus the chat exclusions (audit_query is MCP-only).
        tools=build_chat_tools(),
        model=config.chat.model,
        max_tokens=config.chat.max_tokens,
        system=system_prompt(context.text),
        context_warning=context.warning,
        context_loaded=context.text is not None,
    )


# --- Slack interface doubles (feature 009) ----------------------------------------------------


@dataclass
class FakeSlackClient:
    """A :class:`~obi.slack.handler.SlackPoster` that records posts (and can simulate Slack down).

    ``posts`` is the ordered append-only log of ``{channel, text, thread_ts, ts}`` the handler
    tried to send (each post gets a synthetic ``ts``); ``deletes`` is the list of ``ts`` values the
    handler later asked to delete (the transient "thinking" placeholder). Set ``fail=True`` to
    model an unreachable Slack (every post/delete raises).
    """

    posts: list[dict[str, Any]] = field(default_factory=list)
    deletes: list[str] = field(default_factory=list)
    fail: bool = False

    def post_message(self, channel: str, text: str, thread_ts: str | None = None) -> str | None:
        if self.fail:
            raise RuntimeError("slack unreachable")
        ts = f"ts-{len(self.posts)}"
        self.posts.append({"channel": channel, "text": text, "thread_ts": thread_ts, "ts": ts})
        return ts

    def delete_message(self, channel: str, ts: str) -> None:
        if self.fail:
            raise RuntimeError("slack unreachable")
        self.deletes.append(ts)

    def texts(self) -> list[str]:
        """Just the message texts, in order (handy for assertions)."""

        return [post["text"] for post in self.posts]

    def live_texts(self) -> list[str]:
        """Texts of messages still visible — those not subsequently deleted."""

        return [post["text"] for post in self.posts if post["ts"] not in self.deletes]


def dm_event(
    text: str,
    *,
    user: str = "U_OP",
    channel: str = "D1",
    ts: str = "100.1",
    thread_ts: str | None = None,
) -> dict[str, Any]:
    """A synthetic Slack ``message`` event for a direct message (``channel_type='im'``)."""

    event: dict[str, Any] = {
        "type": "message",
        "channel_type": "im",
        "user": user,
        "channel": channel,
        "text": text,
        "ts": ts,
    }
    if thread_ts is not None:
        event["thread_ts"] = thread_ts
    return event


def mention_event(
    text: str,
    *,
    user: str = "U_OP",
    channel: str = "C1",
    ts: str = "200.1",
    thread_ts: str | None = None,
) -> dict[str, Any]:
    """A synthetic Slack ``app_mention`` event (an @-mention in a channel).

    ``thread_ts`` marks a mention posted inside an existing thread (feature 104 answers those in
    that thread, with the thread's history seeded as context).
    """

    event: dict[str, Any] = {
        "type": "app_mention",
        "user": user,
        "channel": channel,
        "text": text,
        "ts": ts,
    }
    if thread_ts is not None:
        event["thread_ts"] = thread_ts
    return event


@dataclass
class FakeMembership:
    """A :class:`~obi.slack.handler.ChannelMembership` serving per-channel rosters (feature 104).

    ``rosters`` maps channel ID → member user IDs; mutate it between events to model joins and
    leaves. ``calls`` records every channel asked about; ``fail=True`` models an unreadable
    roster (every call raises ⇒ the handler must fail closed).
    """

    rosters: dict[str, set[str]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    fail: bool = False
    # feature 130: fail only these channels (the page gate reads a DIFFERENT channel list
    # than chat auth, so tests need one readable and one unreadable roster at once).
    fail_channels: set[str] = field(default_factory=set)

    def members(self, channel: str) -> set[str]:
        self.calls.append(channel)
        if self.fail or channel in self.fail_channels:
            raise RuntimeError("membership unavailable")
        return set(self.rosters.get(channel, set()))


@dataclass
class FakeUserNames:
    """A :class:`~obi.slack.handler.UserNames` double (feature 130, page attribution).

    ``names`` maps user ID → display name; ``calls`` records lookups; ``fail=True`` models
    an unreadable profile (the handler must fall back to the raw id).
    """

    names: dict[str, str] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    fail: bool = False

    def display(self, user: str) -> str:
        self.calls.append(user)
        if self.fail:
            raise RuntimeError("users.info unavailable")
        return self.names.get(user, user)


@dataclass
class FakeConversations:
    """A :class:`~obi.slack.handler.ConversationTypes` with a fixed set of group DMs.

    ``mpims`` holds the channel IDs that are multi-person DMs; ``calls`` records every channel
    asked about; ``fail=True`` models an unreadable conversation type (every call raises ⇒ the
    handler must fail closed).
    """

    mpims: set[str] = field(default_factory=set)
    calls: list[str] = field(default_factory=list)
    fail: bool = False

    def is_mpim(self, channel: str) -> bool:
        self.calls.append(channel)
        if self.fail:
            raise RuntimeError("conversation type unavailable")
        return channel in self.mpims


@dataclass
class FakeThreadHistory:
    """A :class:`~obi.slack.handler.ThreadHistoryFetcher` serving canned messages (feature 045).

    ``messages`` is what every fetch returns; ``calls`` records ``(channel, thread_ts)`` per
    fetch; ``fail=True`` models an unreadable thread (every fetch raises).
    """

    messages: list[dict[str, Any]] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)
    fail: bool = False

    def fetch_thread(self, channel: str, thread_ts: str) -> list[dict[str, Any]]:
        self.calls.append((channel, thread_ts))
        if self.fail:
            raise RuntimeError("thread history unavailable")
        return list(self.messages)


def bot_message(text: str, *, ts: str = "50.0") -> dict[str, Any]:
    """A synthetic fetched Slack message authored by obi (bot) for thread-history tests."""

    return {"ts": ts, "bot_id": "B_OBI", "text": text}


def user_message(text: str, *, ts: str = "51.0", user: str = "U_OP") -> dict[str, Any]:
    """A synthetic fetched Slack message authored by a human for thread-history tests."""

    return {"ts": ts, "user": user, "text": text}


def slack_handler(
    runtime: ChatRuntime,
    *,
    allowlist: list[str],
    poster: FakeSlackClient | None = None,
    sessions: SessionStore | None = None,
    recorder: AuditRecorder | None = None,
    history: FakeThreadHistory | None = None,
    membership: FakeMembership | None = None,
    authorized_channels: list[str] | None = None,
    bot_user_id: str | None = "U_OBI",
    conversations: FakeConversations | None = None,
    paging: Any = None,
    user_names: FakeUserNames | None = None,
) -> tuple[SlackHandler, FakeSlackClient]:
    """Build a :class:`SlackHandler` wired to a :class:`FakeSlackClient`; returns both."""

    fake = poster or FakeSlackClient()
    handler = SlackHandler(
        runtime=runtime,
        allowlist=allowlist,
        poster=fake,
        sessions=sessions,
        recorder=recorder,
        history=history,
        membership=membership,
        authorized_channels=authorized_channels or [],
        bot_user_id=bot_user_id,
        conversations=conversations,
        paging=paging,
        user_names=user_names,
    )
    return handler, fake


# --- watcher (feature 014) test doubles -------------------------------------------------------


def seed_logs(
    mirror_root: Path,
    db_path: Path,
    rows: list[tuple[str, datetime, str, str]],
    *,
    collector: str = "core",
) -> None:
    """Seed the SQLite log store by writing a syslog mirror and ingesting it (UTC).

    Each row is ``(host, ts, topics, message)``; ``ts`` must be tz-aware. Its date selects the
    mirror path (``<collector>/YYYY/MM/DD/<host>.log``) and its time-of-day is written into the
    BSD-syslog line — the parser merges the two, so the stored ``ts_us`` matches ``ts`` in UTC.
    """

    from obi.core import logstore

    for host, ts, topics, message in rows:
        day_dir = mirror_root / collector / f"{ts.year:04d}" / f"{ts.month:02d}" / f"{ts.day:02d}"
        day_dir.mkdir(parents=True, exist_ok=True)
        stamp = f"{ts.strftime('%b')} {ts.day:2d} {ts.strftime('%H:%M:%S')}"
        line = f"{stamp} {host} {topics} {message}\n"
        with (day_dir / f"{host}.log").open("a", encoding="utf-8") as handle:
            handle.write(line)

    conn = logstore.connect(db_path)
    try:
        logstore.ingest_tree(conn, mirror_root, tz=UTC)
    finally:
        conn.close()


def prime_cursor(store: WatchStore, *, cursor_id: int = 0) -> None:
    """Pre-seed the watcher watermark (ingest id) so existing events count as new from here."""

    store.base.mkdir(parents=True, exist_ok=True)
    payload = {"cursor": {"id": cursor_id}, "last_run": None, "open_findings": {}}
    store.state_path.write_text(json.dumps(payload) + "\n")


def watch_config(
    tmp_path: Path,
    *,
    enabled: bool = True,
    investigation_max_steps: int = 4,
    routeros_only: bool = False,
    ignore_topics: list[str] | None = None,
    ignore_collectors: list[str] | None = None,
    routeros: bool = False,
    api_key: str | None = None,
    slack: bool = False,
    slack_min_severity: str = "warning",
    slack_user: str | None = None,
    lag_alarm_minutes: int = 0,
    lag_alarm_events: int = 0,
    heartbeat_hours: int = 0,
    log_tz: str = "UTC",
) -> Config:
    """A Config for offline watcher tests: a real tmp [logs] store + [watch] dir.

    ``routeros_only`` defaults to False so most watcher tests (which have no inventory) are not
    window-filtered; the filter tests opt in. ``slack=True`` attaches a [slack] section (dummy
    tokens + a one-entry allowlist) and turns delivery on, for Slack-delivery tests (feature 015).

    The feature-062 self-monitoring knobs (``lag_alarm_minutes``/``lag_alarm_events``/
    ``heartbeat_hours``) default to 0 (disabled) so existing tests are unaffected by a clock jump
    reading as stale ingest; the self-monitoring tests opt in — the same pattern as
    ``routeros_only``.
    """

    return Config(
        scope=DiscoveryScope(seeds=["172.25.110.1"]) if routeros else None,
        routeros=settings() if routeros else None,
        logs=LogsSettings(
            mirror_root=tmp_path / "mirror",
            db_path=tmp_path / "logs.db",
            log_tz=log_tz,
        ),
        watch=WatchSettings(
            enabled=enabled,
            investigation_max_steps=investigation_max_steps,
            watch_dir=tmp_path / "watch",
            routeros_only=routeros_only,
            ignore_topics=ignore_topics or [],
            ignore_collectors=ignore_collectors or [],
            slack_notify=slack,
            slack_min_severity=slack_min_severity,
            slack_user=slack_user,
            lag_alarm_minutes=lag_alarm_minutes,
            lag_alarm_events=lag_alarm_events,
            heartbeat_hours=heartbeat_hours,
        ),
        chat=ChatSettings(api_key=Secret(api_key) if api_key else None),
        slack=(
            SlackSettings(
                bot_token=Secret("xoxb-test"),
                app_token=Secret("xapp-test"),
                allowlist=["U_OP"],
            )
            if slack
            else None
        ),
        audit_path=tmp_path / "audit.jsonl",
        inventory_path=tmp_path / "inventory.json",
        timeout=1.0,
    )


def watch_inventory(path: Path, identities: list[str]) -> None:
    """Persist an IDENTIFIED-device inventory for watcher baseline tests."""

    from obi.core import inventory as inv

    entries = [
        InventoryEntry(address=f"172.25.110.{i + 1}", identity=ident)
        for i, ident in enumerate(identities)
    ]
    inv.save(Inventory(scope=DiscoveryScope(seeds=["172.25.110.1"]), entries=entries), path)


# --- Slack delivery (feature 015) test doubles ------------------------------------------------


@dataclass
class FakeNotifier:
    """A core ``Notifier`` for offline watcher tests: records each delivered batch.

    ``batches`` holds the finding-lists passed to ``deliver`` (one per cycle that delivered). By
    default it reports every finding delivered; set ``result`` to force an outcome, or
    ``raises=True`` to simulate a notifier that throws — the cycle must survive it (FR-007).
    """

    batches: list[list[WatchFinding]] = field(default_factory=list)
    result: DeliveryResult | None = None
    raises: bool = False
    # feature 062: heartbeat status DMs (deliver_status). ``status_messages`` records each text;
    # ``status_result`` forces an outcome for the failure-path tests.
    status_messages: list[str] = field(default_factory=list)
    status_result: DeliveryResult | None = None

    def deliver(self, findings: list[WatchFinding]) -> DeliveryResult:
        self.batches.append(list(findings))
        if self.raises:
            raise RuntimeError("notifier boom")
        if self.result is not None:
            return self.result
        return DeliveryResult(delivered=len(findings), failed=0)

    def deliver_status(self, text: str) -> DeliveryResult:
        self.status_messages.append(text)
        if self.raises:
            raise RuntimeError("notifier boom")
        if self.status_result is not None:
            return self.status_result
        return DeliveryResult(delivered=1, failed=0)


@dataclass
class FakeWebClient:
    """A minimal stand-in for ``slack_sdk``'s ``WebClient`` for :class:`SlackNotifier` tests.

    Records ``conversations_open`` + ``chat_postMessage`` calls and hands back ``ts`` values so
    threading works. ``open_fail`` / ``post_fail`` simulate a hard Slack error; ``fail_replies``
    fails only threaded replies (parent posts succeed) to model the partial-failure case. The
    notifier must turn any of these into a failed/degraded result and never raise.
    """

    opened: list[list[str]] = field(default_factory=list)
    posts: list[dict[str, Any]] = field(default_factory=list)
    open_fail: bool = False
    post_fail: bool = False
    fail_replies: bool = False
    _seq: int = 0

    def conversations_open(self, *, users: list[str]) -> dict[str, Any]:
        if self.open_fail:
            raise RuntimeError("channel_open_failed")
        self.opened.append(list(users))
        return {"channel": {"id": "D_OP"}}

    def chat_postMessage(
        self, *, channel: str, text: str, thread_ts: str | None = None
    ) -> dict[str, Any]:
        if self.post_fail or (thread_ts is not None and self.fail_replies):
            raise RuntimeError("post_failed")
        self._seq += 1
        self.posts.append({"channel": channel, "text": text, "thread_ts": thread_ts})
        return {"ts": f"{self._seq}.0"}

    def parents(self) -> list[dict[str, Any]]:
        """Top-level posts (no ``thread_ts``) — one per single alert / digest."""

        return [post for post in self.posts if post["thread_ts"] is None]

    def replies(self) -> list[dict[str, Any]]:
        """Threaded posts (the full reports)."""

        return [post for post in self.posts if post["thread_ts"] is not None]


# --- AR geometry fakes (feature 022) -----------------------------------------------------------


def load_uisp_geometry(name: str) -> list[dict[str, Any]]:
    """Load one recorded geometry fixture: ``devices`` / ``data-links`` / ``sites``."""

    return list(json.loads((UISP_FIXTURES_DIR / f"geometry-{name}.json").read_text()))


def load_uisp_links_fixture(name: str) -> list[dict[str, Any]]:
    """Load one recorded link-health fixture (feature 034): ``devices`` / ``data-links``.

    Unlike the geometry captures these are UNTRIMMED (all RF fields retained; secrets verified
    absent at capture time) — see ``tests/fixtures/uisp/links-README.md``.
    """

    return list(json.loads((UISP_FIXTURES_DIR / f"links-{name}.json").read_text()))


def geometry_snapshot() -> Any:
    """The full recorded controller snapshot, mapped through the real adapter mappers."""

    from obi.core.drivers.uisp import map_geo_device, map_geo_link, map_geo_site
    from obi.core.models import GeometrySnapshot

    return GeometrySnapshot(
        devices=[d for d in map(map_geo_device, load_uisp_geometry("devices")) if d],
        links=[link for link in map(map_geo_link, load_uisp_geometry("data-links")) if link],
        sites=[s for s in map(map_geo_site, load_uisp_geometry("sites")) if s],
    )


def flat_ground(elevation: float = 1190.66) -> Any:
    """A deterministic offline ground lookup (every site at the playa elevation, ``cache``)."""

    from obi.core.models import GroundProvenance

    def ground(site_name: str, lat: float, lon: float) -> tuple[float, Any]:
        return elevation, GroundProvenance.CACHE

    return ground


def bm_ground() -> Any:
    """The deterministic BM terrain model for fixture tests: playa everywhere except the
    mountain relay (like the real EPQS bake would resolve them)."""

    from obi.core.models import GroundProvenance

    terrain = {"Basswood Relay - East": 1370.0, "Basswood Relay - West": 1370.0}

    def ground(site_name: str, lat: float, lon: float) -> tuple[float, Any]:
        return terrain.get(site_name, 1190.66), GroundProvenance.CACHE

    return ground


# --- voice interface doubles (feature 023) ----------------------------------------------------


def pcm_silence(seconds: float = 0.5) -> Any:
    """An all-zero capture — what a permission-denied mic delivers on macOS (research R2)."""

    import numpy as np

    return np.zeros(int(16000 * seconds), dtype=np.float32)


def pcm_noise(seconds: float = 0.5, amplitude: float = 0.001, seed: int = 23) -> Any:
    """A quiet noise-floor capture (non-zero but below the speech RMS gate)."""

    import numpy as np

    rng = np.random.default_rng(seed)
    return (rng.standard_normal(int(16000 * seconds)) * amplitude).astype(np.float32)


def pcm_speech(seconds: float = 1.0, amplitude: float = 0.2) -> Any:
    """A speech-loud synthetic capture (a 220 Hz tone — no human recordings in fixtures)."""

    import numpy as np

    t = np.arange(int(16000 * seconds), dtype=np.float32) / 16000.0
    return (np.sin(2 * np.pi * 220.0 * t) * amplitude).astype(np.float32)


@dataclass
class FakeRecorder:
    """Offline :class:`obi.voice.audio.Recorder`: serves canned captures, counts invocations."""

    captures: list[Any]
    calls: int = 0

    def record(self) -> Any:
        capture = self.captures[min(self.calls, len(self.captures) - 1)]
        self.calls += 1
        return capture


@dataclass
class FakeTranscriber:
    """Offline :class:`obi.voice.stt.Transcriber`: scripted results, records what it was fed."""

    results: list[Any]  # list[TranscriptionResult]
    heard: list[Any] = field(default_factory=list)

    def transcribe(self, audio: Any) -> Any:
        self.heard.append(audio)
        return self.results[min(len(self.heard) - 1, len(self.results) - 1)]


@dataclass
class RecordingSpeaker:
    """Offline :class:`obi.voice.tts.Speaker`: records spoken text and stop() calls."""

    spoken: list[str] = field(default_factory=list)
    stops: int = 0
    speaking: bool = False

    def speak(self, text: str) -> None:
        self.speaking = True
        self.spoken.append(text)
        self.speaking = False

    def stop(self) -> None:
        self.stops += 1
        self.speaking = False


def scripted_input(lines: Sequence[str]) -> Any:
    """A ``input_fn`` double: pops scripted lines; item ``KeyboardInterrupt`` raises it.

    When the script is exhausted it returns ``"q"`` so a session always terminates.
    """

    remaining = list(lines)

    def _input(_prompt: str = "") -> str:
        if not remaining:
            return "q"
        item = remaining.pop(0)
        if isinstance(item, type) and issubclass(item, BaseException):
            raise item()
        if isinstance(item, BaseException):
            raise item
        return str(item)

    return _input


def voice_session(
    runtime: ChatRuntime,
    *,
    recorder: Any = None,
    transcriber: Any = None,
    speaker: Any = None,
    input_lines: Sequence[str] = (),
    settings: Any = None,
) -> Any:
    """A fully-offline VoiceSession over doubles; consoles capture into StringIO."""

    import io

    from rich.console import Console

    from obi.core.config import VoiceSettings
    from obi.voice.session import VoiceSession
    from obi.voice.stt import TranscriptionResult

    return VoiceSession(
        runtime=runtime,
        settings=settings or VoiceSettings(),
        recorder=recorder or FakeRecorder(captures=[pcm_speech()]),
        transcriber=transcriber
        or FakeTranscriber(results=[TranscriptionResult(text="status?", gate="ok")]),
        speaker=speaker or RecordingSpeaker(),
        console=Console(file=io.StringIO(), force_terminal=False, width=200),
        err=Console(file=io.StringIO(), force_terminal=False, width=200),
        input_fn=scripted_input(input_lines),
    )

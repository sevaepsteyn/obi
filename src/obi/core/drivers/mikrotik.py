"""Mikrotik (RouterOS) vendor adapter — strictly read-only (Constitution Principles I, IV).

Uses the ``librouteros`` pure-Python RouterOS API client. Discovery/status issue only ``print``
reads (iterating a ``librouteros`` path issues a ``print``). Feature 028 adds three more read-only
capabilities — :meth:`MikrotikDriver.read_path` (an arbitrary ``print`` of any menu, gated by the
static :data:`_COMMAND_VERBS` denylist so it can never issue a mutating word), and
:meth:`MikrotikDriver.ping` / :meth:`MikrotikDriver.traceroute` (the read-only ``/ping`` and
``/tool/traceroute`` diagnostic commands, count/time-bounded). Feature 075 adds two more of the
same shape — :meth:`MikrotikDriver.monitor_ethernet` / :meth:`MikrotikDriver.monitor_poe` (the
read-only ``/interface/ethernet/monitor`` and ``…/poe/monitor`` telemetry reads, issued with
``once`` so they self-terminate). No write/exec command is ever sent. This module is the ONLY
place permitted to import ``librouteros`` (Principle IV).

The connection step is injectable (``connect=`` argument) so the RouterOS field-mapping —
where the vendor quirks live — is exercised by offline fixture tests without a real device.
"""

from __future__ import annotations

import contextlib
import re
import socket
import ssl
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import librouteros
from librouteros.exceptions import (
    ConnectionClosed,
    FatalError,
    LibRouterosError,
    TrapError,
)
from librouteros.query import And, Key

from obi.core.audit import record_device_command
from obi.core.config import RouterosSettings
from obi.core.drivers.base import ProbeOutcome, ProbeResult
from obi.core.models import (
    Device,
    DeviceStatus,
    NeighborLink,
    OspfArea,
    OspfInstanceInfo,
    OspfInterfaceState,
    OspfInterfaceTemplate,
    OspfNeighbor,
    Reachability,
    RouterAddress,
    VlanParent,
)


class Connection(Protocol):
    """The slice of the RouterOS API the adapter relies on (read + close only).

    ``path()`` builds a print/query chain (``.select()``/``.where()``); calling the connection
    (``__call__``) issues a bare command sentence — used only for the ``/ping`` and
    ``/tool/traceroute`` read-only diagnostics (feature 028).
    """

    def path(self, *path: str) -> Any: ...

    def __call__(self, cmd: str, /, **kwargs: Any) -> Iterable[dict[str, Any]]: ...

    def close(self) -> None: ...


ConnectFn = Callable[[str, float], Connection]

_AUTH_MARKERS = ("invalid user name or password", "not logged in", "access denied")
_UPTIME_UNITS = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}
_UPTIME_RE = re.compile(r"(\d+)([wdhms])")

# --- feature 028: read-only gate for the general RouterOS read tool --------------------------
#
# `read_path` only ever issues the `print` command word, so it is read-only by construction (the
# path selects the MENU, never the verb). This denylist is the explicit, STATIC (Principle I) gate:
# any path whose terminal segment is a command verb is refused pre-flight, before any device
# contact. The set is a module constant — the model cannot widen it at runtime.
_COMMAND_VERBS = frozenset(
    {
        "add",
        "set",
        "remove",
        "enable",
        "disable",
        "unset",
        "move",
        "reset",
        "reboot",
        "shutdown",
        "run",
        "fetch",
        "upload",
        "download",
        "import",
        "export",
        "monitor",
        "scan",
        "sniff",
        "torch",
        "blink",
        "make-static",
        "comment",
        "edit",
        "reset-configuration",
        "reset-counters",
        "clear",
        "flush",
        "release",
        "renew",
        "activate",
        "deactivate",
        "start",
        "stop",
        "restart",
        "install",
        "uninstall",
    }
)

# RouterOS reports durations like "1ms200us", "512us", "1s300ms". Sum the pieces to milliseconds.
_DURATION_UNITS_MS = {"s": 1000.0, "ms": 1.0, "us": 0.001, "ns": 0.000001}
_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)(s|ms|us|ns)")

# --- feature 075: the dedicated read-only `monitor … once` telemetry seam ---------------------
#
# These two command words are COMPILE-TIME LITERALS (never composed from input), issued through the
# connection call seam with `once` so they self-terminate — exactly the ping/traceroute (028) shape.
# They are the ONLY new live capability of feature 075 and the ONLY place `monitor` is issued: the
# print rail's `_COMMAND_VERBS` denylist (which lists `monitor`) and `assert_read_path` are
# untouched, so the general read tool (`read_path` / chat's `routeros_query`) still refuses it
# (research D1, constitution 1.11.0). A monitor snapshot touches no state — it is a read-only
# telemetry read (link/rate/duplex/SFP DDM; live PoE-out status/voltage/current/power).
_ETHERNET_MONITOR_CMD = "/interface/ethernet/monitor"
_POE_MONITOR_CMD = "/interface/ethernet/poe/monitor"


class ReadOnlyPathError(ValueError):
    """Raised when a requested read path resolves to a state-changing command verb."""


def normalize_segments(path: str) -> tuple[str, ...]:
    """Split an operator/model path into menu segments, tolerant of the common forms.

    ``"/interface/print"``, ``"interface print"``, and ``"interface"`` all normalize to
    ``("interface",)`` — obi always issues ``print`` itself, so a trailing ``print`` the operator
    typed is dropped (it is not part of the menu).
    """

    raw = re.split(r"[\s/]+", path.strip())
    segments = [seg for seg in raw if seg]
    if segments and segments[-1].lower() == "print":
        segments = segments[:-1]
    return tuple(segments)


def is_read_path(segments: Sequence[str]) -> bool:
    """True iff ``segments`` is non-empty and its terminal segment is not a command verb."""

    if not segments:
        return False
    return segments[-1].lower() not in _COMMAND_VERBS


def assert_read_path(segments: Sequence[str]) -> None:
    """Raise :class:`ReadOnlyPathError` if ``segments`` is empty or ends in a command verb."""

    if not segments:
        raise ReadOnlyPathError("empty path")
    if segments[-1].lower() in _COMMAND_VERBS:
        raise ReadOnlyPathError(
            f"'{segments[-1]}' is a state-changing command; only reads (print) are allowed"
        )


def _parse_duration_ms(value: object) -> float | None:
    """Parse a RouterOS duration (``"1ms200us"``) to milliseconds; ``None`` if unparseable."""

    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    matches = _DURATION_RE.findall(text)
    if not matches:
        return None
    return sum(float(amount) * _DURATION_UNITS_MS[unit] for amount, unit in matches)


@dataclass
class LiveReadResult:
    """One ``read_path`` outcome. ``records`` are RAW (redaction happens in the tool layer)."""

    records: list[dict[str, Any]]
    truncated: bool = False
    error: str | None = None


@dataclass
class PingProbe:
    """One ICMP probe from a ``/ping`` run."""

    seq: int
    status: str
    time_ms: float | None = None
    size: int | None = None
    ttl: int | None = None


@dataclass
class PingResult:
    """Aggregate of a bounded ``/ping`` run. ``error`` set (with empty probes) on failure."""

    sent: int
    received: int
    loss_pct: float
    rtt_min_ms: float | None = None
    rtt_avg_ms: float | None = None
    rtt_max_ms: float | None = None
    probes: list[PingProbe] = field(default_factory=list)
    error: str | None = None


@dataclass
class TraceHop:
    """One hop of a ``/tool/traceroute`` run."""

    hop: int
    address: str | None
    loss_pct: float | None = None
    avg_ms: float | None = None


@dataclass
class TraceResult:
    """A bounded traceroute. ``complete`` is True when the destination was reached; ``capped`` is
    True when WE stopped it at the hop/time backstop (vs the stream ending on its own)."""

    hops: list[TraceHop]
    complete: bool = False
    capped: bool = False
    error: str | None = None


def parse_uptime(value: object) -> int | None:
    """Parse a RouterOS uptime string (e.g. ``1w2d3h4m5s``) into seconds."""

    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    matches = _UPTIME_RE.findall(text)
    if not matches:
        return None
    return sum(int(amount) * _UPTIME_UNITS[unit] for amount, unit in matches)


def _to_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    # RouterOS reports cpu-load as a percentage, sometimes with a trailing '%'.
    text = str(value).strip().rstrip("%")
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def map_device(
    address: str,
    identity: dict[str, Any],
    resource: dict[str, Any],
    routerboard: dict[str, Any],
) -> Device:
    """Build a :class:`Device` from recorded RouterOS read responses."""

    model = routerboard.get("model") or resource.get("board-name") or None
    raw_serial = routerboard.get("serial-number")
    # Trimmed exact-string identity (feature 018, FR-003); empty after trim ⇒ no serial.
    serial = str(raw_serial).strip() if raw_serial is not None else ""
    return Device(
        identity=str(identity.get("name", "")),
        address=address,
        model=str(model) if model is not None else None,
        version=str(resource["version"]) if resource.get("version") is not None else None,
        serial=serial or None,
    )


def map_status(
    device: Device,
    resource: dict[str, Any],
    routerboard: dict[str, Any],
) -> DeviceStatus:
    """Map RouterOS read responses to a :class:`DeviceStatus`; missing fields => None + note."""

    notes: list[str] = []
    uptime = parse_uptime(resource.get("uptime"))
    cpu = _to_float(resource.get("cpu-load"))
    free_mem = _to_int(resource.get("free-memory"))
    total_mem = _to_int(resource.get("total-memory"))

    for label, present in (
        ("uptime", uptime is not None),
        ("cpu-load", cpu is not None),
        ("free-memory", free_mem is not None),
        ("total-memory", total_mem is not None),
    ):
        if not present:
            notes.append(f"{label} unavailable")

    # Enrich identity-time fields if a fuller picture is available now.
    if device.model is None and (routerboard.get("model") or resource.get("board-name")):
        device.model = str(routerboard.get("model") or resource.get("board-name"))
    if device.version is None and resource.get("version") is not None:
        device.version = str(resource["version"])

    return DeviceStatus(
        device=device,
        reachability=Reachability.REACHABLE,
        uptime_seconds=uptime,
        cpu_load_percent=cpu,
        free_memory_bytes=free_mem,
        total_memory_bytes=total_mem,
        notes=notes,
    )


def map_neighbor(record: dict[str, Any]) -> NeighborLink:
    """Map one ``/ip/neighbor`` record to a vendor-neutral :class:`NeighborLink`.

    Prefers the IPv4 ``address``; falls back to ``address6``. A neighbor advertising no usable
    IP yields ``address=None`` (visible but not probeable).
    """

    address = record.get("address") or record.get("address6")
    return NeighborLink(
        address=str(address) if address else None,
        identity=str(record["identity"]) if record.get("identity") else None,
        platform=str(record["platform"]) if record.get("platform") else None,
        interface=str(record["interface"]) if record.get("interface") else None,
        mac=str(record["mac-address"]) if record.get("mac-address") else None,
    )


def map_ospf_neighbor(record: dict[str, Any]) -> OspfNeighbor:
    """Map one ``/routing/ospf/neighbor`` record to a vendor-neutral :class:`OspfNeighbor` (004).

    The two fields the link-local resolver needs are ``address`` (the neighbor's interface address
    on the link — the ``169.254.x`` peer address for a routed peer link) and ``router-id`` (the
    peer's OSPF router-id, i.e. its routable management address by convention). Field names
    verified against recorded RouterOS ``/routing/ospf/neighbor`` captures.

    Feature 052 additionally carries the enrichment fields the OSPF map needs — the local
    ``interface`` (structural shared-fate key + drill-down display), the adjacency ``state``,
    ``state-changes``, the ``adjacency`` uptime, and ``dr``/``bdr`` — all read-only, all
    tolerant of absence (the 004 callers ignore them).
    """

    address = record.get("address")
    router_id = record.get("router-id")
    return OspfNeighbor(
        address=str(address) if address else None,
        router_id=str(router_id) if router_id else None,
        interface=_opt_str(record.get("interface")),
        state=_opt_str(record.get("state")),
        state_changes=_to_int(record.get("state-changes")),
        adjacency=_opt_str(record.get("adjacency")),
        dr=_opt_str(record.get("dr")),
        bdr=_opt_str(record.get("bdr")),
    )


def _opt_str(value: object) -> str | None:
    """Trimmed string, or ``None`` for absent/empty — the total-mapper idiom (missing → None)."""

    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _ros_flag(record: dict[str, Any], key: str) -> bool:
    """A RouterOS flag field: present (value ``""``/``true``/``yes``/real ``True``) ⇒ True.

    RouterOS renders a SET flag as an empty string (``"passive": ""``) and an unset flag by
    omitting the key; ``"false"``/``"no"`` (or a real ``False``) also read as unset.
    """

    if key not in record:
        return False
    value = record[key]
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("false", "no")


def _ros_bool_opt(value: object) -> bool | None:
    """Optional RouterOS boolean: ``True``/``False`` or ``None`` when absent/unparseable."""

    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "yes"):
            return True
        if low in ("false", "no"):
            return False
    return None


def map_ospf_instance(record: dict[str, Any]) -> OspfInstanceInfo:
    """Map one ``/routing/ospf/instance`` record (enrichment). Total: missing keys → None.

    ``router-id`` is carried RAW (it may be a symbolic reference like ``id-1`` — never used to
    key a node; the LSDB originator is authoritative, decision D1)."""

    return OspfInstanceInfo(
        name=_opt_str(record.get("name")),
        version=_to_int(record.get("version")),
        router_id=_opt_str(record.get("router-id")),
        redistribute=_opt_str(record.get("redistribute")),
        originate_default=_opt_str(record.get("originate-default")),
        out_filter_chain=_opt_str(record.get("out-filter-chain")),
        in_filter_chain=_opt_str(record.get("in-filter-chain")),
    )


def map_ospf_area(record: dict[str, Any]) -> OspfArea:
    """Map one ``/routing/ospf/area`` record (enrichment). Total."""

    return OspfArea(
        name=_opt_str(record.get("name")),
        area_id=_opt_str(record.get("area-id")),
        instance=_opt_str(record.get("instance")),
        area_type=_opt_str(record.get("type")),
    )


def map_ospf_interface_template(record: dict[str, Any]) -> OspfInterfaceTemplate:
    """Map one ``/routing/ospf/interface-template`` record (enrichment). Total.

    ``interfaces`` is the raw selector (name, comma-list, or a ``*NN`` reference to a deleted
    interface — the dangling-template lint reads exactly this)."""

    return OspfInterfaceTemplate(
        interfaces=_opt_str(record.get("interfaces")),
        area=_opt_str(record.get("area")),
        cost=_to_int(record.get("cost")),
        network_type=_opt_str(record.get("type")),
        passive=_ros_flag(record, "passive"),
        disabled=_ros_flag(record, "disabled"),
        use_bfd=_ros_bool_opt(record.get("use-bfd")),
    )


def map_ospf_interface(record: dict[str, Any]) -> OspfInterfaceState:
    """Map one live ``/routing/ospf/interface`` record (enrichment). Total.

    ``interface`` names the near side of an adjacency (the shared-fate key); ``address`` is the
    local link address (``169.254.x`` for a routed /31, possibly rendered ``ip%iface``)."""

    return OspfInterfaceState(
        interface=_opt_str(record.get("interface")),
        address=_opt_str(record.get("address")),
        area=_opt_str(record.get("area")),
        state=_opt_str(record.get("state")),
        network_type=_opt_str(record.get("network-type")),
        cost=_to_int(record.get("cost")),
        passive=_ros_flag(record, "passive") or str(record.get("state") or "").lower() == "passive",
    )


def map_vlan_parent(record: dict[str, Any]) -> VlanParent:
    """Map one ``/interface/vlan`` record to its parent port — the shared-fate key (R5). Total."""

    return VlanParent(
        name=str(record.get("name") or ""),
        parent=_opt_str(record.get("interface")),
        vlan_id=_to_int(record.get("vlan-id")),
    )


def map_router_address(record: dict[str, Any]) -> RouterAddress:
    """Map one ``/ip/address`` record — a configured assignment, present even when the interface's
    OSPF template is disabled, which is what lets a one-sided ghost resolve its intended /31 peer
    (053). Total."""

    return RouterAddress(
        address=str(record.get("address") or ""),
        interface=str(record.get("interface") or ""),
        network=_opt_str(record.get("network")),
        dynamic=_ros_flag(record, "dynamic"),
        disabled=_ros_flag(record, "disabled"),
        comment=str(record.get("comment") or ""),
    )


def _ssl_wrapper(sock: socket.socket) -> ssl.SSLSocket:
    """Wrap a socket for RouterOS api-ssl.

    RouterOS commonly uses a self-signed certificate (and, on older releases, anonymous-DH
    ciphers), so we do not verify the certificate — confidentiality of the credential on the
    wire is the goal, not server authentication. The blast-radius control is the read-only
    RouterOS account, per research.md §3.
    """

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with contextlib.suppress(ssl.SSLError):  # cipher set is platform dependent
        ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
    return ctx.wrap_socket(sock)


def _is_auth_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _AUTH_MARKERS)


class _Lease:
    """Owns ≤1 live :class:`Connection` for the span of one logical operation (feature 062, B5).

    Reads borrow the connection via :meth:`acquire`; the lease — not each read — owns its lifetime,
    so a whole operation's reads run over ONE login instead of one login per read. Failure semantics
    preserve the driver's per-read behavior exactly (research D8):

    - lazy connect: the first :meth:`acquire` connects; a raise propagates into the caller's
      existing per-method ``except`` so a dead device classifies identically to today.
    - cached failure: once a connect fails, later :meth:`acquire` calls re-raise it WITHOUT
      re-contacting the device (a one-shot lease attempts ≤1 connect; a session ≤1, not N).
    - ≤1 replacement: a read that detects a dropped connection calls :meth:`note_dead`, which lets
      the NEXT acquire reconnect once; a second death caches the failure (worst case 2 logins/op).
    """

    def __init__(self, connect: ConnectFn, address: str, timeout: float) -> None:
        self._connect = connect
        self._address = address
        self._timeout = timeout
        self._conn: Connection | None = None
        self._failure: Exception | None = None
        self._replacements_left = 1

    def acquire(self) -> Connection:
        if self._conn is not None:
            return self._conn
        if self._failure is not None:
            raise self._failure
        try:
            self._conn = self._connect(self._address, self._timeout)
        except Exception as exc:  # noqa: BLE001 - cached and re-raised into the caller's handler
            self._failure = exc
            raise
        return self._conn

    def note_dead(self, exc: Exception) -> None:
        """A read found the connection dead: drop it; allow ≤1 replacement, then cache the fault."""

        if self._conn is not None:
            _safe_close(self._conn)
            self._conn = None
        if self._replacements_left > 0:
            self._replacements_left -= 1
        else:
            self._failure = exc

    def close(self) -> None:
        if self._conn is not None:
            _safe_close(self._conn)
            self._conn = None


def _note_if_dead(lease: _Lease, exc: Exception) -> None:
    """Invalidate the lease only on a genuine connection death (not a benign per-read trap), so a
    session reconnects once for a dropped link but keeps a live connection for a protocol error."""

    if isinstance(exc, (ConnectionClosed, FatalError)):
        lease.note_dead(exc)


class MikrotikDriver:
    """Read-only RouterOS adapter implementing the :class:`VendorDriver` protocol."""

    name = "mikrotik"

    def __init__(self, settings: RouterosSettings, *, connect: ConnectFn | None = None) -> None:
        self._settings = settings
        self._connect: ConnectFn = connect or self._default_connect

    def _default_connect(self, address: str, timeout: float) -> Connection:
        kwargs: dict[str, Any] = {
            "host": address,
            "username": self._settings.username,
            "password": self._settings.password.reveal(),
            "port": self._settings.port,
            "timeout": timeout,
        }
        if self._settings.use_tls:
            kwargs["ssl_wrapper"] = _ssl_wrapper
        return librouteros.connect(**kwargs)  # type: ignore[no-any-return]

    @staticmethod
    def _read_one(api: Connection, *path: str) -> dict[str, Any]:
        for record in api.path(*path):
            return dict(record)
        return {}

    # feature 042: every public operation records one `device_command` audit event through the
    # ambient capture scope (a no-op outside one) — the exact operation, duration, and the
    # mapped error when the total method resolved a failure into its result.
    @staticmethod
    def _record(
        target: str, operation: str, start: float, *, error: str | None, items: int | None = None
    ) -> None:
        record_device_command(
            target,
            "routeros-api",
            operation,
            response_items=items,
            duration_ms=(time.monotonic() - start) * 1000.0,
            outcome="error" if error else "ok",
            error=error,
        )

    # feature 062 (B5): a lease scopes a connection to one logical operation. The public one-shot
    # methods wrap a single-use lease (connect + close, byte-identical to the pre-062 behavior); a
    # :meth:`session` shares one lease across every read. The ``_*_recorded`` bodies hold the audit
    # + total-error handling and take a lease, so one-shot and session record identically (V).
    @contextlib.contextmanager
    def _single_lease(self, address: str, timeout: float) -> Iterator[_Lease]:
        lease = _Lease(self._connect, address, timeout)
        try:
            yield lease
        finally:
            lease.close()

    @contextlib.contextmanager
    def session(self, address: str, *, timeout: float) -> Iterator[MikrotikSession]:
        """Open a reusable READ session for ``address`` (feature 062, B5): every read inside runs
        over ONE login instead of one per read. Implements
        :class:`~obi.core.drivers.base.SessionCapable`. Single-thread-use — the concurrent status
        sweep keeps its own one-shot calls (already 1 login/device)."""

        lease = _Lease(self._connect, address, timeout)
        try:
            yield MikrotikSession(self, lease, address)
        finally:
            lease.close()

    def probe(self, address: str, *, timeout: float) -> ProbeResult:
        """READ-ONLY discovery probe. Classifies the address; never mutates it."""

        with self._single_lease(address, timeout) as lease:
            return self._probe_recorded(lease, address)

    def _probe_recorded(self, lease: _Lease, address: str) -> ProbeResult:
        start = time.monotonic()
        result = self._probe_impl(lease, address)
        self._record(
            address,
            "probe /system/identity+resource+routerboard",
            start,
            error=result.detail if result.outcome is not ProbeOutcome.IDENTIFIED else None,
        )
        return result

    def _probe_impl(self, lease: _Lease, address: str) -> ProbeResult:
        try:
            api = lease.acquire()
        except Exception as exc:  # noqa: BLE001 - classify every failure mode
            return ProbeResult(address, _classify_connect_error(exc), detail=str(exc))
        try:
            identity = self._read_one(api, "system", "identity")
            resource = self._read_one(api, "system", "resource")
            routerboard = self._read_one(api, "system", "routerboard")
            if not identity and not resource:
                return ProbeResult(address, ProbeOutcome.UNIDENTIFIED, detail="no RouterOS data")
            device = map_device(address, identity, resource, routerboard)
            return ProbeResult(address, ProbeOutcome.IDENTIFIED, device=device)
        except (TrapError, LibRouterosError) as exc:
            _note_if_dead(lease, exc)
            return ProbeResult(address, ProbeOutcome.UNIDENTIFIED, detail=str(exc))

    def fetch_status(self, device: Device, *, timeout: float) -> DeviceStatus:
        """READ-ONLY health snapshot. Always returns a DeviceStatus; never raises."""

        with self._single_lease(device.address, timeout) as lease:
            return self._fetch_status_recorded(lease, device)

    def _fetch_status_recorded(self, lease: _Lease, device: Device) -> DeviceStatus:
        start = time.monotonic()
        result = self._fetch_status_impl(lease, device)
        failed = next((n for n in result.notes if n.startswith("connection failed")), None)
        self._record(device.address, "read /system/resource+routerboard", start, error=failed)
        return result

    def _fetch_status_impl(self, lease: _Lease, device: Device) -> DeviceStatus:
        try:
            api = lease.acquire()
        except Exception as exc:  # noqa: BLE001 - total method (FR-005)
            reachability = _connect_error_to_reachability(exc)
            return DeviceStatus(device, reachability, notes=[f"connection failed: {exc}"])
        try:
            resource = self._read_one(api, "system", "resource")
            routerboard = self._read_one(api, "system", "routerboard")
            if not resource:
                return DeviceStatus(
                    device,
                    Reachability.REACHABLE,
                    notes=["no /system/resource data returned"],
                )
            return map_status(device, resource, routerboard)
        except (TrapError, LibRouterosError) as exc:
            _note_if_dead(lease, exc)
            return DeviceStatus(device, Reachability.REACHABLE, notes=[f"partial read: {exc}"])

    def fetch_neighbors(self, device: Device, *, timeout: float) -> list[NeighborLink]:
        """READ-ONLY. Read ``/ip/neighbor`` and map to NeighborLinks. Total: ``[]`` on error."""

        with self._single_lease(device.address, timeout) as lease:
            return self._fetch_neighbors_recorded(lease, device)

    def _fetch_neighbors_recorded(self, lease: _Lease, device: Device) -> list[NeighborLink]:
        start = time.monotonic()
        result = self._fetch_neighbors_impl(lease, device)
        self._record(device.address, "read /ip/neighbor", start, error=None, items=len(result))
        return result

    def _fetch_neighbors_impl(self, lease: _Lease, device: Device) -> list[NeighborLink]:
        try:
            api = lease.acquire()
        except Exception:  # noqa: BLE001 - total method: a dead device yields no neighbors
            return []
        try:
            return [map_neighbor(dict(record)) for record in api.path("ip", "neighbor")]
        except (TrapError, LibRouterosError) as exc:
            _note_if_dead(lease, exc)
            return []

    def fetch_ospf_neighbors(self, device: Device, *, timeout: float) -> list[OspfNeighbor]:
        """READ-ONLY (004). Read ``/routing/ospf/neighbor`` → OspfNeighbors. Total: ``[]`` on error.

        Implements the :class:`OspfCapable` capability. Issues only a ``print`` of
        ``/routing/ospf/neighbor`` (iterating the path); never a write/exec.
        """

        with self._single_lease(device.address, timeout) as lease:
            return self._fetch_ospf_neighbors_recorded(lease, device)

    def _fetch_ospf_neighbors_recorded(self, lease: _Lease, device: Device) -> list[OspfNeighbor]:
        start = time.monotonic()
        result = self._fetch_ospf_neighbors_impl(lease, device)
        self._record(
            device.address, "read /routing/ospf/neighbor", start, error=None, items=len(result)
        )
        return result

    def _fetch_ospf_neighbors_impl(self, lease: _Lease, device: Device) -> list[OspfNeighbor]:
        try:
            api = lease.acquire()
        except Exception:  # noqa: BLE001 - total method: no OSPF data yields no candidates
            return []
        try:
            return [
                map_ospf_neighbor(dict(record))
                for record in api.path("routing", "ospf", "neighbor")
            ]
        except (TrapError, LibRouterosError) as exc:
            _note_if_dead(lease, exc)
            return []

    # --- feature 052: OSPF topology enrichment reads (print-only, total) ----------------------

    def _read_mapped(
        self,
        lease: _Lease,
        address: str,
        path: tuple[str, ...],
        mapper: Callable[[dict[str, Any]], Any],
        operation: str,
    ) -> list[Any]:
        """READ-ONLY. ``print`` a menu and map every row over ``lease``; TOTAL (``[]`` on failure).

        Iterating a ``librouteros`` path issues a ``print`` — never a write/exec. One audit
        ``device_command`` per call (Principle V), same as the other fetch_* methods."""

        start = time.monotonic()
        try:
            api = lease.acquire()
        except Exception:  # noqa: BLE001 - total method: a dead device yields no rows
            self._record(address, operation, start, error="connection failed", items=0)
            return []
        try:
            rows = [mapper(dict(record)) for record in api.path(*path)]
            self._record(address, operation, start, error=None, items=len(rows))
            return rows
        except (TrapError, LibRouterosError) as exc:
            _note_if_dead(lease, exc)
            self._record(address, operation, start, error=_short(exc), items=0)
            return []

    def fetch_ospf_lsa(self, address: str, *, timeout: float) -> list[dict[str, Any]]:
        """READ-ONLY. Print the ``/routing/ospf/lsa`` LSDB — the RAW records (originator/id/type/
        body). Parsing the ``body`` text is core-pure (:func:`obi.core.ospf.parse_lsa_body`), so
        the adapter returns rows verbatim. TOTAL: ``[]`` on error."""

        with self._single_lease(address, timeout) as lease:
            return self._fetch_ospf_lsa_over(lease, address)

    def _fetch_ospf_lsa_over(self, lease: _Lease, address: str) -> list[dict[str, Any]]:
        start = time.monotonic()
        try:
            api = lease.acquire()
        except Exception:  # noqa: BLE001 - total method
            self._record(address, "read /routing/ospf/lsa", start, error="connection failed")
            return []
        try:
            rows = [dict(record) for record in api.path("routing", "ospf", "lsa")]
            self._record(address, "read /routing/ospf/lsa", start, error=None, items=len(rows))
            return rows
        except (TrapError, LibRouterosError) as exc:
            _note_if_dead(lease, exc)
            self._record(address, "read /routing/ospf/lsa", start, error=_short(exc))
            return []

    def fetch_ospf_instances(self, address: str, *, timeout: float) -> list[OspfInstanceInfo]:
        with self._single_lease(address, timeout) as lease:
            return self._fetch_ospf_instances_over(lease, address)

    def _fetch_ospf_instances_over(self, lease: _Lease, address: str) -> list[OspfInstanceInfo]:
        return self._read_mapped(
            lease, address, ("routing", "ospf", "instance"), map_ospf_instance,
            "read /routing/ospf/instance",
        )

    def fetch_ospf_areas(self, address: str, *, timeout: float) -> list[OspfArea]:
        with self._single_lease(address, timeout) as lease:
            return self._fetch_ospf_areas_over(lease, address)

    def _fetch_ospf_areas_over(self, lease: _Lease, address: str) -> list[OspfArea]:
        return self._read_mapped(
            lease, address, ("routing", "ospf", "area"), map_ospf_area, "read /routing/ospf/area",
        )

    def fetch_ospf_interface_templates(
        self, address: str, *, timeout: float
    ) -> list[OspfInterfaceTemplate]:
        with self._single_lease(address, timeout) as lease:
            return self._fetch_ospf_interface_templates_over(lease, address)

    def _fetch_ospf_interface_templates_over(
        self, lease: _Lease, address: str
    ) -> list[OspfInterfaceTemplate]:
        return self._read_mapped(
            lease, address, ("routing", "ospf", "interface-template"),
            map_ospf_interface_template, "read /routing/ospf/interface-template",
        )

    def fetch_ospf_interfaces(self, address: str, *, timeout: float) -> list[OspfInterfaceState]:
        with self._single_lease(address, timeout) as lease:
            return self._fetch_ospf_interfaces_over(lease, address)

    def _fetch_ospf_interfaces_over(
        self, lease: _Lease, address: str
    ) -> list[OspfInterfaceState]:
        return self._read_mapped(
            lease, address, ("routing", "ospf", "interface"), map_ospf_interface,
            "read /routing/ospf/interface",
        )

    def fetch_ospf_neighbors_full(self, address: str, *, timeout: float) -> list[OspfNeighbor]:
        """The widened neighbor read (state/interface/uptime), keyed by address like the fleet
        sweep expects; the 004 :meth:`fetch_ospf_neighbors` (Device-keyed) is unchanged."""

        with self._single_lease(address, timeout) as lease:
            return self._fetch_ospf_neighbors_full_over(lease, address)

    def _fetch_ospf_neighbors_full_over(self, lease: _Lease, address: str) -> list[OspfNeighbor]:
        return self._read_mapped(
            lease, address, ("routing", "ospf", "neighbor"), map_ospf_neighbor,
            "read /routing/ospf/neighbor",
        )

    def fetch_vlan_parents(self, address: str, *, timeout: float) -> list[VlanParent]:
        with self._single_lease(address, timeout) as lease:
            return self._fetch_vlan_parents_over(lease, address)

    def _fetch_vlan_parents_over(self, lease: _Lease, address: str) -> list[VlanParent]:
        return self._read_mapped(
            lease, address, ("interface", "vlan"), map_vlan_parent, "read /interface/vlan",
        )

    def fetch_router_addresses(self, address: str, *, timeout: float) -> list[RouterAddress]:
        """Configured ``/ip/address`` assignments (053) — the structural source that lets a
        one-sided ghost name its intended peer even when the far side's template is disabled."""

        with self._single_lease(address, timeout) as lease:
            return self._fetch_router_addresses_over(lease, address)

    def _fetch_router_addresses_over(self, lease: _Lease, address: str) -> list[RouterAddress]:
        return self._read_mapped(
            lease, address, ("ip", "address"), map_router_address, "read /ip/address",
        )

    # --- feature 028: general read-only live access (read + the two diagnostics) --------------

    def read_path(
        self,
        address: str,
        segments: Sequence[str],
        *,
        where: Sequence[tuple[str, str]] = (),
        fields: Sequence[str] = (),
        timeout: float,
        max_records: int,
    ) -> LiveReadResult:
        """READ-ONLY. ``print`` the ``segments`` menu with optional query/proplist. TOTAL.

        Only ever issues ``print`` (the path chooses the menu, never the verb); the terminal segment
        is verb-checked by :func:`assert_read_path` as defense-in-depth. Reads at most
        ``max_records + 1`` rows to flag truncation. Never raises: connection and protocol failures
        resolve to a :class:`LiveReadResult` with ``error`` set and no rows.
        """

        with self._single_lease(address, timeout) as lease:
            return self._read_path_recorded(
                lease, address, segments, where=where, fields=fields, max_records=max_records
            )

    def _read_path_recorded(
        self,
        lease: _Lease,
        address: str,
        segments: Sequence[str],
        *,
        where: Sequence[tuple[str, str]] = (),
        fields: Sequence[str] = (),
        max_records: int,
    ) -> LiveReadResult:
        assert_read_path(segments)  # the tool layer also checks pre-flight; this is the last guard
        start = time.monotonic()
        result = self._read_path_impl(
            lease, address, segments, where=where, fields=fields, max_records=max_records
        )
        self._record(
            address,
            f"print /{'/'.join(segments)}",
            start,
            error=result.error,
            items=len(result.records),
        )
        return result

    def _read_path_impl(
        self,
        lease: _Lease,
        address: str,
        segments: Sequence[str],
        *,
        where: Sequence[tuple[str, str]] = (),
        fields: Sequence[str] = (),
        max_records: int,
    ) -> LiveReadResult:
        try:
            api = lease.acquire()
        except Exception as exc:  # noqa: BLE001 - total method: classify every failure mode
            return LiveReadResult(records=[], error=_reachability_note(exc))
        try:
            query: Any = api.path(*segments).select(*[Key(f) for f in fields])
            conditions = [Key(k) == v for k, v in where]
            if len(conditions) == 1:
                query = query.where(conditions[0])
            elif len(conditions) > 1:
                query = query.where(And(*conditions))
            records: list[dict[str, Any]] = []
            truncated = False
            for record in query:
                if len(records) >= max_records:
                    truncated = True
                    break
                records.append(dict(record))
            return LiveReadResult(records=records, truncated=truncated)
        except (TrapError, LibRouterosError) as exc:
            _note_if_dead(lease, exc)
            return LiveReadResult(records=[], error=_short(exc))

    def ping(self, address: str, destination: str, *, count: int, timeout: float) -> PingResult:
        """READ-ONLY diagnostic. Bounded ``/ping`` FROM ``address`` to ``destination``. TOTAL.

        ``count`` bounds the run (RouterOS ``/ping count=N`` self-terminates). A destination that
        never replies is 100% loss, NOT an error.
        """

        start = time.monotonic()
        result = self._ping_impl(address, destination, count=count, timeout=timeout)
        self._record(address, f"/ping {destination} count={count}", start, error=result.error)
        return result

    def _ping_impl(
        self, address: str, destination: str, *, count: int, timeout: float
    ) -> PingResult:
        try:
            api = self._connect(address, timeout)
        except Exception as exc:  # noqa: BLE001 - total method
            return PingResult(0, 0, 0.0, error=_reachability_note(exc))
        try:
            probes = [
                _map_ping_probe(dict(sentence), i)
                for i, sentence in enumerate(api("/ping", address=destination, count=count))
            ]
            return _aggregate_ping(probes)
        except (TrapError, LibRouterosError) as exc:
            return PingResult(0, 0, 0.0, error=_short(exc))
        finally:
            _safe_close(api)

    def traceroute(
        self,
        address: str,
        destination: str,
        *,
        max_probes: int,
        deadline_s: float,
        timeout: float,
    ) -> TraceResult:
        """READ-ONLY diagnostic. Bounded ``/tool/traceroute`` FROM ``address``. TOTAL.

        RouterOS traceroute runs CONTINUOUSLY (re-probing until stopped) unless bounded, so we pass
        ``count=1`` for a single, self-terminating pass (verified live: one pass then ``!done``).
        ``max_probes`` bounds the number of DISTINCT hops and ``deadline_s`` the wall clock — both
        safety backstops; closing cancels. Hops are keyed by address (RouterOS re-emits a hop's row
        as its RTT/loss refine, so a duplicate updates, not appends); a mid-stream read timeout
        yields the partial path gathered so far (``complete=False``).
        """

        start = time.monotonic()
        result = self._traceroute_impl(
            address, destination, max_probes=max_probes, deadline_s=deadline_s, timeout=timeout
        )
        self._record(
            address,
            f"/tool/traceroute {destination} count=1",
            start,
            error=result.error,
            items=len(result.hops),
        )
        return result

    def _traceroute_impl(
        self,
        address: str,
        destination: str,
        *,
        max_probes: int,
        deadline_s: float,
        timeout: float,
    ) -> TraceResult:
        try:
            api = self._connect(address, timeout)
        except Exception as exc:  # noqa: BLE001 - total method
            return TraceResult(hops=[], error=_reachability_note(exc))
        order: list[str] = []
        latest: dict[str, dict[str, Any]] = {}
        complete = False
        capped = False
        try:
            started = time.monotonic()
            probes = api("/tool/traceroute", address=destination, count=1)
            for sentence in probes:
                record = dict(sentence)
                addr = str(record.get("address") or "").strip()
                key = addr or f"*{len(order)}"
                if key not in latest:
                    order.append(key)
                latest[key] = record
                if addr and addr == destination:
                    complete = True
                    break
                # Bound the PATH LENGTH (distinct hops), NOT the sentence count: RouterOS re-emits
                # each hop's row several times as it refines RTT/loss, so a sentence-count cap
                # tripped mid-path (a public traceroute stopped ~hop 12 in ~3 s, far below the hop
                # cap). ``deadline_s`` is the separate runaway/time backstop.
                if len(order) >= max_probes or (time.monotonic() - started) >= deadline_s:
                    capped = True
                    break
            else:
                # The stream ended on its own (RouterOS finished its bounded single pass, `!done`) —
                # we did NOT cut it. A single-pass traceroute stops once it REACHES the destination;
                # an unreachable target instead runs to max-TTL with an unresponsive (`*`) tail. So
                # a final RESPONDING hop means reached. This is how a HOSTNAME destination
                # completes: its hops are resolved IPs, never equal to the literal name we passed.
                if order and str(latest[order[-1]].get("address") or "").strip():
                    complete = True
        except TimeoutError:
            pass  # partial path; return what we gathered (complete=False)
        except (TrapError, LibRouterosError) as exc:
            return TraceResult(hops=[], error=_short(exc))
        finally:
            _safe_close(api)  # closing cancels the continuous command
        hops = [_map_trace_hop(latest[key], i) for i, key in enumerate(order)]
        return TraceResult(hops=hops, complete=complete, capped=capped)

    # --- feature 075: read-only `monitor … once` telemetry reads (link/SFP DDM, PoE-out) -------

    def monitor_ethernet(
        self, address: str, names: Sequence[str], *, timeout: float, max_records: int
    ) -> LiveReadResult:
        """READ-ONLY. One ``/interface/ethernet/monitor once`` snapshot for ``names``. TOTAL.

        ``names`` are the device's OWN ethernet interface names (harvested from its
        ``/interface/ethernet`` print, never user input), joined into ``=numbers=``; ``once`` makes
        the monitor self-terminate. Returns link status/rate/duplex/auto-negotiation and, for a
        populated cage, the SFP DDM fields. Never raises: connection/protocol failures resolve to a
        :class:`LiveReadResult` with ``error`` set and no rows.
        """

        with self._single_lease(address, timeout) as lease:
            return self._monitor_recorded(
                lease, address, _ETHERNET_MONITOR_CMD, names, max_records=max_records
            )

    def monitor_poe(
        self, address: str, names: Sequence[str], *, timeout: float, max_records: int
    ) -> LiveReadResult:
        """READ-ONLY. One ``/interface/ethernet/poe/monitor once`` snapshot for ``names``. TOTAL.

        ``names`` are the PoE-out ports the ``/interface/ethernet/poe`` print returned (empty ⇒ no
        call, an empty result). Returns live PoE-out status and, for a powered port,
        voltage/current/power. Same TOTAL contract as :meth:`monitor_ethernet`.
        """

        with self._single_lease(address, timeout) as lease:
            return self._monitor_recorded(
                lease, address, _POE_MONITOR_CMD, names, max_records=max_records
            )

    def _monitor_recorded(
        self,
        lease: _Lease,
        address: str,
        command: str,
        names: Sequence[str],
        *,
        max_records: int,
    ) -> LiveReadResult:
        start = time.monotonic()
        result = self._monitor_impl(lease, address, command, names, max_records=max_records)
        self._record(
            address,
            f"{command} once ({len(names)} ports)",
            start,
            error=result.error,
            items=len(result.records),
        )
        return result

    def _monitor_impl(
        self,
        lease: _Lease,
        address: str,
        command: str,
        names: Sequence[str],
        *,
        max_records: int,
    ) -> LiveReadResult:
        if not names:
            return LiveReadResult(records=[])  # nothing to monitor ⇒ no device call, no error
        try:
            api = lease.acquire()
        except Exception as exc:  # noqa: BLE001 - total method: classify every failure mode
            return LiveReadResult(records=[], error=_reachability_note(exc))
        try:
            # `once` self-terminates the monitor; `numbers` selects the device's own interfaces.
            sentences = api(command, **{"once": "", "numbers": ",".join(names)})
            records: list[dict[str, Any]] = []
            truncated = False
            for sentence in sentences:
                if len(records) >= max_records:
                    truncated = True
                    break
                records.append(dict(sentence))
            return LiveReadResult(records=records, truncated=truncated)
        except (TrapError, LibRouterosError) as exc:
            _note_if_dead(lease, exc)
            return LiveReadResult(records=[], error=_short(exc))


class MikrotikSession:
    """One reusable READ session bound to a single device (feature 062, B5).

    Every read runs over the shared :class:`_Lease`, so a whole operation (OSPF enrichment of one
    router, discovery of one address) uses ONE login instead of one per read. Mirrors the driver's
    read surface minus the per-call ``timeout`` (fixed at :meth:`MikrotikDriver.session`); the
    reads, per-read audit records, and total-failure values are byte-identical to the one-shot
    methods — session reuse changes no outcome. Implements
    :class:`~obi.core.drivers.base.RouterReadSession`. Single-thread-use."""

    def __init__(self, driver: MikrotikDriver, lease: _Lease, address: str) -> None:
        self._driver = driver
        self._lease = lease
        self._address = address

    def probe(self) -> ProbeResult:
        return self._driver._probe_recorded(self._lease, self._address)

    def fetch_neighbors(self, device: Device) -> list[NeighborLink]:
        return self._driver._fetch_neighbors_recorded(self._lease, device)

    def fetch_ospf_neighbors(self, device: Device) -> list[OspfNeighbor]:
        return self._driver._fetch_ospf_neighbors_recorded(self._lease, device)

    def read_path(
        self,
        segments: Sequence[str],
        *,
        where: Sequence[tuple[str, str]] = (),
        fields: Sequence[str] = (),
        max_records: int,
    ) -> LiveReadResult:
        return self._driver._read_path_recorded(
            self._lease, self._address, segments, where=where, fields=fields,
            max_records=max_records,
        )

    def monitor_ethernet(self, names: Sequence[str], *, max_records: int) -> LiveReadResult:
        return self._driver._monitor_recorded(
            self._lease, self._address, _ETHERNET_MONITOR_CMD, names, max_records=max_records
        )

    def monitor_poe(self, names: Sequence[str], *, max_records: int) -> LiveReadResult:
        return self._driver._monitor_recorded(
            self._lease, self._address, _POE_MONITOR_CMD, names, max_records=max_records
        )

    def fetch_ospf_lsa(self) -> list[dict[str, Any]]:
        return self._driver._fetch_ospf_lsa_over(self._lease, self._address)

    def fetch_ospf_instances(self) -> list[OspfInstanceInfo]:
        return self._driver._fetch_ospf_instances_over(self._lease, self._address)

    def fetch_ospf_areas(self) -> list[OspfArea]:
        return self._driver._fetch_ospf_areas_over(self._lease, self._address)

    def fetch_ospf_interface_templates(self) -> list[OspfInterfaceTemplate]:
        return self._driver._fetch_ospf_interface_templates_over(self._lease, self._address)

    def fetch_ospf_interfaces(self) -> list[OspfInterfaceState]:
        return self._driver._fetch_ospf_interfaces_over(self._lease, self._address)

    def fetch_ospf_neighbors_full(self) -> list[OspfNeighbor]:
        return self._driver._fetch_ospf_neighbors_full_over(self._lease, self._address)

    def fetch_vlan_parents(self) -> list[VlanParent]:
        return self._driver._fetch_vlan_parents_over(self._lease, self._address)

    def fetch_router_addresses(self) -> list[RouterAddress]:
        return self._driver._fetch_router_addresses_over(self._lease, self._address)


def _classify_connect_error(exc: Exception) -> ProbeOutcome:
    if _is_auth_error(exc):
        return ProbeOutcome.AUTH_FAILED
    if isinstance(exc, (TrapError, FatalError, ConnectionClosed)):
        return ProbeOutcome.AUTH_FAILED if _is_auth_error(exc) else ProbeOutcome.UNIDENTIFIED
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return ProbeOutcome.TIMED_OUT
    if isinstance(exc, ssl.SSLError):
        return ProbeOutcome.UNIDENTIFIED
    if isinstance(exc, ConnectionRefusedError):
        return ProbeOutcome.UNREACHABLE
    if isinstance(exc, OSError):
        return ProbeOutcome.UNREACHABLE
    return ProbeOutcome.UNIDENTIFIED


def _connect_error_to_reachability(exc: Exception) -> Reachability:
    if _is_auth_error(exc):
        return Reachability.AUTH_FAILED
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return Reachability.TIMED_OUT
    if isinstance(exc, (TrapError, FatalError, ConnectionClosed)):
        return Reachability.AUTH_FAILED if _is_auth_error(exc) else Reachability.UNREACHABLE
    return Reachability.UNREACHABLE


def _safe_close(api: Connection) -> None:
    with contextlib.suppress(Exception):  # closing must never surface an error
        api.close()


def _short(exc: object) -> str:
    """A one-line, secret-free note from an exception (credentials never appear in messages)."""

    text = " ".join(str(exc).split())
    return text[:200] if text else exc.__class__.__name__


def _reachability_note(exc: Exception) -> str:
    """Classify a connection failure and render it as a short, secret-free note."""

    return f"{_connect_error_to_reachability(exc).value}: {_short(exc)}"


def _map_ping_probe(record: dict[str, Any], index: int) -> PingProbe:
    """Map one ``/ping`` ``!re`` sentence to a :class:`PingProbe`."""

    time_ms = _parse_duration_ms(record.get("time"))
    status = str(record.get("status") or "").strip()
    if not status:
        status = "ok" if time_ms is not None else "timeout"
    seq = _to_int(record.get("seq"))
    return PingProbe(
        seq=seq if seq is not None else index + 1,
        status=status,
        time_ms=time_ms,
        size=_to_int(record.get("size")),
        ttl=_to_int(record.get("ttl")),
    )


def _aggregate_ping(probes: list[PingProbe]) -> PingResult:
    """Summarize ping probes: sent/received/loss and min/avg/max RTT over the replied probes."""

    sent = len(probes)
    rtts = [p.time_ms for p in probes if p.time_ms is not None]
    received = len(rtts)
    loss = 0.0 if sent == 0 else round((sent - received) / sent * 100, 1)
    return PingResult(
        sent=sent,
        received=received,
        loss_pct=loss,
        rtt_min_ms=min(rtts) if rtts else None,
        rtt_avg_ms=round(sum(rtts) / len(rtts), 3) if rtts else None,
        rtt_max_ms=max(rtts) if rtts else None,
        probes=probes,
    )


def _map_trace_hop(record: dict[str, Any], index: int) -> TraceHop:
    """Map one ``/tool/traceroute`` hop sentence to a :class:`TraceHop` (best-effort fields)."""

    addr = str(record.get("address") or "").strip() or None
    avg = _parse_duration_ms(record.get("avg"))
    if avg is None:
        avg = _to_float(record.get("avg"))
    return TraceHop(
        hop=index + 1,
        address=addr,
        loss_pct=_to_float(record.get("loss")),
        avg_ms=avg,
    )

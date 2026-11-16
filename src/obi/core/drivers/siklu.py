"""Siklu EtherHaul vendor adapter — strictly read-only (Constitution Principles I, IV).

Siklu is a device-per-address vendor (no controller), reached over SSH. The EtherHaul CLI honors
NO exec channel — a one-shot ``ssh host "show ..."`` drops into the interactive CLI and ignores the
command — so the adapter opens a paramiko **interactive shell**, detects the ``<system-name>>``
prompt, and runs a **static allowlist** of read-only ``show`` commands (research §1). This module is
the ONLY place permitted to import ``paramiko`` (Principle IV), mirroring how ``librouteros`` is
quarantined in ``mikrotik.py``.

The session is injectable (``session=`` factory, mirroring Mikrotik's ``connect=``) so the
key-value parsers — where the vendor quirks live — are exercised by offline fixture tests captured
read-only from the real radios, without a real device.

**Read-only by construction (Principle I)**: ``run()`` is only ever called with members of
:data:`SHOW_COMMANDS` (module constants); no operator/model/config text is interpolated into a
command, so there is no code path that can emit a mutating command. ``host`` selects the connection
target, never the command text.
"""

from __future__ import annotations

import contextlib
import re
import socket
import time
from collections.abc import Callable
from typing import Protocol

import paramiko

from obi.core.audit import record_device_command
from obi.core.config import SikluSettings
from obi.core.models import Device, EthPort, Reachability, RfLink, SikluRadioStatus

# The read-only command allowlist (all confirmed live, all non-mutating; research §3). The driver
# issues ONLY these, in this order, and nothing else.
SHOW_COMMANDS: tuple[str, ...] = (
    "show system",
    "show inventory",
    "show sw",
    "show rf",
    "show eth",
    "show ip",
)

# A trailing CLI prompt looks like ``siteA-ptp-siteB-70>``; match a generic ``…>`` or ``…#``.
_PROMPT_RE = re.compile(r"[>#]\s*$")
_AUTH_MARKERS = ("authentication", "auth fail", "permission denied", "not authorized")


class SikluSession(Protocol):
    """The slice of an SSH session the adapter relies on (send one command, read to prompt)."""

    def run(self, command: str) -> str:
        """Send ONE command and return its output up to (and excluding) the next prompt."""
        ...

    def close(self) -> None: ...


# (host, timeout) -> a connected, prompt-synchronized session.
SessionFactory = Callable[[str, float], SikluSession]


class SikluAuthError(Exception):
    """Raised by the session factory when the radio rejects obi's credentials."""


class _ParamikoShellSession:
    """A paramiko ``invoke_shell()`` session that reads until the EtherHaul prompt returns.

    The EtherHaul CLI is interactive-only, so each ``run`` writes ``f"{command}\\n"`` and reads
    until a trailing ``>``/``#`` prompt (bounded by ``timeout``). No pager was observed; a defensive
    space is sent on a ``--More--`` continuation should a future firmware page.
    """

    def __init__(
        self, client: paramiko.SSHClient, channel: paramiko.Channel, timeout: float
    ) -> None:
        self._client = client
        self._channel = channel
        self._timeout = timeout

    @staticmethod
    def _strip_prompt(text: str, command: str) -> str:
        """Drop the echoed command line and the trailing prompt line from a read block."""

        lines = text.splitlines()
        # The radio echoes the command we sent; drop the first echo line if present.
        if lines and command in lines[0]:
            lines = lines[1:]
        # Drop a trailing prompt line (``name>``) and any trailing blanks.
        while lines and (_PROMPT_RE.search(lines[-1]) or not lines[-1].strip()):
            lines.pop()
        return "\n".join(lines)

    def _read_until_prompt(self) -> str:
        chunks: list[str] = []
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            if self._channel.recv_ready():
                data = self._channel.recv(65536).decode("utf-8", errors="replace")
                chunks.append(data)
                joined = "".join(chunks)
                if "--More--" in joined:  # defensive paging (none seen live)
                    self._channel.send(b" ")
                    continue
                if _PROMPT_RE.search(joined):
                    return joined
            else:
                if self._channel.exit_status_ready():
                    break
                time.sleep(0.05)
        return "".join(chunks)

    def run(self, command: str) -> str:
        self._channel.send(f"{command}\n".encode())
        raw = self._read_until_prompt()
        return self._strip_prompt(raw, command)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._channel.close()
        with contextlib.suppress(Exception):
            self._client.close()


# --- value coercion ---------------------------------------------------------------------------


def _to_int(value: str | None) -> int | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _to_float(value: str | None) -> float | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _bool_up(value: str | None) -> bool | None:
    """Map a CLI state word to a tri-state bool: ``up``→True, ``down``→False, else ``None``."""

    if value is None:
        return None
    text = value.strip().lower()
    if text == "up":
        return True
    if text == "down":
        return False
    return None


# --- key-value parsers (pure) -----------------------------------------------------------------


def _kv_lines(text: str) -> list[tuple[str, str]]:
    """Split CLI output into ``(left, value)`` pairs on the first ``:`` of each non-blank line."""

    pairs: list[tuple[str, str]] = []
    for line in text.splitlines():
        if ":" not in line:
            continue
        left, value = line.split(":", 1)
        left = left.strip()
        if not left:
            continue
        pairs.append((left, value.strip()))
    return pairs


def parse_system(text: str) -> dict[str, str]:
    """Parse ``show system`` (bare ``system <field> : <value>``) into ``{field: value}``."""

    out: dict[str, str] = {}
    for left, value in _kv_lines(text):
        parts = left.split(None, 1)
        if len(parts) == 2 and parts[0] == "system":
            out[parts[1].strip()] = value
    return out


def parse_inventory(text: str) -> dict[int, dict[str, str]]:
    """Parse ``show inventory`` (``inventory <N> <field> : <value>``) by leading integer.

    Indices are sparse (the live unit skipped 7/8/11/12/15) — parsed by the leading integer, never
    assuming contiguity (research §3).
    """

    out: dict[int, dict[str, str]] = {}
    for left, value in _kv_lines(text):
        parts = left.split(None, 2)
        if len(parts) == 3 and parts[0] == "inventory":
            index = _to_int(parts[1])
            if index is None:
                continue
            out.setdefault(index, {})[parts[2].strip()] = value
    return out


def parse_sw(text: str) -> str | None:
    """Parse ``show sw`` and return the **running** flash bank's version, or ``None``.

    The output is a fixed-width table:
    ``Flash Bank | Version | Running | Scheduled to run | startup-config``. The running bank is the
    row whose ``Running`` column is ``yes``.
    """

    header_seen = False
    for line in text.splitlines():
        cols = line.split()
        if not cols:
            continue
        if cols[0].lower() == "flash":  # header row: "Flash Bank Version Running ..."
            header_seen = True
            continue
        if not header_seen:
            continue
        # data row: <bank> <version> <running> <scheduled> <startup-config>
        if len(cols) >= 3 and cols[0].isdigit() and cols[2].lower() == "yes":
            return cols[1]
    return None


def parse_rf(text: str) -> RfLink:
    """Parse ``show rf`` (``rf <field> : <value>``) into an :class:`RfLink`; all fields optional."""

    fields: dict[str, str] = {}
    for left, value in _kv_lines(text):
        parts = left.split(None, 1)
        if len(parts) == 2 and parts[0] == "rf":
            fields[parts[1].strip()] = value
    return RfLink(
        operational=_bool_up(fields.get("operational")),
        tx_state=fields.get("tx-state") or None,
        rx_state=fields.get("rx-state") or None,
        rssi_dbm=_to_int(fields.get("rssi")),
        cinr_db=_to_int(fields.get("cinr")),
        mode=fields.get("mode") or None,
        lowest_modulation=fields.get("lowest-modulation") or None,
        tx_power_dbm=_to_int(fields.get("tx-power")),
        air_capacity_mbps=_to_int(fields.get("air-capacity")),
        channel_width_mhz=_to_int(fields.get("channel-width")),
        tx_frequency_mhz=_to_int(fields.get("tx-frequency")),
        rx_frequency_mhz=_to_int(fields.get("rx-frequency")),
        alignment_status=fields.get("alignment-status") or None,
    )


# Ethernet ports rendered in their natural CLI order.
_ETH_PORT_ORDER = {"host": 0, "eth0": 1, "eth1": 2, "eth2": 3}
_SFP_DDM_FIELDS = {
    "sfp-ddm-temperature": "temperature",
    "sfp-ddm-vcc": "vcc",
    "sfp-ddm-tx-bias": "tx-bias",
    "sfp-ddm-tx-power": "tx-power",
    "sfp-ddm-rx-power": "rx-power",
}


def parse_eth(text: str) -> list[EthPort]:
    """Parse ``show eth`` (``eth <port> <field> : <value>``) into a list of :class:`EthPort`.

    SFP presence is derived from the ``sfp-ddm-*-status`` lines: any status other than ``No SFP``
    ⇒ present; all ``No SFP`` ⇒ absent; no DDM lines ⇒ ``None``. DDM readings are recorded only when
    an SFP is present (an empty slot reports all-zero ``No SFP`` readings, which are not real data).
    """

    by_port: dict[str, dict[str, str]] = {}
    order: list[str] = []
    for left, value in _kv_lines(text):
        parts = left.split(None, 2)
        if len(parts) == 3 and parts[0] == "eth":
            port = parts[1].strip()
            if port not in by_port:
                by_port[port] = {}
                order.append(port)
            by_port[port][parts[2].strip()] = value

    order.sort(key=lambda p: _ETH_PORT_ORDER.get(p, 99))
    ports: list[EthPort] = []
    for port in order:
        fields = by_port[port]
        # SFP presence + DDM.
        statuses = [
            v for k, v in fields.items() if k.startswith("sfp-ddm") and k.endswith("-status")
        ]
        sfp_present: bool | None
        sfp_ddm: dict[str, float] | None
        if not statuses:
            sfp_present = None
            sfp_ddm = None
        elif all(v.strip() == "No SFP" for v in statuses):
            sfp_present = False
            sfp_ddm = None
        else:
            sfp_present = True
            sfp_ddm = {}
            for ddm_key, label in _SFP_DDM_FIELDS.items():
                reading = _to_float(fields.get(ddm_key))
                if reading is not None:
                    sfp_ddm[label] = reading
            sfp_ddm = sfp_ddm or None

        ports.append(
            EthPort(
                name=port,
                admin_up=_bool_up(fields.get("admin")),
                operational_up=_bool_up(fields.get("operational")),
                mac=fields.get("mac-addr") or None,
                mtu=_to_int(fields.get("mtu")),
                speed_type=(fields.get("eth-act-type") or fields.get("eth-type") or None),
                connector_type=fields.get("connector-type") or None,
                tx_throughput=_to_int(fields.get("tx-throughput")),
                rx_throughput=_to_int(fields.get("rx-throughput")),
                sfp_present=sfp_present,
                sfp_ddm=sfp_ddm,
            )
        )
    return ports


def parse_ip(text: str) -> dict[int, dict[str, str]]:
    """Parse ``show ip`` (``ip <N> <field> : <value>``) by leading integer (sparse-index safe)."""

    out: dict[int, dict[str, str]] = {}
    for left, value in _kv_lines(text):
        parts = left.split(None, 2)
        if len(parts) == 3 and parts[0] == "ip":
            index = _to_int(parts[1])
            if index is None:
                continue
            out.setdefault(index, {})[parts[2].strip()] = value
    return out


_UPTIME_RE = re.compile(r"^(\d+):(\d+):(\d+):(\d+)$")


def parse_uptime(value: str | None) -> int | None:
    """Parse an EtherHaul uptime ``DDDD:HH:MM:SS`` into seconds; ``None`` if unparseable."""

    if value is None:
        return None
    match = _UPTIME_RE.match(value.strip())
    if not match:
        return None
    days, hours, minutes, seconds = (int(g) for g in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


# --- identity ---------------------------------------------------------------------------------


def _is_siklu(system: dict[str, str], inventory: dict[int, dict[str, str]]) -> bool:
    """A connected host is a Siklu radio iff it exposes a system name and the chassis is Siklu."""

    chassis = inventory.get(1, {})
    mfg = (chassis.get("mfg-name") or "").strip().lower()
    description = (system.get("description") or "").strip()
    has_name = bool((system.get("name") or "").strip())
    return has_name and (mfg == "siklu" or description.upper().startswith("EH-"))


def build_device(
    host: str,
    system: dict[str, str],
    inventory: dict[int, dict[str, str]],
    running_version: str | None,
) -> Device:
    """Build a :class:`Device` from the parsed reads (research §3–4).

    ``identity`` = system name; ``serial`` = chassis (inventory 1) serial; ``model`` = chassis
    model-name (fallback the system description); ``version`` = the running flash bank (fallback the
    chassis sw-rev); ``address`` = the roster host.
    """

    chassis = inventory.get(1, {})
    identity = (system.get("name") or "").strip()
    serial = (chassis.get("serial") or "").strip() or None
    model = (
        (chassis.get("model-name") or "").strip()
        or (system.get("description") or "").strip()
        or None
    )
    version = running_version or (chassis.get("sw-rev") or "").strip() or None
    return Device(
        identity=identity,
        address=host,
        model=model,
        version=version,
        serial=serial,
    )


# --- driver -----------------------------------------------------------------------------------


def _classify_connect_error(exc: Exception) -> Reachability:
    if isinstance(exc, SikluAuthError):
        return Reachability.AUTH_FAILED
    message = str(exc).lower()
    if isinstance(exc, paramiko.AuthenticationException) or any(
        marker in message for marker in _AUTH_MARKERS
    ):
        return Reachability.AUTH_FAILED
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return Reachability.TIMED_OUT
    if isinstance(exc, ConnectionRefusedError):
        return Reachability.UNREACHABLE
    if isinstance(exc, (paramiko.SSHException, OSError)):
        return Reachability.UNREACHABLE
    return Reachability.UNREACHABLE


class SikluDriver:
    """Read-only Siklu EtherHaul adapter (device-per-address; research §3).

    Not part of the generic ``VendorDriver`` Protocol — its health payload (RF + per-Ethernet-port)
    is richer than ``DeviceStatus`` — so it exposes its own :meth:`fetch_radio` returning a
    :class:`SikluRadioStatus`. The engine depends on this concrete driver via the injected session.
    """

    name = "siklu"

    def __init__(self, settings: SikluSettings, *, session: SessionFactory | None = None) -> None:
        self._settings = settings
        self._session: SessionFactory = session or self._default_session

    def _default_session(self, host: str, timeout: float) -> SikluSession:
        """Open a paramiko interactive shell to ``host`` and synchronize to the CLI prompt.

        The EtherHaul honors no exec channel, so we ``invoke_shell()`` and read the login banner up
        to the first prompt before any command is sent. The password is revealed only here, at
        connect time, and never logged.
        """

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=host,
                port=self._settings.port,
                username=self._settings.username,
                password=self._settings.password.reveal(),
                timeout=timeout,
                allow_agent=False,
                look_for_keys=False,
            )
        except paramiko.AuthenticationException as exc:
            client.close()
            raise SikluAuthError(str(exc)) from exc
        channel = client.invoke_shell()
        channel.settimeout(timeout)
        session = _ParamikoShellSession(client, channel, timeout)
        # Drain the login banner up to the first prompt so the first command's read is clean.
        session._read_until_prompt()
        return session

    def fetch_radio(self, host: str, *, timeout: float) -> SikluRadioStatus:
        """READ-ONLY, TOTAL, bounded. Query one radio; always return a :class:`SikluRadioStatus`.

        Opens one session, runs each :data:`SHOW_COMMANDS` entry, parses the key-value output, and
        always closes the session. MUST NOT raise: every failure resolves to a status carrying the
        mapped :class:`Reachability` plus a note (FR-007, FR-011). The engine — not the driver —
        writes the audit record.
        """

        try:
            session = self._session(host, timeout)
        except Exception as exc:  # noqa: BLE001 - total method: classify every failure mode
            reachability = _classify_connect_error(exc)
            return SikluRadioStatus(
                device=Device(identity=host, address=host),
                reachability=reachability,
                notes=[f"connection failed: {exc}"],
            )

        notes: list[str] = []
        outputs: dict[str, str] = {}
        try:
            for command in SHOW_COMMANDS:
                # feature 042: each show command is one audited device_command (ambient scope).
                start = time.monotonic()
                try:
                    outputs[command] = session.run(command)
                except Exception as exc:  # noqa: BLE001 - a single bad read is partial, not fatal
                    notes.append(f"{command} unavailable: {exc}")
                    outputs[command] = ""
                    record_device_command(
                        host,
                        "ssh",
                        command,
                        duration_ms=(time.monotonic() - start) * 1000.0,
                        outcome="error",
                        error=str(exc),
                    )
                else:
                    record_device_command(
                        host,
                        "ssh",
                        command,
                        response_bytes=len(outputs[command].encode("utf-8", "ignore")),
                        duration_ms=(time.monotonic() - start) * 1000.0,
                    )
        finally:
            with contextlib.suppress(Exception):  # closing must never surface an error
                session.close()

        system = parse_system(outputs.get("show system", ""))
        inventory = parse_inventory(outputs.get("show inventory", ""))
        running_version = parse_sw(outputs.get("show sw", ""))

        if not _is_siklu(system, inventory):
            return SikluRadioStatus(
                device=Device(identity=host, address=host),
                reachability=Reachability.UNIDENTIFIED,
                notes=[*notes, "connected but not a recognizable Siklu EtherHaul radio"],
            )

        device = build_device(host, system, inventory, running_version)
        rf = parse_rf(outputs.get("show rf", "")) if outputs.get("show rf") else None
        eth_ports = parse_eth(outputs.get("show eth", ""))

        uptime = parse_uptime(system.get("uptime"))
        temperature = _to_float(system.get("temperature"))
        location = (system.get("location") or "").strip() or None

        if uptime is None:
            notes.append("uptime unavailable")
        if temperature is None:
            notes.append("temperature unavailable")
        if rf is None:
            notes.append("rf link unavailable")

        return SikluRadioStatus(
            device=device,
            reachability=Reachability.REACHABLE,
            uptime_seconds=uptime,
            temperature_celsius=temperature,
            location=location,
            rf=rf,
            eth_ports=eth_ports,
            notes=notes,
        )

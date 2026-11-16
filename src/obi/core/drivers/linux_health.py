"""Read-only SSH health probe of the Linux collector VMs (feature 096).

The third sanctioned ``paramiko`` importer (after ``siklu.py`` and ``routeros_config.py``).
Exec channel, one fresh connection per command (the 026 shape) — a Linux sshd honors exec,
so no interactive shell is needed. Read-only by construction: the probe iterates ONLY the
module-constant :data:`PROBE_COMMANDS` (cat/grep/df heads); the host selects the
connection target, never command text (Constitution I / IV quarantine).

**Auth mode (deliberate, new here)**: key-based — ``look_for_keys=True`` and
``allow_agent=True``, and there is NO password anywhere. Both existing adapters are
password-only with keys disabled; this driver instead rides the operator's EXISTING
the log-shipping user's ed25519 trust (the log-shipping identity, verified live on all five
collectors),
so obi's config carries no secret for it (contracts/linux-probe-driver.md).

The transport step is injectable (``exec_fn``) so every test runs offline against recorded
fixtures. The driver is TOTAL: connect failures become typed outcomes, a single failing
command degrades to a note while the other commands still land, and absence is absence —
no reading is ever zero-filled (FR-003).
"""

from __future__ import annotations

import contextlib
import re
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import paramiko

from obi.core.audit import record_device_command
from obi.core.config import HealthLinuxSettings

# The static, read-only probe (research D5). Each entry is a compile-time literal:
#  1. two /proc/stat samples 2 s apart → a real CPU-utilization delta (not loadavg);
#  2. /proc/meminfo → total/available memory;
#  3. df over real local filesystems (pseudo-fs filtered again at parse time);
#  4. hwmon chip names + temperatures, self-labeling via grep's filename prefix; the
#     trailing `|| true` keeps a sensor-less host at exit 0 (absence is a state).
PROBE_COMMANDS: tuple[str, ...] = (
    "cat /proc/stat && sleep 2 && cat /proc/stat",
    "cat /proc/meminfo",
    "df -P -k -l -x tmpfs -x devtmpfs",
    "grep . /sys/class/hwmon/hwmon*/name /sys/class/hwmon/hwmon*/temp*_input "
    "2>/dev/null || true",
)

#: ``(host, command, timeout) -> stdout`` — the injectable transport seam (026 shape).
ExecFn = Callable[[str, str, float], str]


class LinuxProbeOutcome(StrEnum):
    """Whole-host probe outcome; per-command failures degrade to notes instead."""

    OK = "ok"
    AUTH_FAILED = "auth_failed"
    TIMED_OUT = "timed_out"
    UNREACHABLE = "unreachable"


@dataclass(frozen=True)
class LinuxHealthResult:
    """One host's probe readings. ``None``/empty means ABSENT (named in ``notes``), never 0.

    ``cpu_util_percent_x10`` and each disk/temp value are ×10 integers (one decimal),
    matching the store's raw-value conventions (data-model.md §1)."""

    host: str
    outcome: LinuxProbeOutcome
    cpu_util_percent_x10: int | None = None
    mem_total_kb: int | None = None
    mem_available_kb: int | None = None
    disks: tuple[tuple[str, int], ...] = ()  # (mount point, used percent ×10)
    temps: tuple[tuple[str, str, int], ...] = ()  # (instance, chip label, deci-°C)
    notes: tuple[str, ...] = ()


# --- pure parsers (fixture-tested; no I/O) ----------------------------------------------------


def parse_proc_stat_delta(text: str) -> float | None:
    """CPU utilization %% from two aggregate ``cpu `` lines (first vs last).

    busy = total − (idle + iowait). A wrapped/reset counter (negative or inverted delta)
    yields ``None`` — never a fabricated number."""

    lines = [line for line in text.splitlines() if line.startswith("cpu ")]
    if len(lines) < 2:
        return None
    try:
        first = [int(x) for x in lines[0].split()[1:]]
        last = [int(x) for x in lines[-1].split()[1:]]
    except ValueError:
        return None
    if len(first) < 4 or len(last) < 4:
        return None

    def idle(fields: list[int]) -> int:
        return fields[3] + (fields[4] if len(fields) > 4 else 0)

    d_total = sum(last) - sum(first)
    d_idle = idle(last) - idle(first)
    if d_total <= 0 or d_idle < 0 or d_idle > d_total:
        return None
    return 100.0 * (d_total - d_idle) / d_total


def parse_meminfo(text: str) -> tuple[int, int] | None:
    """``(MemTotal kB, MemAvailable kB)`` or ``None`` when either line is missing."""

    total = re.search(r"^MemTotal:\s+(\d+)\s+kB", text, re.MULTILINE)
    available = re.search(r"^MemAvailable:\s+(\d+)\s+kB", text, re.MULTILINE)
    if total is None or available is None:
        return None
    return int(total.group(1)), int(available.group(1))


def parse_df(text: str) -> list[tuple[str, int]]:
    """``(mount point, used percent ×10)`` per REAL filesystem (device under ``/dev/``).

    Pseudo-filesystems that slip past df's exclusions (efivarfs, seen live on core-vms-1)
    are dropped here — they are not disks and must not page anyone."""

    disks: list[tuple[str, int]] = []
    for line in text.splitlines()[1:]:
        parts = line.split(None, 5)
        if len(parts) < 6 or not parts[0].startswith("/dev/"):
            continue
        try:
            used, available = int(parts[2]), int(parts[3])
        except ValueError:
            continue
        denominator = used + available
        if denominator <= 0:
            continue
        disks.append((parts[5].strip(), round(1000 * used / denominator)))
    return disks


def parse_hwmon(text: str) -> list[tuple[str, str, int]]:
    """``(instance, chip label, deci-°C)`` per temp input; ``[]`` when nothing is exposed.

    Input lines are grep's ``path:content`` pairs; chip names label their hwmon's temps."""

    names: dict[str, str] = {}
    for match in re.finditer(r"/hwmon(\d+)/name:(\S+)", text):
        names[match.group(1)] = match.group(2)
    temps: list[tuple[str, str, int]] = []
    for match in re.finditer(r"/hwmon(\d+)/(temp\d+)_input:(-?\d+)", text):
        chip, sensor, milli = match.group(1), match.group(2), int(match.group(3))
        instance = f"hwmon{chip}/{sensor}"
        temps.append((instance, names.get(chip, f"hwmon{chip}"), milli // 100))
    return temps


# --- driver -----------------------------------------------------------------------------------


def _short(exc: BaseException) -> str:
    """A one-line, secret-free failure note, bounded."""

    return f"{type(exc).__name__}: {exc}"[:200]


def _classify(exc: BaseException) -> LinuxProbeOutcome:
    if isinstance(exc, paramiko.AuthenticationException):
        return LinuxProbeOutcome.AUTH_FAILED
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return LinuxProbeOutcome.TIMED_OUT
    return LinuxProbeOutcome.UNREACHABLE


class LinuxHealthDriver:
    """Total, read-only probe driver over the injectable :data:`ExecFn` seam."""

    def __init__(self, settings: HealthLinuxSettings, *, exec_fn: ExecFn | None = None) -> None:
        self._settings = settings
        self._exec: ExecFn = exec_fn if exec_fn is not None else self._ssh_exec

    def _ssh_lookup(self, host: str) -> dict[str, object]:
        """Resolve ``host`` through the operator's ``~/.ssh/config`` — hostname aliasing
        and the (non-default-named) IdentityFile ARE the existing trust this driver rides;
        paramiko reads neither on its own (verified live: bare paramiko cannot reach any
        collector that plain ``ssh`` reaches)."""

        ssh_config = paramiko.SSHConfig()
        path = Path.home() / ".ssh" / "config"
        with contextlib.suppress(OSError):
            ssh_config = paramiko.SSHConfig.from_path(str(path))
        entry = ssh_config.lookup(host)
        return {
            "hostname": entry.get("hostname", host),
            "port": int(str(entry.get("port", 22))),
            "key_filename": entry.get("identityfile"),
        }

    def _ssh_exec(self, host: str, command: str, timeout: float) -> str:
        """Default transport: one exec-channel command over the operator's key trust."""

        resolved = self._ssh_lookup(host)
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=str(resolved["hostname"]),
                port=int(str(resolved["port"])),
                username=self._settings.username,
                key_filename=resolved["key_filename"],  # type: ignore[arg-type]
                timeout=timeout,
                allow_agent=True,
                look_for_keys=True,
            )
            _, stdout, stderr = client.exec_command(command, timeout=timeout)
            data = stdout.read().decode(errors="replace")
            status = stdout.channel.recv_exit_status()
            if status != 0:
                detail = stderr.read().decode(errors="replace")[:120]
                raise paramiko.SSHException(f"exit {status}: {detail}")
            return data
        finally:
            with contextlib.suppress(Exception):
                client.close()

    def fetch_host_health(
        self, host: str, *, timeout: float | None = None
    ) -> LinuxHealthResult:
        """READ-ONLY. Probe one host; total (never raises), bounded per command."""

        effective_timeout = timeout if timeout is not None else self._settings.timeout
        outputs: dict[str, str] = {}
        errors: dict[str, BaseException] = {}
        for command in PROBE_COMMANDS:  # ONLY module constants ever reach the transport
            started = time.monotonic()
            try:
                text = self._exec(host, command, effective_timeout)
            except Exception as exc:  # noqa: BLE001 - total driver: classify, never raise
                errors[command] = exc
                record_device_command(
                    host,
                    "ssh",
                    command,
                    duration_ms=(time.monotonic() - started) * 1000,
                    outcome="error",
                    error=_short(exc),
                )
                continue
            outputs[command] = text
            record_device_command(
                host,
                "ssh",
                command,
                response_bytes=len(text),
                duration_ms=(time.monotonic() - started) * 1000,
            )

        if not outputs and errors:
            first = next(iter(errors.values()))
            return LinuxHealthResult(
                host=host,
                outcome=_classify(first),
                notes=(f"probe failed: {_short(first)}",),
            )

        notes = [
            f"{command.split()[0]} unavailable: {_short(exc)}"
            for command, exc in errors.items()
        ]

        cpu = parse_proc_stat_delta(outputs.get(PROBE_COMMANDS[0], ""))
        if cpu is None:
            notes.append("cpu utilization unavailable")
        mem = parse_meminfo(outputs.get(PROBE_COMMANDS[1], ""))
        if mem is None:
            notes.append("memory readings unavailable")
        disks = tuple(parse_df(outputs.get(PROBE_COMMANDS[2], "")))
        if not disks:
            notes.append("no filesystems parsed")
        temps = tuple(parse_hwmon(outputs.get(PROBE_COMMANDS[3], "")))
        if not temps:
            notes.append("no thermal sensors exposed (VM-normal)")

        return LinuxHealthResult(
            host=host,
            outcome=LinuxProbeOutcome.OK,
            cpu_util_percent_x10=round(cpu * 10) if cpu is not None else None,
            mem_total_kb=mem[0] if mem else None,
            mem_available_kb=mem[1] if mem else None,
            disks=disks,
            temps=temps,
            notes=tuple(notes),
        )

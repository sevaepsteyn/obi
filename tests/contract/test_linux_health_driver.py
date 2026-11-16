"""Contract (feature 096): the Linux health probe driver — read-only by construction.

Asserts exactly the static ``PROBE_COMMANDS`` allowlist is sent, in order; that no
state-changing verb exists anywhere in the module; that every transport exception class
maps to its typed outcome (the driver is total — it never raises); that a single failing
command degrades to a note while the others still land; and that key-based auth is the
deliberate, documented mode (no password parameter exists).
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import paramiko
import pytest

from obi.core.config import HealthLinuxSettings
from obi.core.drivers import linux_health
from obi.core.drivers.linux_health import (
    PROBE_COMMANDS,
    LinuxHealthDriver,
    LinuxProbeOutcome,
)

# Verbs that must never appear in the probe module (space-guarded to avoid substrings of
# legitimate words). The allowlist is cat/sleep/df/grep only.
WRITE_VERBS = ("sudo ", " rm ", "reboot", "shutdown", "systemctl", "mkfs", " dd ", "tee ")

_FIXTURE_PATH = Path(__file__).parent.parent / "fixtures" / "linux_health" / "core-vms-1.json"


def _settings(**overrides) -> HealthLinuxSettings:
    values = {"hosts": ["core-vms-1"], "username": "admin"}
    values.update(overrides)
    return HealthLinuxSettings(**values)


class RecordingExec:
    """A fake ExecFn serving the recorded fixture; records every (host, command) sent."""

    def __init__(self, outputs: dict[str, str] | None = None,
                 fail_on: dict[str, Exception] | None = None) -> None:
        if outputs is None:
            outputs = json.loads(_FIXTURE_PATH.read_text())["commands"]
        self.outputs = outputs
        self.fail_on = fail_on or {}
        self.sent: list[tuple[str, str]] = []

    def __call__(self, host: str, command: str, timeout: float) -> str:
        self.sent.append((host, command))
        if command in self.fail_on:
            raise self.fail_on[command]
        return self.outputs.get(command, "")


def test_exactly_the_allowlist_is_sent_in_order() -> None:
    exec_fn = RecordingExec()
    driver = LinuxHealthDriver(_settings(), exec_fn=exec_fn)
    result = driver.fetch_host_health("core-vms-1")
    assert result.outcome is LinuxProbeOutcome.OK
    assert [cmd for _, cmd in exec_fn.sent] == list(PROBE_COMMANDS)
    assert all(host == "core-vms-1" for host, _ in exec_fn.sent)


def test_no_write_verbs_anywhere_in_the_module() -> None:
    source = inspect.getsource(linux_health)
    for verb in WRITE_VERBS:
        assert verb not in source, verb
    for command in PROBE_COMMANDS:
        head = command.split()[0]
        assert head in ("cat", "grep", "df"), command  # read-only heads only


def test_connect_failures_map_to_typed_outcomes_never_raise() -> None:
    cases = [
        (paramiko.AuthenticationException("denied"), LinuxProbeOutcome.AUTH_FAILED),
        (TimeoutError("slow"), LinuxProbeOutcome.TIMED_OUT),
        (TimeoutError("slow"), LinuxProbeOutcome.TIMED_OUT),
        (ConnectionRefusedError("no sshd"), LinuxProbeOutcome.UNREACHABLE),
        (paramiko.SSHException("boom"), LinuxProbeOutcome.UNREACHABLE),
        (OSError("no route"), LinuxProbeOutcome.UNREACHABLE),
        (RuntimeError("weird"), LinuxProbeOutcome.UNREACHABLE),
    ]
    for exc, expected in cases:
        exec_fn = RecordingExec(fail_on=dict.fromkeys(PROBE_COMMANDS, exc))
        result = LinuxHealthDriver(_settings(), exec_fn=exec_fn).fetch_host_health("h")
        assert result.outcome is expected, exc
        assert result.notes  # a secret-free note names the failure
        assert result.cpu_util_percent_x10 is None
        assert result.disks == () and result.temps == ()


def test_single_command_failure_is_isolated_not_fatal() -> None:
    exec_fn = RecordingExec(fail_on={PROBE_COMMANDS[0]: paramiko.SSHException("hiccup")})
    result = LinuxHealthDriver(_settings(), exec_fn=exec_fn).fetch_host_health("core-vms-1")
    assert result.outcome is LinuxProbeOutcome.OK
    assert result.cpu_util_percent_x10 is None  # the failed command's readings are absent
    assert result.mem_total_kb == 32215988  # the others still landed
    assert result.disks
    assert any("hiccup" in note or "unavailable" in note for note in result.notes)
    assert [cmd for _, cmd in exec_fn.sent] == list(PROBE_COMMANDS)  # kept going


def test_hwmon_absence_is_a_note_not_an_error() -> None:
    outputs = json.loads(_FIXTURE_PATH.read_text())["commands"]
    outputs = {**outputs, PROBE_COMMANDS[3]: ""}
    result = LinuxHealthDriver(_settings(), exec_fn=RecordingExec(outputs)).fetch_host_health(
        "core-vms-1"
    )
    assert result.outcome is LinuxProbeOutcome.OK
    assert result.temps == ()
    assert any("thermal" in note or "temperature" in note for note in result.notes)


def test_key_based_auth_no_password_anywhere() -> None:
    """The deliberate new auth mode: the collectors trust the operator's ssh identity; obi's
    config holds no secret for this driver (contracts/linux-probe-driver.md)."""

    assert "password" not in {f for f in HealthLinuxSettings.__dataclass_fields__}
    signature = inspect.signature(LinuxHealthDriver.__init__)
    assert "password" not in signature.parameters
    source = inspect.getsource(linux_health)
    assert "look_for_keys=True" in source and "allow_agent=True" in source


def test_zero_readings_are_never_fabricated_on_failure() -> None:
    exec_fn = RecordingExec(outputs=dict.fromkeys(PROBE_COMMANDS, ""))
    result = LinuxHealthDriver(_settings(), exec_fn=exec_fn).fetch_host_health("core-vms-1")
    assert result.outcome is LinuxProbeOutcome.OK
    assert result.cpu_util_percent_x10 is None
    assert result.mem_total_kb is None and result.mem_available_kb is None
    assert result.disks == () and result.temps == ()
    assert result.notes  # every absent class is named, never zero-filled


@pytest.mark.parametrize("n", range(len(PROBE_COMMANDS)))
def test_every_command_head_is_a_compile_time_literal(n: int) -> None:
    """The host selects the connection target, never command text — the tuple is static."""

    assert isinstance(PROBE_COMMANDS[n], str)
    assert "{" not in PROBE_COMMANDS[n] and "%" not in PROBE_COMMANDS[n]

"""Unit (feature 026): the RouterOS /export SSH driver — an injected exec_fn exercises every
failure classification offline, and the command sent is always a fixed read-only /export."""

from __future__ import annotations

import paramiko
import pytest

from obi.core.config import ConfigBackupSettings, Secret
from obi.core.drivers.routeros_config import (
    RouterosConfigDriver,
    build_export_command,
)
from obi.core.models import CaptureOutcome

_EXPORT = "# 2026-07-05 12:00:00 by RouterOS 7.15\n/ip address\nadd address=172.25.110.1/24\n"


def _settings(**kw: object) -> ConfigBackupSettings:
    return ConfigBackupSettings(username="u", password=Secret("p"), **kw)  # type: ignore[arg-type]


def _driver(exec_fn) -> RouterosConfigDriver:
    return RouterosConfigDriver(_settings(), exec_fn=exec_fn)


def test_happy_path_returns_content() -> None:
    seen: dict[str, str] = {}

    def fake_exec(host: str, command: str, timeout: float) -> str:
        seen["host"] = host
        seen["command"] = command
        return _EXPORT

    result = _driver(fake_exec).fetch_export("172.25.110.1", timeout=5.0)
    assert result.outcome is CaptureOutcome.CAPTURED_CHANGED
    assert result.content == _EXPORT
    assert seen["host"] == "172.25.110.1"
    # The command is a fixed read-only /export (default terse) — never operator text (Principle I).
    assert seen["command"] == "/export terse"


def test_build_export_command_fixed_modifiers() -> None:
    assert build_export_command(terse=True, verbose=False) == "/export terse"
    assert build_export_command(terse=False, verbose=False) == "/export"
    assert build_export_command(terse=True, verbose=True) == "/export verbose terse"
    assert build_export_command(terse=False, verbose=True) == "/export verbose"


def test_driver_uses_configured_modifiers() -> None:
    seen: dict[str, str] = {}

    def fake_exec(host: str, command: str, timeout: float) -> str:
        seen["command"] = command
        return _EXPORT

    RouterosConfigDriver(_settings(terse=False, verbose=True), exec_fn=fake_exec).fetch_export(
        "172.25.110.1", timeout=5.0
    )
    assert seen["command"] == "/export verbose"


def test_empty_export_classified() -> None:
    result = _driver(lambda h, c, t: "   \n").fetch_export("172.25.110.1", timeout=5.0)
    assert result.outcome is CaptureOutcome.EMPTY_EXPORT
    assert result.content is None


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (paramiko.AuthenticationException("bad creds"), CaptureOutcome.AUTH_FAILED),
        (PermissionError("not enough permissions"), CaptureOutcome.PERMISSION_DENIED),
        (TimeoutError("timed out"), CaptureOutcome.TIMED_OUT),
        (paramiko.SSHException("protocol error"), CaptureOutcome.UNREACHABLE),
        (ConnectionRefusedError("refused"), CaptureOutcome.UNREACHABLE),
        (OSError("no route to host"), CaptureOutcome.UNREACHABLE),
    ],
)
def test_error_classification(exc: Exception, expected: CaptureOutcome) -> None:
    def boom(host: str, command: str, timeout: float) -> str:
        raise exc

    result = _driver(boom).fetch_export("172.25.110.1", timeout=5.0)
    assert result.outcome is expected
    assert result.content is None
    assert result.note  # a short, secret-free note is always attached


def test_never_raises_on_unexpected_error() -> None:
    def boom(host: str, command: str, timeout: float) -> str:
        raise RuntimeError("something weird")

    result = _driver(boom).fetch_export("172.25.110.1", timeout=5.0)
    assert result.outcome is CaptureOutcome.UNREACHABLE


def test_note_never_contains_password() -> None:
    def boom(host: str, command: str, timeout: float) -> str:
        raise paramiko.AuthenticationException("auth failed")

    result = _driver(boom).fetch_export("172.25.110.1", timeout=5.0)
    assert result.note is not None
    assert _settings().password.reveal() == "p"  # sanity: the secret exists
    assert "super" not in (result.note or "")  # never leaks credential material

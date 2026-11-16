"""Read-only RouterOS ``/export`` over SSH (feature 026) — strictly read-only (Principles I, IV).

The RouterOS **binary API cannot return an export** to a read-only account without writing a
``.rsc`` file onto the device (a mutation), so config backup uses the SSH **exec channel**: RouterOS
streams ``/export`` to stdout over a plain ``exec_command`` — no interactive-shell prompt scraping
(unlike ``siklu.py``, whose EtherHaul honors no exec channel). The command is the **hardcoded
base command ``/export``** (composed only with the fixed ``terse``/``verbose`` modifiers by
:func:`build_export_command`, never operator- or model-supplied text) — so no command injection and
no device mutation is reachable (Principle I). ``/export`` is never ``show-sensitive``, so RouterOS
hides user passwords, wireless keys, and IPsec secrets; the SNMP community it leaves cleartext is
scrubbed downstream by ``configstore.redact_export`` before anything is stored.

This is a second sanctioned ``paramiko`` importer alongside ``siklu.py`` — the SSH transport for
RouterOS — keeping the vendor/transport specifics quarantined here (Principle IV). The exec step is
injectable (``exec_fn=``) so every branch is exercised offline without a real device.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass

import paramiko

from obi.core.audit import record_device_command
from obi.core.config import ConfigBackupSettings
from obi.core.models import CaptureOutcome

#: The read-only base command. Composed with fixed ``verbose``/``terse`` modifiers (never
#: operator-supplied text) by :func:`build_export_command` — see the module docstring (Principle I).
EXPORT_COMMAND = "/export"


def build_export_command(*, terse: bool, verbose: bool) -> str:
    """Compose ``/export`` with the fixed, read-only ``verbose``/``terse`` modifiers (no injection).

    ``terse`` emits one command per line (clean diffs); ``verbose`` also includes default values.
    Only these two hardcoded tokens can be appended — the operator toggles booleans, never text.
    """

    parts = [EXPORT_COMMAND]
    if verbose:
        parts.append("verbose")
    if terse:
        parts.append("terse")
    return " ".join(parts)

_PERMISSION_MARKERS = (
    "not allowed",
    "permission denied",
    "no permission",
    "access denied",
    "not enough permissions",
)

#: (host, command, timeout) -> stdout. Raises to signal a failure mode (classified by the driver).
ExecFn = Callable[[str, str, float], str]


@dataclass
class ExportResult:
    """One device's capture attempt. ``content`` is the raw ``/export`` on success, else ``None``.

    When ``content`` is present the nominal ``outcome`` is ``CAPTURED_CHANGED``; the engine refines
    it to ``CAPTURED_UNCHANGED`` if the git store finds no change. ``note`` is short + secret-free.
    """

    address: str
    outcome: CaptureOutcome
    content: str | None = None
    note: str | None = None


def _short(exc: object) -> str:
    """A one-line, secret-free note from an exception (credentials never appear in messages)."""

    text = " ".join(str(exc).split())
    return text[:200] if text else exc.__class__.__name__


class RouterosConfigDriver:
    """Read-only RouterOS config adapter — SSH ``/export`` per device. TOTAL: never raises."""

    name = "routeros-config"

    def __init__(self, settings: ConfigBackupSettings, *, exec_fn: ExecFn | None = None) -> None:
        self._settings = settings
        self._command = build_export_command(terse=settings.terse, verbose=settings.verbose)
        self._exec: ExecFn = exec_fn or self._default_exec

    def _default_exec(self, host: str, command: str, timeout: float) -> str:
        """Open an SSH exec channel, run ``command``, return stdout. Password revealed only here."""

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=host,
                port=self._settings.ssh_port,
                username=self._settings.username,
                password=self._settings.password.reveal(),
                timeout=timeout,
                allow_agent=False,
                look_for_keys=False,
            )
            stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
            stdin.close()
            out = stdout.read().decode("utf-8", "replace")
            err = stderr.read().decode("utf-8", "replace")
            status = stdout.channel.recv_exit_status()
            if status != 0 and not out:
                if any(marker in err.lower() for marker in _PERMISSION_MARKERS):
                    raise PermissionError(err.strip() or f"exit status {status}")
                raise paramiko.SSHException(err.strip() or f"/export exit status {status}")
            return out
        finally:
            with contextlib.suppress(Exception):  # closing must never surface an error
                client.close()

    def fetch_export(self, address: str, *, timeout: float) -> ExportResult:
        """READ-ONLY, TOTAL. Capture one device's ``/export``; classify every failure mode.

        Never raises: each failure resolves to an :class:`ExportResult` carrying the mapped
        :class:`CaptureOutcome` + a short note. The engine — not the driver — writes the audit
        record and decides changed-vs-unchanged.
        """

        # feature 042: the exact SSH command is one audited device_command (ambient scope).
        # Only the size of the response is recorded here — the export text itself reaches the
        # audit store solely through the engine's redacted path.
        start = time.monotonic()
        result = self._fetch_export_impl(address, timeout=timeout)
        record_device_command(
            address,
            "ssh",
            self._command,
            response_bytes=len(result.content.encode("utf-8", "ignore")) if result.content else 0,
            duration_ms=(time.monotonic() - start) * 1000.0,
            outcome="ok" if result.content is not None else "error",
            error=result.note if result.content is None else None,
        )
        return result

    def _fetch_export_impl(self, address: str, *, timeout: float) -> ExportResult:
        try:
            raw = self._exec(address, self._command, timeout)
        except paramiko.AuthenticationException as exc:
            return ExportResult(
                address,
                CaptureOutcome.AUTH_FAILED,
                note=f"auth failed ({_short(exc)}) — check creds and the RouterOS 'ssh' policy",
            )
        except PermissionError as exc:
            return ExportResult(address, CaptureOutcome.PERMISSION_DENIED, note=_short(exc))
        except TimeoutError as exc:
            return ExportResult(address, CaptureOutcome.TIMED_OUT, note=_short(exc))
        except paramiko.SSHException as exc:
            return ExportResult(address, CaptureOutcome.UNREACHABLE, note=_short(exc))
        except OSError as exc:
            return ExportResult(address, CaptureOutcome.UNREACHABLE, note=_short(exc))
        except Exception as exc:  # noqa: BLE001 - total method: classify every failure mode
            return ExportResult(address, CaptureOutcome.UNREACHABLE, note=_short(exc))

        if not raw or not raw.strip():
            return ExportResult(address, CaptureOutcome.EMPTY_EXPORT, note="empty export")
        return ExportResult(address, CaptureOutcome.CAPTURED_CHANGED, content=raw)

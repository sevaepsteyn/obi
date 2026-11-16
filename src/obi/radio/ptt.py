"""PTT control of the FTDI half — the only module that can key the transmitter (feature 088).

Everything here is dictated by measured criticalrf findings (contracts/tx-gate.md G3/G4/G7):

- **Both DTR and RTS key the transmitter** (confirmed on air), so idle means BOTH low, and
  every unkey clears both. Keying asserts RTS only (the PTT convention; DTR never rises).
- **The driver asserts both lines during open()** — an unavoidable 1–10 ms key transient —
  so the port is opened exactly ONCE per process lifetime and held forever. A per-TX open
  would key the radio on every transmission.
- **Sole-holder exclusivity is a safety property**: with one holder, a killed process
  de-keys at last close (confirmed); with two, the radio stays keyed (confirmed). The
  :class:`PttController` therefore refuses to exist without evidence of the held station
  lock (:func:`obi.radio.runner.acquire_station_lock`, taken before any device is touched),
  and the real port is additionally opened with the kernel's exclusive flag.
- **De-key on every exit path**: unkey runs on signal delivery, at interpreter exit, on
  close, and — the last-resort backstop — from an independent watchdog timer that fires if
  a playback thread wedges while keyed.

The LLM has no path to this module (tx-gate G1): only the TxExecutor holds a controller.
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import threading
from collections.abc import Callable
from typing import Protocol


class PttPort(Protocol):
    """The modem-control seam. The real implementation is :class:`SerialPttPort`."""

    def open(self) -> None: ...  # pragma: no cover - protocol
    def set_dtr(self, value: bool) -> None: ...  # pragma: no cover - protocol
    def set_rts(self, value: bool) -> None: ...  # pragma: no cover - protocol
    def close(self) -> None: ...  # pragma: no cover - protocol


class _Timer(Protocol):
    def start(self) -> None: ...  # pragma: no cover - protocol
    def cancel(self) -> None: ...  # pragma: no cover - protocol


def _default_timer(seconds: float, callback: Callable[[], None]) -> _Timer:
    timer = threading.Timer(seconds, callback)
    timer.daemon = True
    return timer


class PttController:
    """Owns the one open port and guarantees de-key on every exit path."""

    def __init__(
        self,
        *,
        port_factory: Callable[[], PttPort],
        station_lock: object | None,
        watchdog_s: float,
        timer_factory: Callable[[float, Callable[[], None]], _Timer] = _default_timer,
        atexit_register: Callable[[Callable[[], None]], object] = atexit.register,
    ) -> None:
        if station_lock is None:
            raise RuntimeError(
                "PttController requires the held station lock — the port must never be "
                "opened without sole-holder evidence (a second fd turns a killed process "
                "into a stuck transmitter)."
            )
        self._port_factory = port_factory
        self._station_lock = station_lock  # held by reference for the controller's lifetime
        self._watchdog_s = watchdog_s
        self._timer_factory = timer_factory
        self._atexit_register = atexit_register
        self._port: PttPort | None = None
        self._watchdog: _Timer | None = None
        self._keyed = False
        self._closed = False
        self._lock = threading.Lock()

    @property
    def keyed(self) -> bool:
        return self._keyed

    def open(self) -> None:
        """Open the port ONCE and immediately drop both lines (the open-transient answer)."""

        if self._port is not None or self._closed:
            raise RuntimeError("the PTT port is opened exactly once per process lifetime")
        port = self._port_factory()
        port.open()
        # The driver raised DTR/RTS during open (1-10 ms of unavoidable RF) — clear NOW.
        port.set_dtr(False)
        port.set_rts(False)
        self._port = port
        self._atexit_register(self._dekey_quietly)

    def key(self) -> None:
        """Assert PTT (RTS only) and arm the watchdog. Callers pair with try/finally unkey."""

        with self._lock:
            if self._port is None:
                raise RuntimeError("PTT port is not open")
            self._port.set_rts(True)
            self._keyed = True
            watchdog = self._timer_factory(self._watchdog_s, self._dekey_quietly)
            watchdog.start()
            self._watchdog = watchdog

    def unkey(self) -> None:
        """Drop BOTH lines (either one keys) and disarm the watchdog. Idempotent."""

        with self._lock:
            self._unkey_locked()

    def _unkey_locked(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None
        if self._port is not None:
            self._port.set_rts(False)
            self._port.set_dtr(False)
        self._keyed = False

    def _dekey_quietly(self) -> None:
        """The watchdog / atexit path: best-effort de-key that never raises."""

        with contextlib.suppress(Exception):  # a de-key path must never propagate
            self.unkey()

    def handle_signal(self, _signum: int, _frame: object) -> None:
        """SIGINT/SIGTERM/SIGHUP path: de-key before whatever teardown follows."""

        self._dekey_quietly()

    def close(self) -> None:
        """Unkey, then close the port (the genuine last close drops the lines in-kernel).

        NEVER raises on device loss: close is a de-key path, and the de-key discipline
        ("must never propagate", :meth:`_dekey_quietly`) applies here too. Field
        2026-08-29 01:26 + 01:28: the FTDI vanished mid-shutdown (USB re-enumeration
        under restart churn) and the resulting Errno 6 turned two CLEAN exits into
        crash exits — launchd's crash throttling then held the station deaf for six
        minutes. A vanished device is physically de-keyed (the PTT line floats, the
        radio releases), so swallowing is RF-safe; the record stays loud in the log.
        """

        with self._lock:
            if self._closed:
                return
            try:
                self._unkey_locked()
            except OSError as exc:
                logging.getLogger(__name__).warning(
                    "PTT serial failed during close (%s) — a vanished device is "
                    "already de-keyed by disconnect",
                    exc,
                )
                self._keyed = False
            if self._port is not None:
                try:
                    with contextlib.suppress(OSError):
                        self._port.close()
                finally:
                    self._port = None
            self._closed = True


class SerialPttPort:
    """The real FTDI port via pyserial — exclusive open, lazily imported (`radio` extra)."""

    def __init__(self, device: str) -> None:
        self._device = device
        self._serial: object | None = None

    def open(self) -> None:
        import serial

        # exclusive=True adds the kernel's TIOCEXCL on top of the station flock: a second
        # open of the tty fails outright instead of silently creating the stuck-PTT case.
        self._serial = serial.Serial(self._device, exclusive=True)

    def set_dtr(self, value: bool) -> None:
        assert self._serial is not None
        self._serial.dtr = value  # type: ignore[attr-defined]

    def set_rts(self, value: bool) -> None:
        assert self._serial is not None
        self._serial.rts = value  # type: ignore[attr-defined]

    def close(self) -> None:
        if self._serial is not None:
            self._serial.close()  # type: ignore[attr-defined]
            self._serial = None

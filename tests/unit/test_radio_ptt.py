"""Unit (feature 088, US2): the PTT controller against a recording fake port.

Covers tx-gate G3/G4/G7's port half: lock-required-before-open, open exactly once,
both lines cleared immediately after open and on every unkey, RTS-only keying, the
watchdog force-unkey, signal/atexit de-key paths, and the loud second-holder refusal.
No pyserial import, no real port (FR-036).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from obi.radio.ptt import PttController
from obi.radio.runner import acquire_station_lock


@dataclass
class FakePttPort:
    """Records every operation in order; the assertions read the tape."""

    ops: list[object] = field(default_factory=list)

    def open(self) -> None:
        self.ops.append("open")

    def set_dtr(self, value: bool) -> None:
        self.ops.append(("dtr", value))

    def set_rts(self, value: bool) -> None:
        self.ops.append(("rts", value))

    def close(self) -> None:
        self.ops.append("close")


@dataclass
class FakeTimer:
    """Captures the watchdog callback instead of scheduling it."""

    seconds: float
    callback: Callable[[], None]
    started: bool = False
    cancelled: bool = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True


class TimerFactory:
    def __init__(self) -> None:
        self.timers: list[FakeTimer] = []

    def __call__(self, seconds: float, callback: Callable[[], None]) -> FakeTimer:
        timer = FakeTimer(seconds, callback)
        self.timers.append(timer)
        return timer


def _controller(
    port: FakePttPort, *, timers: TimerFactory | None = None, atexit_reg: object = None
) -> PttController:
    return PttController(
        port_factory=lambda: port,
        station_lock=object(),  # evidence the caller holds the exclusive station lock (G3)
        watchdog_s=20.0,
        timer_factory=timers or TimerFactory(),
        atexit_register=atexit_reg or (lambda fn: None),
    )


def test_lock_is_required_before_any_port_exists() -> None:
    with pytest.raises(RuntimeError, match="station lock"):
        PttController(
            port_factory=FakePttPort,
            station_lock=None,
            watchdog_s=20.0,
            timer_factory=TimerFactory(),
            atexit_register=lambda fn: None,
        )


def test_open_clears_both_lines_immediately() -> None:
    port = FakePttPort()
    controller = _controller(port)
    controller.open()
    # The open() transient keys the radio 1-10 ms (unavoidable); both lines drop at once.
    assert port.ops == ["open", ("dtr", False), ("rts", False)]


def test_open_exactly_once_per_lifetime() -> None:
    port = FakePttPort()
    controller = _controller(port)
    controller.open()
    with pytest.raises(RuntimeError, match="once"):
        controller.open()
    assert port.ops.count("open") == 1


def test_key_asserts_rts_only_and_unkey_clears_both() -> None:
    port = FakePttPort()
    controller = _controller(port)
    controller.open()
    port.ops.clear()
    controller.key()
    assert port.ops == [("rts", True)]  # DTR stays low — RTS is the PTT convention
    assert controller.keyed
    port.ops.clear()
    controller.unkey()
    assert ("rts", False) in port.ops and ("dtr", False) in port.ops
    assert not controller.keyed


def test_key_requires_open() -> None:
    controller = _controller(FakePttPort())
    with pytest.raises(RuntimeError):
        controller.key()


def test_watchdog_started_on_key_cancelled_on_unkey() -> None:
    port = FakePttPort()
    timers = TimerFactory()
    controller = _controller(port, timers=timers)
    controller.open()
    controller.key()
    assert len(timers.timers) == 1 and timers.timers[0].started
    controller.unkey()
    assert timers.timers[0].cancelled


def test_watchdog_fires_and_force_unkeys_a_wedged_hold() -> None:
    port = FakePttPort()
    timers = TimerFactory()
    controller = _controller(port, timers=timers)
    controller.open()
    controller.key()
    port.ops.clear()
    timers.timers[0].callback()  # the playback thread wedged; the watchdog fires (G7)
    assert ("rts", False) in port.ops and ("dtr", False) in port.ops
    assert not controller.keyed


def test_signal_path_unkeys() -> None:
    port = FakePttPort()
    controller = _controller(port)
    controller.open()
    controller.key()
    port.ops.clear()
    controller.handle_signal(15, None)
    assert ("rts", False) in port.ops and ("dtr", False) in port.ops


def test_atexit_hook_registered_and_unkeys() -> None:
    port = FakePttPort()
    registered: list[Callable[[], None]] = []
    controller = _controller(port, atexit_reg=registered.append)
    controller.open()
    assert registered, "an atexit de-key hook must be registered at open"
    controller.key()
    port.ops.clear()
    registered[0]()
    assert ("rts", False) in port.ops


def test_close_unkeys_then_closes() -> None:
    port = FakePttPort()
    controller = _controller(port)
    controller.open()
    controller.key()
    port.ops.clear()
    controller.close()
    assert port.ops[-1] == "close"
    assert ("rts", False) in port.ops and ("dtr", False) in port.ops
    # Idempotent: a second close is a no-op, never a crash.
    controller.close()


@dataclass
class VanishedPttPort(FakePttPort):
    """The FTDI mid-USB-re-enumeration: every line operation raises Errno 6."""

    def set_dtr(self, value: bool) -> None:
        raise OSError(6, "Device not configured")

    def set_rts(self, value: bool) -> None:
        raise OSError(6, "Device not configured")

    def close(self) -> None:
        raise OSError(6, "Device not configured")


def test_close_survives_a_vanished_device() -> None:
    # Field 2026-08-29 01:26/01:28: Errno 6 in close() killed the daemon twice and
    # launchd throttling held the station deaf for six minutes. Close is a de-key
    # path — it must NEVER raise; a vanished device is already de-keyed.
    port = FakePttPort()
    controller = _controller(port)
    controller.open()
    controller.key()
    controller._port = VanishedPttPort()  # the device drops out from under us
    controller.close()  # must not raise
    assert controller.keyed is False
    assert controller._closed is True and controller._port is None
    # Still idempotent afterwards.
    controller.close()


def test_close_survives_port_close_raising() -> None:
    @dataclass
    class CloseRaisesPort(FakePttPort):
        def close(self) -> None:
            raise OSError(6, "Device not configured")

    port = CloseRaisesPort()
    controller = _controller(port)
    controller.open()
    controller.close()  # unkey succeeded, port.close() raises — still swallowed
    assert controller._closed is True and controller._port is None


def test_key_failure_still_raises() -> None:
    # The narrow scope of the guarantee: failing to KEY stays loud (the TX worker
    # catches it and records a blocked disposition) — only de-key/teardown is quiet.
    port = FakePttPort()
    controller = _controller(port)
    controller.open()
    controller._port = VanishedPttPort()
    with pytest.raises(OSError):
        controller.key()


def test_station_lock_second_holder_fails_loudly_before_open(tmp_path: Path) -> None:
    # G3/U1: the flock refusal happens before any device could be touched — including when
    # the serial half is absent (this test never opens a port at all).
    lock_path = tmp_path / "station.lock"
    first = acquire_station_lock(lock_path)
    assert first is not None
    with pytest.raises(RuntimeError, match="station lock"):
        acquire_station_lock(lock_path)

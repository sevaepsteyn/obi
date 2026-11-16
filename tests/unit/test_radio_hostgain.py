"""Unit (feature 092, US4): reading and restoring the host codec's gains.

These settings silently revert to power-on defaults when the USB interface re-enumerates —
observed 2026-08-21, input 0.504 (+12 dB, clips reference speech) and output 1.000. Nothing
in obi noticed, which is what this module fixes.

The CoreAudio calls are behind an injected backend — **no test calls CoreAudio** (contract
host-gain.md). The invariant that matters most is H1: a read that fails reports UNKNOWN,
never `calibrated`. A false "all good" is worse than no answer, because it ends the search.
"""

from __future__ import annotations

from obi.radio.hostgain import (
    POWER_ON_INPUT,
    POWER_ON_OUTPUT,
    Control,
    read,
    restore,
)


class FakeBackend:
    """Stands in for the CoreAudio HAL: a device name → {scope: {element: scalar}} map."""

    def __init__(
        self,
        controls: dict[str, dict[int, float]] | None = None,
        *,
        device: str | None = "USB PnP Sound Device",
        error: Exception | None = None,
    ) -> None:
        self.controls = controls if controls is not None else {}
        self.device = device
        self.error = error
        self.resolve_calls = 0
        self.writes: list[tuple[str, int, float]] = []

    def resolve(self, name: str) -> object | None:
        self.resolve_calls += 1
        if self.error is not None:
            raise self.error
        return 138 if self.device == name else None

    def read_scope(self, device: object, scope: str) -> dict[int, float]:
        if self.error is not None:
            raise self.error
        return dict(self.controls.get(scope, {}))

    def write_scope(self, device: object, scope: str, element: int, value: float) -> None:
        if self.error is not None:
            raise self.error
        self.controls.setdefault(scope, {})[element] = value
        self.writes.append((scope, element, value))


def _calibrated() -> dict[str, dict[int, float]]:
    """The reference state: input master at the floor, output at 0.90 on both channels."""

    return {"input": {0: 0.0}, "output": {1: 0.901, 2: 0.901}}


def _power_on() -> dict[str, dict[int, float]]:
    """What the codec comes back as after a re-enumeration."""

    return {"input": {0: POWER_ON_INPUT}, "output": {1: POWER_ON_OUTPUT, 2: POWER_ON_OUTPUT}}


# -- H1: total, and never a false pass ------------------------------------------------------


def test_absent_device_is_unknown_not_calibrated() -> None:
    report = read("USB PnP Sound Device", backend=FakeBackend(device=None))
    assert report.verdict == "unknown"
    assert report.input.verdict == "unknown"
    assert report.output.verdict == "unknown"
    assert report.detail


def test_backend_error_is_unknown_and_never_raises() -> None:
    report = read("USB PnP Sound Device", backend=FakeBackend(error=RuntimeError("HAL boom")))
    assert report.verdict == "unknown"
    assert "boom" in (report.detail or "")


def test_unsupported_platform_is_unknown() -> None:
    """CoreAudio is macOS-only; elsewhere this must degrade, not explode."""

    report = read("USB PnP Sound Device", backend=None, platform="linux")
    assert report.verdict == "unknown"
    assert "macos" in (report.detail or "").lower()


def test_scope_with_no_controls_is_unknown() -> None:
    backend = FakeBackend({"input": {}, "output": {1: 0.9}})
    report = read("USB PnP Sound Device", backend=backend)
    assert report.input.verdict == "unknown"
    assert report.output.verdict == "calibrated"
    assert report.verdict == "unknown"  # one unknown scope taints the whole verdict


# -- H2: resolved by name, every call -------------------------------------------------------


def test_device_is_resolved_on_every_call() -> None:
    backend = FakeBackend(_calibrated())
    read("USB PnP Sound Device", backend=backend)
    read("USB PnP Sound Device", backend=backend)
    assert backend.resolve_calls == 2, "a cached handle would miss a re-enumeration"


# -- H3: per-scope, per-element -------------------------------------------------------------


def test_output_without_an_element_zero_master_still_reads_both_channels() -> None:
    """This codec exposes no output master — only per-channel controls on elements 1 and 2."""

    report = read("USB PnP Sound Device", backend=FakeBackend(_calibrated()))
    assert [c.element for c in report.output.controls] == [1, 2]
    assert report.input.controls == [Control(element=0, scalar=0.0)]


def test_restore_writes_every_settable_element_in_a_scope() -> None:
    """H3/H6 — assuming a master on both scopes silently no-ops on output."""

    backend = FakeBackend(_power_on())
    restore("USB PnP Sound Device", input_gain=0.0, output_gain=0.90, backend=backend)
    assert sorted(backend.writes) == [
        ("input", 0, 0.0),
        ("output", 1, 0.90),
        ("output", 2, 0.90),
    ]


# -- H4: verdict against the configured reference -------------------------------------------


def test_within_tolerance_is_calibrated() -> None:
    report = read("USB PnP Sound Device", backend=FakeBackend(_calibrated()))
    assert report.verdict == "calibrated"
    assert report.input.verdict == "calibrated"
    assert report.output.verdict == "calibrated"


def test_power_on_defaults_are_drifted_on_both_scopes() -> None:
    """The exact failure this module exists to catch (observed 2026-08-21)."""

    report = read("USB PnP Sound Device", backend=FakeBackend(_power_on()))
    assert report.verdict == "drifted"
    assert report.input.verdict == "drifted"
    assert report.output.verdict == "drifted"
    # The consequence, not just the number — +12 dB clips reference speech.
    assert "clip" in report.input.consequence.lower()


def test_tolerance_is_configurable() -> None:
    controls = {"input": {0: 0.03}, "output": {1: 0.90, 2: 0.90}}
    tight = read("USB PnP Sound Device", backend=FakeBackend(controls), tolerance=0.01)
    loose = read("USB PnP Sound Device", backend=FakeBackend(controls), tolerance=0.05)
    assert tight.input.verdict == "drifted"
    assert loose.input.verdict == "calibrated"


def test_verdict_compares_scalars_not_reported_db() -> None:
    """The codec's dB reporting is nonlinear and inconsistent; scalars are the truth."""

    import inspect

    from obi.radio import hostgain

    source = inspect.getsource(hostgain)
    assert "kAudioDevicePropertyVolumeDecibels" not in source


# -- H6: restore reads back -----------------------------------------------------------------


def test_restore_reports_the_read_back_values_not_the_requested_ones() -> None:
    """A write that did not take must be visible, not assumed."""

    backend = FakeBackend(_power_on())

    class StubbornBackend(FakeBackend):
        def write_scope(self, device: object, scope: str, element: int, value: float) -> None:
            self.writes.append((scope, element, value))  # accepted, but does nothing

    stubborn = StubbornBackend(_power_on())
    good = restore("USB PnP Sound Device", input_gain=0.0, output_gain=0.90, backend=backend)
    bad = restore("USB PnP Sound Device", input_gain=0.0, output_gain=0.90, backend=stubborn)
    assert good.verdict == "calibrated"
    assert bad.verdict == "drifted", "a no-op write must not be reported as success"


def test_restore_on_an_absent_device_is_unknown_not_a_crash() -> None:
    report = restore(
        "USB PnP Sound Device", input_gain=0.0, output_gain=0.90, backend=FakeBackend(device=None)
    )
    assert report.verdict == "unknown"

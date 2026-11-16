"""The host codec's gain stages (feature 092) — obi's eyes on a setting it keeps losing.

`contracts/host-gain.md` in specs/092-radio-tx-loudness is the specification; the invariants
in one line each: every read is TOTAL and a failure reports ``unknown``, never ``calibrated``
(H1); the device is resolved by CoreAudio name on every call (H2); scopes are reported
per-element because this codec has no output master (H3); the verdict compares scalars
against the configured reference (H4); nothing here runs without an explicit operator
command (H5); a restore reads back what it achieved (H6); the handset knob is not
observable and is never inferred (H7).

**Why this exists.** The C-Media codec's input and output gains do not survive USB
re-enumeration. Observed 2026-08-21: the interface came back at a new USB location and both
stages had silently reverted to power-on defaults — input 0.504 (+12 dB, enough to clip
reference speech, when the calibrated value is the 0.0 floor) and output 1.000. Both
directions degraded at once and nothing in obi noticed. The operator found it by ear.

**Why obi never fixes it silently.** Writing host audio state obi did not create is the kind
of invisible action that makes a system untrustworthy — the operator would have no way to
know whether a level they set had been overridden. So this module reports, and only
``obi radio gain --restore`` writes.

CoreAudio is reached through ``ctypes`` rather than a new dependency: it is four property
calls, and pyobjc would be a heavy addition for that. macOS-only by nature; everywhere else
this degrades to ``unknown``.
"""

from __future__ import annotations

import struct
import sys
from dataclasses import dataclass, field
from typing import Protocol

#: What the codec comes back as after a re-enumeration — the signature of the failure.
POWER_ON_INPUT = 0.504
POWER_ON_OUTPUT = 1.000

#: CoreAudio four-character property selectors.
_DEVICES = "dev#"
_NAME = "lnam"
_VOLUME_SCALAR = "volm"
_SCOPE_GLOBAL = "glob"
_SCOPES = {"input": "inpt", "output": "outp"}

#: The elements worth probing. Element 0 is the "master"; this codec exposes one on input
#: and none on output, where the real controls are the two per-channel elements.
_ELEMENTS = (0, 1, 2)


@dataclass(frozen=True)
class Control:
    """One settable volume control: which element, and its current scalar."""

    element: int
    scalar: float


@dataclass(frozen=True)
class ScopeReport:
    """One direction's controls, and how they compare with the calibrated reference."""

    scope: str
    controls: list[Control]
    reference: float
    verdict: str  # calibrated | drifted | unknown
    consequence: str = ""


@dataclass(frozen=True)
class GainReport:
    """Both directions plus an overall verdict. Never raises to build one (H1)."""

    device: str
    input: ScopeReport
    output: ScopeReport
    verdict: str  # calibrated | drifted | unknown
    detail: str | None = None
    #: The third leg of the calibration, which obi cannot measure (H7).
    operator_check: str = "handset A volume knob at 1/10"


class Backend(Protocol):
    """The CoreAudio seam. Injected in tests so no test touches the real HAL."""

    def resolve(self, name: str) -> object | None: ...  # pragma: no cover - protocol

    def read_scope(
        self, device: object, scope: str
    ) -> dict[int, float]: ...  # pragma: no cover - protocol

    def write_scope(
        self, device: object, scope: str, element: int, value: float
    ) -> None: ...  # pragma: no cover - protocol


def _fourcc(code: str) -> int:
    return int(struct.unpack(">I", code.encode())[0])


@dataclass
class CoreAudioBackend:
    """The real HAL, via ctypes. Resolves by NAME every call — indices renumber (H2)."""

    _lib: object = field(default=None, repr=False)
    _cf: object = field(default=None, repr=False)

    def _frameworks(self) -> tuple[object, object]:
        if self._lib is None or self._cf is None:
            import ctypes
            import ctypes.util

            self._lib = ctypes.CDLL(ctypes.util.find_library("CoreAudio"))
            self._cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
        return self._lib, self._cf

    @staticmethod
    def _address_type() -> object:
        import ctypes

        class Address(ctypes.Structure):
            _fields_ = [
                ("mSelector", ctypes.c_uint32),
                ("mScope", ctypes.c_uint32),
                ("mElement", ctypes.c_uint32),
            ]

        return Address

    def resolve(self, name: str) -> object | None:
        import ctypes

        lib, cf = self._frameworks()
        address = self._address_type()
        addr = address(_fourcc(_DEVICES), _fourcc(_SCOPE_GLOBAL), 0)  # type: ignore[operator]
        size = ctypes.c_uint32()
        if lib.AudioObjectGetPropertyDataSize(  # type: ignore[attr-defined]
            1, ctypes.byref(addr), 0, None, ctypes.byref(size)
        ):
            return None
        count = size.value // 4
        buffer = (ctypes.c_uint32 * count)()
        if lib.AudioObjectGetPropertyData(  # type: ignore[attr-defined]
            1, ctypes.byref(addr), 0, None, ctypes.byref(size), buffer
        ):
            return None
        for device_id in buffer:
            name_addr = address(_fourcc(_NAME), _fourcc(_SCOPE_GLOBAL), 0)  # type: ignore[operator]
            ref = ctypes.c_void_p()
            ref_size = ctypes.c_uint32(ctypes.sizeof(ref))
            if lib.AudioObjectGetPropertyData(  # type: ignore[attr-defined]
                device_id,
                ctypes.byref(name_addr),
                0,
                None,
                ctypes.byref(ref_size),
                ctypes.byref(ref),
            ):
                continue
            out = ctypes.create_string_buffer(256)
            cf.CFStringGetCString(ref, out, 256, 0x08000100)  # type: ignore[attr-defined]
            if out.value.decode(errors="replace") == name:
                return int(device_id)
        return None

    def read_scope(self, device: object, scope: str) -> dict[int, float]:
        import ctypes

        lib, _cf = self._frameworks()
        address = self._address_type()
        found: dict[int, float] = {}
        for element in _ELEMENTS:
            addr = address(  # type: ignore[operator]
                _fourcc(_VOLUME_SCALAR), _fourcc(_SCOPES[scope]), element
            )
            if not lib.AudioObjectHasProperty(device, ctypes.byref(addr)):  # type: ignore[attr-defined]
                continue
            value = ctypes.c_float()
            size = ctypes.c_uint32(ctypes.sizeof(value))
            if lib.AudioObjectGetPropertyData(  # type: ignore[attr-defined]
                device, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(value)
            ):
                continue
            found[element] = float(value.value)
        return found

    def write_scope(self, device: object, scope: str, element: int, value: float) -> None:
        import ctypes

        lib, _cf = self._frameworks()
        address = self._address_type()
        addr = address(_fourcc(_VOLUME_SCALAR), _fourcc(_SCOPES[scope]), element)  # type: ignore[operator]
        scalar = ctypes.c_float(value)
        lib.AudioObjectSetPropertyData(  # type: ignore[attr-defined]
            device, ctypes.byref(addr), 0, None, ctypes.sizeof(scalar), ctypes.byref(scalar)
        )


def _consequence(scope: str, controls: list[Control], reference: float) -> str:
    """Say what the drift COSTS, not just that a number moved."""

    if not controls:
        return ""
    worst = max(controls, key=lambda c: abs(c.scalar - reference))
    if scope == "input":
        if worst.scalar > reference:
            return (
                "hotter than the calibrated floor — clips reference speech "
                "(the PTT click already peaks at -0.2 dBFS)"
            )
        return "below the calibrated floor"
    if worst.scalar > reference:
        return "hotter than the calibrated TX drive"
    return "quieter than the calibrated TX drive — risks the radio's noise gate"


def _scope_report(
    scope: str, controls: dict[int, float], reference: float, tolerance: float
) -> ScopeReport:
    if not controls:
        return ScopeReport(scope=scope, controls=[], reference=reference, verdict="unknown")
    found = [Control(element=e, scalar=v) for e, v in sorted(controls.items())]
    drifted = any(abs(c.scalar - reference) > tolerance for c in found)
    return ScopeReport(
        scope=scope,
        controls=found,
        reference=reference,
        verdict="drifted" if drifted else "calibrated",
        consequence=_consequence(scope, found, reference) if drifted else "",
    )


def _combine(input_report: ScopeReport, output_report: ScopeReport) -> str:
    """One unknown scope taints the whole verdict — a half-read is not a pass (H1)."""

    verdicts = {input_report.verdict, output_report.verdict}
    if "unknown" in verdicts:
        return "unknown"
    return "drifted" if "drifted" in verdicts else "calibrated"


def _unknown(device: str, detail: str, input_gain: float, output_gain: float) -> GainReport:
    return GainReport(
        device=device,
        input=ScopeReport("input", [], input_gain, "unknown"),
        output=ScopeReport("output", [], output_gain, "unknown"),
        verdict="unknown",
        detail=detail,
    )


def read(
    device_name: str,
    *,
    input_gain: float = 0.0,
    output_gain: float = 0.90,
    tolerance: float = 0.02,
    backend: Backend | None = None,
    platform: str | None = None,
) -> GainReport:
    """Both host gains and their verdict against the reference. NEVER raises (H1)."""

    # The platform gate guards the DEFAULT backend only: an injected backend is the
    # caller's business (that is how tests run anywhere without a HAL).
    if backend is None and (platform or sys.platform) != "darwin":
        return _unknown(
            device_name,
            "host gain control is macOS-only (CoreAudio); nothing to read here",
            input_gain,
            output_gain,
        )
    try:
        hal = backend or CoreAudioBackend()
        device = hal.resolve(device_name)
        if device is None:
            return _unknown(
                device_name,
                f"audio device '{device_name}' not found — is the interface plugged in?",
                input_gain,
                output_gain,
            )
        input_report = _scope_report(
            "input", hal.read_scope(device, "input"), input_gain, tolerance
        )
        output_report = _scope_report(
            "output", hal.read_scope(device, "output"), output_gain, tolerance
        )
    except Exception as exc:  # a probe reports, it never crashes the caller
        return _unknown(
            device_name, str(exc) or exc.__class__.__name__, input_gain, output_gain
        )
    return GainReport(
        device=device_name,
        input=input_report,
        output=output_report,
        verdict=_combine(input_report, output_report),
    )


def restore(
    device_name: str,
    *,
    input_gain: float = 0.0,
    output_gain: float = 0.90,
    tolerance: float = 0.02,
    backend: Backend | None = None,
    platform: str | None = None,
) -> GainReport:
    """Set both scopes to the reference, then READ BACK and report what was achieved (H6).

    Only ever called from `obi radio gain --restore` — obi does not touch host audio state
    on its own (H5). The read-back matters: a write the HAL accepted but did not apply must
    show as still drifted, not as success.
    """

    if backend is None and (platform or sys.platform) != "darwin":
        return _unknown(
            device_name,
            "host gain control is macOS-only (CoreAudio); nothing to restore here",
            input_gain,
            output_gain,
        )
    hal = backend or CoreAudioBackend()
    try:
        device = hal.resolve(device_name)
        if device is None:
            return _unknown(
                device_name,
                f"audio device '{device_name}' not found — nothing was changed",
                input_gain,
                output_gain,
            )
        for scope, target in (("input", input_gain), ("output", output_gain)):
            # EVERY settable element, not just a master: this codec has no output master,
            # so a master-only write is a silent no-op on the transmit side (H3).
            for element in hal.read_scope(device, scope):
                hal.write_scope(device, scope, element, target)
    except Exception as exc:
        return _unknown(
            device_name, str(exc) or exc.__class__.__name__, input_gain, output_gain
        )
    return read(
        device_name,
        input_gain=input_gain,
        output_gain=output_gain,
        tolerance=tolerance,
        backend=hal,
        platform=platform,
    )

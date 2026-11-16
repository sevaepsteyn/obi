"""Resolution and presence-probing of the radio interface's two USB halves (feature 088).

The criticalrf interface presents as two independent USB endpoints: a C-Media audio codec
(RX in / TX out) and an FTDI FT232R UART whose modem-control lines key the transmitter.
Both are resolved **by identity at call time** — the audio device by CoreAudio name, the
serial node by FTDI serial number — never by a stored index: PortAudio renumbers audio
devices on every plug/unplug exactly as avfoundation does (FR-029).

``probe()`` is total (never raises) and reports each half separately, because split-brain
is a real state: with the audio half alone the service runs RX-only; with neither it idles
and retries. Actual opening of the devices lives in ``capture.py`` / ``ptt.py`` — this
module only answers "what is present, and where".
"""

from __future__ import annotations

import glob as _glob
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# Injectable seams: a query returning sounddevice-shaped device dicts, and fs helpers.
DeviceQuery = Callable[[], list[dict[str, Any]]]
PathExists = Callable[[str], bool]
PathGlob = Callable[[str], list[str]]


def _sounddevice_query() -> list[dict[str, Any]]:
    """Default audio-device query via sounddevice (lazy import — `radio` extra only)."""

    import sounddevice  # noqa: PLC0415 — deliberate lazy import, see pyproject extras

    devices = sounddevice.query_devices()
    return [dict(d) for d in devices]


def resolve_audio_device(name: str, *, kind: str, query: DeviceQuery | None = None) -> int | None:
    """Index of the audio device matching ``name`` with channels for ``kind`` (input/output).

    Exact name match wins; otherwise a UNIQUE substring match is accepted (a renamed hub
    port sometimes decorates the name). Ambiguity or absence ⇒ ``None`` — callers decide
    whether that is fatal (capture) or a degraded state (probe).
    """

    channel_key = "max_input_channels" if kind == "input" else "max_output_channels"
    devices = (query or _sounddevice_query)()
    candidates = [(index, d) for index, d in enumerate(devices) if int(d.get(channel_key, 0)) > 0]
    exact = [index for index, d in candidates if str(d.get("name", "")) == name]
    if len(exact) == 1:
        return exact[0]
    partial = [index for index, d in candidates if name in str(d.get("name", ""))]
    if len(partial) == 1:
        return partial[0]
    return None


def resolve_serial_port(
    ftdi_serial: str | None,
    override: str | None = None,
    *,
    exists: PathExists | None = None,
    glob: PathGlob | None = None,
) -> str | None:
    """The serial device node for the PTT half, or ``None`` when absent/ambiguous.

    An explicit ``[radio] serial_port`` override wins (if present on disk). Otherwise the
    known FTDI serial's node is preferred; a single ``cu.usbserial-*`` node is accepted as
    a swapped-cable fallback (the criticalrf common.sh behavior). Two nodes ⇒ ``None`` —
    guessing between serial adapters is how a wrong device gets keyed.
    """

    path_exists = exists or os.path.exists
    path_glob = glob or (lambda pattern: sorted(_glob.glob(pattern)))
    if override:
        return override if path_exists(override) else None
    exact = f"/dev/cu.usbserial-{ftdi_serial}" if ftdi_serial else None
    if exact is not None and path_exists(exact):
        return exact
    found = path_glob("/dev/cu.usbserial-*")
    if len(found) == 1:
        return found[0]
    return None


def claimed_port_candidates(
    ftdi_serial: str | None,
    override: str | None = None,
    *,
    glob: PathGlob | None = None,
) -> frozenset[str]:
    """Every serial device the radio station could claim — for OTHER subsystems to avoid.

    Deliberately NOT :func:`resolve_serial_port`. That function answers "which device do I
    open right now?" and so existence-checks every branch, returning ``None`` when the node
    is absent. This one answers "which devices must nobody else touch?", and the answer must
    hold *while the device is absent* — a USB node that re-enumerates between a check and an
    open would otherwise slip through the gap. So: names only, no ``path_exists`` call, union
    rather than a single winner (feature 121, contract G6).

    Why this exists at all: opening the FTDI half asserts DTR and RTS, both of which key the
    transmitter (see :mod:`obi.radio.ptt` — measured on air), and a subsystem that does not
    know it is holding a radio has no de-key step. Feature 121 found the mesh driver one
    unplugged RAK4631 away from doing exactly that.

    The glob branch mirrors :func:`resolve_serial_port`'s swapped-cable fallback: if that
    function would accept a lone ``cu.usbserial-*`` node as the station, this must exclude it.
    """

    path_glob = glob or (lambda pattern: sorted(_glob.glob(pattern)))
    candidates = {f"/dev/cu.usbserial-{ftdi_serial}"} if ftdi_serial else set()
    if override:
        candidates.add(override)
    candidates.update(path_glob("/dev/cu.usbserial-*"))
    return frozenset(candidates)


@dataclass(frozen=True)
class HalfReport:
    """Presence of one half of the interface, with an operator-facing detail line."""

    present: bool
    detail: str


@dataclass(frozen=True)
class DevicesReport:
    """Presence of both halves. Split-brain (one half only) is a real, reportable state."""

    audio: HalfReport
    serial: HalfReport

    @property
    def all_present(self) -> bool:
        return self.audio.present and self.serial.present


def probe(
    *,
    audio_name: str,
    ftdi_serial: str | None,
    serial_port: str | None = None,
    query: DeviceQuery | None = None,
    exists: PathExists | None = None,
    glob: PathGlob | None = None,
) -> DevicesReport:
    """Total presence probe of both halves — never raises, never opens either device."""

    try:
        in_index = resolve_audio_device(audio_name, kind="input", query=query)
        out_index = resolve_audio_device(audio_name, kind="output", query=query)
    except Exception as exc:  # sounddevice missing/broken is a report, not a crash
        detail = str(exc) or exc.__class__.__name__
        if isinstance(exc, ImportError | ModuleNotFoundError):
            detail = "sounddevice not installed — run: uv sync --extra radio"
        audio = HalfReport(present=False, detail=detail)
    else:
        if in_index is not None and out_index is not None:
            audio = HalfReport(
                present=True,
                detail=f"'{audio_name}' (input #{in_index}, output #{out_index})",
            )
        else:
            missing = "input" if in_index is None else "output"
            audio = HalfReport(
                present=False,
                detail=f"audio device '{audio_name}' not found ({missing} side) — "
                "is the interface plugged in? (suspect the hub chain first)",
            )
    node = resolve_serial_port(ftdi_serial, serial_port, exists=exists, glob=glob)
    if node is not None:
        serial = HalfReport(present=True, detail=node)
    else:
        serial = HalfReport(
            present=False,
            detail=f"no serial node for FTDI {ftdi_serial} "
            "(and no unique /dev/cu.usbserial-* fallback)",
        )
    return DevicesReport(audio=audio, serial=serial)

"""Unit (feature 088): device resolution for the radio interface's two USB halves.

Everything runs against injected fakes — no sounddevice import, no filesystem probing of
real /dev nodes (FR-036: tests never touch the hardware).
"""

from __future__ import annotations

from typing import Any

from obi.radio.devices import (
    claimed_port_candidates,
    probe,
    resolve_audio_device,
    resolve_serial_port,
)


def _codec(name: str, inputs: int = 1, outputs: int = 2) -> dict[str, Any]:
    return {"name": name, "max_input_channels": inputs, "max_output_channels": outputs}


_DEVICES = [
    _codec("MacBook Pro Microphone", inputs=1, outputs=0),
    _codec("MacBook Pro Speakers", inputs=0, outputs=2),
    _codec("USB PnP Sound Device"),
]


def test_audio_exact_match_by_kind() -> None:
    assert resolve_audio_device("USB PnP Sound Device", kind="input", query=lambda: _DEVICES) == 2
    assert resolve_audio_device("USB PnP Sound Device", kind="output", query=lambda: _DEVICES) == 2


def test_audio_kind_respects_channel_counts() -> None:
    # The speakers have no inputs; asking for the input side must not match them.
    index = resolve_audio_device("MacBook Pro Speakers", kind="input", query=lambda: _DEVICES)
    assert index is None


def test_audio_unique_substring_fallback() -> None:
    devices = [_codec("Hub: USB PnP Sound Device #3")]
    assert resolve_audio_device("USB PnP Sound Device", kind="input", query=lambda: devices) == 0


def test_audio_ambiguous_substring_is_none() -> None:
    devices = [_codec("USB PnP Sound Device A"), _codec("USB PnP Sound Device B")]
    assert resolve_audio_device("USB PnP Sound Device", kind="input", query=lambda: devices) is None


def test_audio_absent_is_none() -> None:
    assert resolve_audio_device("USB PnP Sound Device", kind="input", query=lambda: []) is None


def test_serial_prefers_known_ftdi_serial() -> None:
    node = "/dev/cu.usbserial-FBC3793547DF"
    assert (
        resolve_serial_port("FBC3793547DF", exists=lambda p: p == node, glob=lambda _: [])
        == node
    )


def test_serial_override_wins_when_present() -> None:
    assert (
        resolve_serial_port("FBC3793547DF", "/dev/cu.x", exists=lambda p: p == "/dev/cu.x")
        == "/dev/cu.x"
    )
    # A configured override that is not on disk is absent, not silently substituted.
    assert (
        resolve_serial_port(
            "FBC3793547DF",
            "/dev/cu.gone",
            exists=lambda p: p == "/dev/cu.usbserial-FBC3793547DF",
            glob=lambda _: [],
        )
        is None
    )


def test_serial_single_node_fallback() -> None:
    assert (
        resolve_serial_port("ZZ", exists=lambda _: False, glob=lambda _: ["/dev/cu.usbserial-X1"])
        == "/dev/cu.usbserial-X1"
    )


def test_serial_two_nodes_is_none_never_a_guess() -> None:
    nodes = ["/dev/cu.usbserial-X1", "/dev/cu.usbserial-X2"]
    assert resolve_serial_port("ZZ", exists=lambda _: False, glob=lambda _: nodes) is None


def test_probe_all_present() -> None:
    report = probe(
        audio_name="USB PnP Sound Device",
        ftdi_serial="FBC3793547DF",
        query=lambda: _DEVICES,
        exists=lambda p: p == "/dev/cu.usbserial-FBC3793547DF",
        glob=lambda _: [],
    )
    assert report.all_present
    assert "input #2" in report.audio.detail
    assert report.serial.detail == "/dev/cu.usbserial-FBC3793547DF"


def test_probe_split_brain_reports_halves_distinctly() -> None:
    report = probe(
        audio_name="USB PnP Sound Device",
        ftdi_serial="FBC3793547DF",
        query=lambda: _DEVICES,
        exists=lambda _: False,
        glob=lambda _: [],
    )
    assert report.audio.present and not report.serial.present
    assert not report.all_present
    assert "FBC3793547DF" in report.serial.detail


def test_probe_missing_sounddevice_is_actionable_not_a_crash() -> None:
    def boom() -> list[dict[str, Any]]:
        raise ModuleNotFoundError("No module named 'sounddevice'")

    report = probe(
        audio_name="USB PnP Sound Device",
        ftdi_serial="FBC3793547DF",
        query=boom,
        exists=lambda _: False,
        glob=lambda _: [],
    )
    assert not report.audio.present
    assert "uv sync --extra radio" in report.audio.detail


# --- 121: which devices must OTHER subsystems refuse to open? -----------------------------
#
# Contract: specs/121-mesh-hardening/contracts/mesh-port-safety.md G6. Deliberately distinct
# from resolve_serial_port: that answers "what do I open now" and existence-checks every
# branch; this answers "what must nobody else touch" and must hold while the device is absent.


def test_claimed_candidates_include_the_ftdi_path_when_it_does_not_exist() -> None:
    """G6: the whole point — the exclusion must be non-empty exactly when the node is gone.

    resolve_serial_port returns None here. An exclusion built from it would be empty at the
    precise moment the mesh driver goes looking for a port and finds only the transmitter.
    """

    assert (
        resolve_serial_port("FBC3793547DF", exists=lambda _p: False, glob=lambda _p: []) is None
    )
    claimed = claimed_port_candidates("FBC3793547DF", glob=lambda _p: [])
    assert "/dev/cu.usbserial-FBC3793547DF" in claimed


def test_claimed_candidates_include_the_override() -> None:
    claimed = claimed_port_candidates("FBC3793547DF", "/dev/cu.custom-ptt", glob=lambda _p: [])
    assert "/dev/cu.custom-ptt" in claimed
    # The identity-derived path stays claimed too: an override may be edited out later, and a
    # stale exclusion is harmless where a missing one is not.
    assert "/dev/cu.usbserial-FBC3793547DF" in claimed


def test_claimed_candidates_include_glob_matches() -> None:
    """Mirrors resolve_serial_port's swapped-cable fallback: if that would accept a lone
    cu.usbserial-* node as the station, this must exclude it."""

    claimed = claimed_port_candidates("FBC3793547DF", glob=lambda _p: ["/dev/cu.usbserial-OTHER"])
    assert claimed == {"/dev/cu.usbserial-FBC3793547DF", "/dev/cu.usbserial-OTHER"}


def test_claimed_candidates_is_a_frozenset() -> None:
    assert isinstance(claimed_port_candidates("FBC3793547DF", glob=lambda _p: []), frozenset)

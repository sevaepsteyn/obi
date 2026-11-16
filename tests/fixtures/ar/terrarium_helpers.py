"""Test-only terrarium tile ENCODER (feature 031) — exercises the production decoder.

Production ships only a decoder (``core/terrain.py``); tests build known tiles here so the
decoder is verified against every PNG scanline filter type (0–4) and against exact
elevation values, fully offline. Kept under fixtures (not tests/unit) so both unit and
integration suites can synthesize tiles.
"""

from __future__ import annotations

import struct
import zlib


def _chunk(chunk_type: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + chunk_type
        + payload
        + struct.pack(">I", zlib.crc32(chunk_type + payload) & 0xFFFFFFFF)
    )


def terrarium_rgb(elevation_m: float) -> tuple[int, int, int]:
    """The terrarium byte triple for an elevation: value = R·256 + G + B/256 − 32768."""

    value = round((elevation_m + 32768.0) * 256.0)
    return (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF


def encode_terrarium(
    elevations: list[list[float]], *, filter_type: int = 0, rgba: bool = False
) -> bytes:
    """A valid non-interlaced 8-bit truecolor PNG encoding the elevation grid.

    ``filter_type`` applies the SAME filter to every scanline (0–4) so tests can pin each
    defilter path; ``rgba`` adds an opaque alpha channel (terrarium tiles appear in both
    truecolor flavors in the wild).
    """

    height = len(elevations)
    width = len(elevations[0])
    channels = 4 if rgba else 3
    color_type = 6 if rgba else 2

    raw_rows: list[bytearray] = []
    for row in elevations:
        line = bytearray()
        for elevation in row:
            r, g, b = terrarium_rgb(elevation)
            line += bytes((r, g, b, 255)) if rgba else bytes((r, g, b))
        raw_rows.append(line)

    stride = width * channels
    filtered = bytearray()
    prev = bytearray(stride)
    for line in raw_rows:
        filtered.append(filter_type)
        out = bytearray(stride)
        for i in range(stride):
            left = line[i - channels] if i >= channels else 0
            up = prev[i]
            up_left = prev[i - channels] if i >= channels else 0
            if filter_type == 0:
                out[i] = line[i]
            elif filter_type == 1:
                out[i] = (line[i] - left) & 0xFF
            elif filter_type == 2:
                out[i] = (line[i] - up) & 0xFF
            elif filter_type == 3:
                out[i] = (line[i] - ((left + up) >> 1)) & 0xFF
            elif filter_type == 4:
                p = left + up - up_left
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - up_left)
                if pa <= pb and pa <= pc:
                    predictor = left
                elif pb <= pc:
                    predictor = up
                else:
                    predictor = up_left
                out[i] = (line[i] - predictor) & 0xFF
            else:
                raise ValueError(f"unsupported filter {filter_type}")
        filtered += out
        prev = line

    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(bytes(filtered)))
        + _chunk(b"IEND", b"")
    )


def flat_tile_bytes(elevation_m: float, *, size: int = 256, filter_type: int = 0) -> bytes:
    """A full-size constant-elevation tile — the workhorse for offline terrain tests."""

    return encode_terrarium([[elevation_m] * size] * size, filter_type=filter_type)

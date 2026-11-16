"""Unit tests: GNIS DomesticNames fetch/parse/cache (feature 031, research R5)."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from obi.core.gnis import GnisSource, parse_domestic_names

FIXTURE = Path(__file__).parents[1] / "fixtures" / "ar" / "gnis-sample.txt"


def fixture_zip(member: str = "Text/DomesticNames_NV.txt") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(member, FIXTURE.read_bytes())
    return buffer.getvalue()


# --- parsing ---------------------------------------------------------------------------------


def test_parse_keeps_only_well_formed_summits() -> None:
    summits = parse_domestic_names(fixture_zip())
    assert summits is not None
    assert [s.name for s in summits] == ["Old Razorback Mountain", "Elephant Mountain"]
    razorback = summits[0]
    assert (razorback.latitude, razorback.longitude) == (40.8971, -115.0029)


def test_parse_matches_header_names_case_insensitively() -> None:
    text = FIXTURE.read_text()
    upper = text.split("\n", 1)
    shouted = (upper[0].upper() + "\n" + upper[1]).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("names.txt", shouted)
    summits = parse_domestic_names(buffer.getvalue())
    assert summits is not None and len(summits) == 2


def test_parse_rejects_unusable_inputs() -> None:
    assert parse_domestic_names(b"not a zip") is None

    empty = io.BytesIO()
    with zipfile.ZipFile(empty, "w") as archive:
        archive.writestr("readme.pdf", b"no text member")
    assert parse_domestic_names(empty.getvalue()) is None

    wrong_layout = io.BytesIO()
    with zipfile.ZipFile(wrong_layout, "w") as archive:
        archive.writestr("names.txt", b"some|other|columns\n1|2|3\n")
    assert parse_domestic_names(wrong_layout.getvalue()) is None


# --- GnisSource cache behavior ----------------------------------------------------------------


def test_summits_fetch_once_then_serve_from_cache(tmp_path: Path) -> None:
    calls: list[str] = []

    def fetcher(state: str) -> bytes:
        calls.append(state)
        return fixture_zip()

    source = GnisSource(tmp_path, fetcher=fetcher)
    first = source.summits("nv")  # lowercase input normalizes
    assert first is not None and len(first) == 2
    assert calls == ["NV"]
    assert (tmp_path / "gnis-summits-NV.json").is_file()

    offline = GnisSource(tmp_path, fetcher=lambda state: None)
    cached = offline.summits("NV")
    assert cached is not None
    assert [s.name for s in cached] == [s.name for s in first]


def test_unreachable_and_uncached_is_none(tmp_path: Path) -> None:
    assert GnisSource(tmp_path, fetcher=lambda state: None).summits("NV") is None


def test_unreadable_cache_refetches(tmp_path: Path) -> None:
    (tmp_path / "gnis-summits-NV.json").write_text("{corrupt")
    source = GnisSource(tmp_path, fetcher=lambda state: fixture_zip())
    summits = source.summits("NV")
    assert summits is not None and len(summits) == 2
    assert json.loads((tmp_path / "gnis-summits-NV.json").read_text())  # cache repaired


def test_unusable_download_is_none_and_not_cached(tmp_path: Path) -> None:
    source = GnisSource(tmp_path, fetcher=lambda state: b"not a zip")
    assert source.summits("NV") is None
    assert not (tmp_path / "gnis-summits-NV.json").exists()


def test_zip_is_not_retained(tmp_path: Path) -> None:
    source = GnisSource(tmp_path, fetcher=lambda state: fixture_zip())
    assert source.summits("NV") is not None
    leftovers = [p for p in tmp_path.rglob("*") if p.suffix in {".zip", ".txt"}]
    assert leftovers == []

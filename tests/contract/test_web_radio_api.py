"""Contract tests: GET /api/radio.json (feature 088, US4).

Real TLS server + a real store seeded with the production radiostore code. Every assertion
pins a sentence of specs/088-radio/contracts/radio-api.md (the logs-api tail idiom).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from datetime import UTC
from pathlib import Path

import httpx
import pytest

from obi.core import radiostore
from obi.core.config import RadioSettings
from obi.core.radiostore import OutboundRecord, TransmissionRecord
from obi.web.server import create_server


def _seed(db_path: Path) -> None:
    import time

    conn = radiostore.connect(db_path)
    now_us = time.time_ns() // 1000 - 3_600_000_000  # an hour ago; entries step forward
    rows = [
        ("rx", "yoyo this is nono", "ok", None),
        ("rx", "obi obi this is nono", "ok", "nono"),
        ("self", "our own echo", "ok", None),
        ("rx", "", "unintelligible", None),
        ("rx", "the mountain link is down", "ok", None),
    ]
    for index, (kind, transcript, gate, caller) in enumerate(rows):
        radiostore.record_transmission(
            conn,
            TransmissionRecord(
                started_us=now_us + index * 10_000_000,
                ended_us=now_us + index * 10_000_000 + 2_000_000,
                kind=kind,
                transcript=transcript,
                stt_gate=gate,
                caller=caller,
                trigger_fired=caller is not None,
                trigger_reason="name_at_start" if caller else None,
            ),
            tz=UTC,
        )
    call_id = radiostore.open_call(conn, opened_us=now_us, caller="nono", tier="safe")
    radiostore.record_outbound(
        conn,
        OutboundRecord(
            created_us=now_us + 50_000_000,
            call_id=call_id,
            kind="ack",
            text="go for obi",
            disposition="transmitted",
            render_ms=900,
            gate_log="{}",
            tx_started_us=now_us + 50_000_000,
            tx_ended_us=now_us + 51_000_000,
        ),
        tz=UTC,
    )
    conn.close()


def _serve(tmp_path: Path, settings: RadioSettings | None) -> Iterator[str]:
    export_path = tmp_path / "links.json"
    export_path.write_text(json.dumps({"generated_at": "2026-08-19T00:00:00+00:00"}))
    server = create_server(
        bind="127.0.0.1",
        port=0,
        export_path=export_path,
        state_dir=tmp_path / "state",
        radio_settings=settings,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"https://127.0.0.1:{server.server_address[1]}"
    try:
        yield base
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def served(tmp_path: Path) -> Iterator[str]:
    db_path = tmp_path / "radio.db"
    _seed(db_path)
    yield from _serve(tmp_path, RadioSettings(db_path=db_path, shadow_tx=True))


@pytest.fixture()
def storeless(tmp_path: Path) -> Iterator[str]:
    yield from _serve(tmp_path, RadioSettings(db_path=tmp_path / "never.db"))


def _get(base: str, query: str = "") -> httpx.Response:
    return httpx.get(f"{base}/api/radio.json{query}", verify=False, timeout=5.0)


def test_initial_page_ascending_with_cursor_and_stage(served: str) -> None:
    response = _get(served)
    assert response.status_code == 200
    payload = response.json()
    assert payload["store"] == "ok" and payload["mode"] == "initial"
    assert payload["stage"] == "shadow-tx"  # from the served config snapshot
    ids = [e["id"] for e in payload["entries"]]
    assert ids == sorted(ids)
    # `self` rows are excluded by default; the transmitted ack appears as kind='obi'.
    kinds = [e["kind"] for e in payload["entries"]]
    assert "self" not in kinds and "obi" in kinds
    obi_entry = next(e for e in payload["entries"] if e["kind"] == "obi")
    assert obi_entry["outbound_kind"] == "ack" and obi_entry["disposition"] == "transmitted"
    rx_entry = next(e for e in payload["entries"] if e["caller"] == "nono")
    assert rx_entry["trigger"] == "name_at_start"
    # Timestamps are the store's pre-localized strings, verbatim.
    assert all(len(e["timestamp"]) == 19 for e in payload["entries"])
    assert isinstance(payload["cursor"], int)


def test_tail_mode_after_id(served: str) -> None:
    first = _get(served).json()
    ids = [e["id"] for e in first["entries"]]
    midpoint = ids[1]
    tail = _get(served, f"?after={midpoint}").json()
    assert tail["mode"] == "tail"
    assert all(e["id"] > midpoint for e in tail["entries"])
    # Draining the tail: after the newest id, entries are empty and the cursor holds.
    drained = _get(served, f"?after={first['cursor']}").json()
    assert drained["entries"] == [] and drained["cursor"] == first["cursor"]


def test_kind_filter_and_self_debug_view(served: str) -> None:
    only_obi = _get(served, "?kind=obi").json()
    assert [e["kind"] for e in only_obi["entries"]] == ["obi"]
    only_self = _get(served, "?kind=self").json()
    assert [e["kind"] for e in only_self["entries"]] == ["self"]


def test_fts_query(served: str) -> None:
    hits = _get(served, "?q=mountain").json()
    assert len(hits["entries"]) == 1
    assert "mountain" in hits["entries"][0]["transcript"]


def test_bad_params_are_400(served: str) -> None:
    assert _get(served, "?window=soon").status_code == 400
    assert _get(served, "?after=abc").status_code == 400
    assert _get(served, "?kind=bogus").status_code == 400
    assert _get(served, "?limit=0").status_code == 400


def test_absent_store_is_a_200_state_with_hint(storeless: str) -> None:
    payload = _get(storeless).json()
    assert payload["store"] == "absent"
    assert payload["entries"] == [] and payload["cursor"] is None
    assert "obi radio start" in payload["hint"]


def test_unconfigured_radio_is_absent_too(tmp_path: Path) -> None:
    for base in _serve(tmp_path, None):
        payload = _get(base).json()
        assert payload["store"] == "absent"


def test_post_is_405_and_head_ok(served: str) -> None:
    response = httpx.post(f"{served}/api/radio.json", verify=False, timeout=5.0)
    assert response.status_code == 405
    head = httpx.head(f"{served}/api/radio.json", verify=False, timeout=5.0)
    assert head.status_code == 200 and head.content == b""


# --- clip replay (088 follow-on, constitution 1.13.0) --------------------------------------


def _seed_clip(tmp_path: Path, transcript: str, clip_rel: str | None) -> int:
    import time

    db = tmp_path / "radio.db"
    clips = tmp_path / "clips"
    conn = radiostore.connect(db)
    if clip_rel is not None and not clip_rel.startswith(".."):
        target = clips / clip_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"RIFFfakeWAVEdata")
    now_us = time.time_ns() // 1000
    row_id = radiostore.record_transmission(
        conn,
        TransmissionRecord(
            started_us=now_us,
            ended_us=now_us + 1_000_000,
            kind="rx",
            transcript=transcript,
            stt_gate="ok",
            clip_path=clip_rel,
        ),
        tz=UTC,
    )
    conn.close()
    return row_id


def test_clip_route_serves_the_row_audio(tmp_path: Path) -> None:
    row_id = _seed_clip(tmp_path, "hello", "2026/08/19/1.wav")
    settings = RadioSettings(db_path=tmp_path / "radio.db", clips_dir=tmp_path / "clips")
    for base in _serve(tmp_path, settings):
        listing = httpx.get(f"{base}/api/radio.json", verify=False, timeout=5.0).json()
        entry = next(e for e in listing["entries"] if e["id"] == row_id)
        assert entry["has_clip"] is True
        clip = httpx.get(f"{base}/api/radio-clip.wav?id={row_id}", verify=False, timeout=5.0)
        assert clip.status_code == 200
        assert clip.headers["content-type"] == "audio/wav"
        assert clip.content == b"RIFFfakeWAVEdata"
        missing = httpx.get(f"{base}/api/radio-clip.wav?id=99999", verify=False, timeout=5.0)
        assert missing.status_code == 404
        bad = httpx.get(f"{base}/api/radio-clip.wav?id=abc", verify=False, timeout=5.0)
        assert bad.status_code == 400
        post = httpx.post(f"{base}/api/radio-clip.wav", verify=False, timeout=5.0)
        assert post.status_code == 405


def test_clip_route_normalizes_a_quiet_clip_for_playback(tmp_path: Path) -> None:
    # Field 2026-08-20: obi's own clips are scaled down for the radio's MIC input and
    # replayed ~19 dB below the received traffic, which is recorded off the SPEAKER. The
    # served bytes are normalized; the stored file stays the faithful record.
    import io
    import math
    import wave

    import numpy as np

    quiet = io.BytesIO()
    with wave.open(quiet, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(22_050)
        writer.writeframes((np.full(11_025, 0.05) * 32767).astype("<i2").tobytes())
    stored = quiet.getvalue()

    row_id = _seed_clip(tmp_path, "quiet", "2026/08/20/quiet.wav")
    clip_file = tmp_path / "clips" / "2026/08/20/quiet.wav"
    clip_file.write_bytes(stored)

    settings = RadioSettings(db_path=tmp_path / "radio.db", clips_dir=tmp_path / "clips")
    for base in _serve(tmp_path, settings):
        served = httpx.get(f"{base}/api/radio-clip.wav?id={row_id}", verify=False, timeout=5.0)
        assert served.status_code == 200
        assert served.content != stored  # it was normalized, not passed through
        assert int(served.headers["content-length"]) == len(served.content)

        def _rms(raw: bytes) -> float:
            with wave.open(io.BytesIO(raw), "rb") as reader:
                pcm = np.frombuffer(reader.readframes(reader.getnframes()), dtype="<i2")
            value = float(np.sqrt(np.mean(np.square(pcm.astype(np.float64))))) / 32768.0
            return 20.0 * math.log10(value)

        assert _rms(served.content) > _rms(stored) + 10.0
        # and the file on disk is untouched — the record, not the playback
        assert clip_file.read_bytes() == stored


def test_clip_route_refuses_a_traversal_path_from_the_store(tmp_path: Path) -> None:
    # Even a tampered store row cannot read outside the clips dir.
    secret = tmp_path / "secret.txt"
    secret.write_text("credentials")
    row_id = _seed_clip(tmp_path, "evil", "../secret.txt")
    settings = RadioSettings(db_path=tmp_path / "radio.db", clips_dir=tmp_path / "clips")
    for base in _serve(tmp_path, settings):
        response = httpx.get(
            f"{base}/api/radio-clip.wav?id={row_id}", verify=False, timeout=5.0
        )
        assert response.status_code == 404
        assert b"credentials" not in response.content


def test_clipless_row_reports_has_clip_false(tmp_path: Path) -> None:
    row_id = _seed_clip(tmp_path, "no clip", None)
    settings = RadioSettings(db_path=tmp_path / "radio.db", clips_dir=tmp_path / "clips")
    for base in _serve(tmp_path, settings):
        listing = httpx.get(f"{base}/api/radio.json", verify=False, timeout=5.0).json()
        entry = next(e for e in listing["entries"] if e["id"] == row_id)
        assert entry["has_clip"] is False
        response = httpx.get(
            f"{base}/api/radio-clip.wav?id={row_id}", verify=False, timeout=5.0
        )
        assert response.status_code == 404


def test_speaker_attribution_fields_served(tmp_path: Path) -> None:
    # 088 follow-up: per-row passive speaker attribution rides the feed verbatim; rows
    # without attribution (pre-v2, carrier, obi's own) serve NULLs.
    import time

    db = tmp_path / "radio.db"
    conn = radiostore.connect(db)
    now_us = time.time_ns() // 1000 - 600_000_000
    radiostore.record_transmission(
        conn,
        TransmissionRecord(
            started_us=now_us,
            ended_us=now_us + 3_000_000,
            kind="rx",
            transcript="waterworks go for it",
            stt_gate="ok",
            speaker="it",
            speaker_confidence="high",
        ),
        tz=UTC,
    )
    radiostore.record_transmission(
        conn,
        TransmissionRecord(
            started_us=now_us + 10_000_000,
            ended_us=now_us + 12_000_000,
            kind="rx",
            transcript="unattributed",
            stt_gate="ok",
        ),
        tz=UTC,
    )
    conn.close()
    for base in _serve(tmp_path, RadioSettings(db_path=db)):
        entries = _get(base).json()["entries"]
        assert entries[0]["speaker"] == "it"
        assert entries[0]["speaker_confidence"] == "high"
        assert entries[1]["speaker"] is None and entries[1]["speaker_confidence"] is None

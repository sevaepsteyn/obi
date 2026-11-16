"""Integration: the unified web server over TLS + the `obi web serve` CLI (022 T024, 033)."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from obi.cli.main import app
from obi.web.server import STATIC_DIR, _allowlist, create_server, urls_for
from tests.integration._uisp import write_uisp_config

runner = CliRunner()

_EXTERNAL_URL = re.compile(r"https?://")


@pytest.fixture()
def served(tmp_path: Path) -> Iterator[str]:
    export = tmp_path / "links.json"
    export.write_text(json.dumps({"generated_at": "2026-07-01T00:00:00+00:00", "links": []}))
    server = create_server(
        bind="127.0.0.1", port=0, export_path=export, state_dir=tmp_path / "state"
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_serves_the_viewer_over_tls(served: str) -> None:
    index = httpx.get(f"{served}/", verify=False, timeout=5.0)
    assert index.status_code == 200
    # 037: the landing is the terminal workspace shell; widgets load as modules from it.
    assert "obi terminal" in index.text and "./shell.js" in index.text
    links = httpx.get(f"{served}/links.json", verify=False, timeout=5.0)
    assert links.status_code == 200


def test_zero_internet_guard_no_external_references(served: str) -> None:
    """FR-011: the served HTML/JS reference nothing outside the origin (playa-proof).

    The vendored three.js module is exempt (its MIT license header contains URLs in
    comments); what matters is that index.html and viewer.js — the code that could fetch —
    contain no absolute http(s) reference.
    """

    for path in ("/", "/viewer.js", "/shell.js", "/wm.js", "/theme.css"):
        body = httpx.get(f"{served}{path}", verify=False, timeout=5.0).text
        assert not _EXTERNAL_URL.search(body), f"{path} references an external URL"
    index = httpx.get(f"{served}/", verify=False, timeout=5.0).text
    assert "./vendor/three.module.min.js" in index  # three.js resolves locally


def test_missing_export_still_serves_the_page(tmp_path: Path) -> None:
    server = create_server(
        bind="127.0.0.1",
        port=0,
        export_path=tmp_path / "nonexistent.json",
        state_dir=tmp_path / "state",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"https://127.0.0.1:{server.server_address[1]}"
    try:
        assert httpx.get(f"{base}/", verify=False, timeout=5.0).status_code == 200
        assert httpx.get(f"{base}/links.json", verify=False, timeout=5.0).status_code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_urls_for_lists_candidates(tmp_path: Path) -> None:
    export = tmp_path / "links.json"
    export.write_text("{}")
    server = create_server(
        bind="127.0.0.1", port=0, export_path=export, state_dir=tmp_path / "state"
    )
    try:
        urls = urls_for(server, "127.0.0.1")
        assert urls == [f"https://127.0.0.1:{server.server_address[1]}/"]
    finally:
        server.server_close()


def test_every_allowlisted_asset_serves_on_a_voiceless_server(tmp_path: Path) -> None:
    """033 US1: one origin serves the full bundle — 2D module included — with voice off."""

    export = tmp_path / "links.json"
    export.write_text(json.dumps({"generated_at": "2026-07-01T00:00:00+00:00", "links": []}))
    export.with_name("terrain.json").write_text(json.dumps({"profiles": {}, "sites": {}}))
    # 055: the hillshade underlay raster is a conditional export-dir artifact like terrain.json;
    # provide a fixture so this "every allowlisted asset serves" sweep covers its new route.
    export.with_name("terrain-shade.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    # 098: the BRC city-grid artifact is a conditional export-dir artifact like terrain.json;
    # provide a fixture so this sweep covers its new route.
    export.with_name("brc.json").write_text(json.dumps({"year": 2026, "sites": {}}))
    server = create_server(
        bind="127.0.0.1", port=0, export_path=export, state_dir=tmp_path / "state"
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"https://127.0.0.1:{server.server_address[1]}"
    try:
        for path in _allowlist(STATIC_DIR, export):
            response = httpx.get(f"{base}{path}", verify=False, timeout=5.0)
            assert response.status_code == 200, path
    finally:
        server.shutdown()
        server.server_close()


def test_web_serve_cli_prints_urls_and_audits_web_serve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """033 US1/FR-010: startup prints the one https origin and audits action=web_serve."""

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    # 048: voice is on by default and the --voice flag is gone; disable it via config so this
    # test stays focused on the plain static serve path.
    config = write_uisp_config(tmp_path, extra="[web]\nvoice = false")
    export = tmp_path / "links.json"
    export.write_text(json.dumps({"generated_at": "2026-07-01T00:00:00+00:00", "links": []}))

    def immediate_interrupt(self: object, poll_interval: float = 0.5) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(
        "http.server.ThreadingHTTPServer.serve_forever", immediate_interrupt
    )
    result = runner.invoke(
        app,
        [
            "web",
            "serve",
            "--scope",
            str(config),
            "--export",
            str(export),
            "--port",
            "0",
            "--bind",
            "127.0.0.1",
        ],
    )
    assert result.exit_code == 0, result.output
    flat = result.output.replace("\n", "")  # rich wraps long lines
    assert "https://127.0.0.1:" in flat
    assert "obi web export" in flat  # freshness guidance names the new command
    records = [
        json.loads(line)
        for line in (tmp_path / "audit.jsonl").read_text().splitlines()
        if line
    ]
    serve_records = [r for r in records if r.get("action") == "web_serve"]
    assert serve_records, "expected a web_serve audit record"
    assert serve_records[-1]["params"]["voice"] is False


def test_web_serve_cli_voice_prints_the_disclosure_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """033 US3/FR-007 + 048: voice (on by default) mounts the ONE 024 engine via the shared
    builder and prints the cloud disclosure exactly once at startup."""

    from obi.cli import main as cli_main
    from obi.voice.stt import TranscriptionResult
    from obi.voice.web.turns import ConversationStore, VoiceWebApp
    from tests.fakes import FakeTranscriber, chat_config, chat_runtime, text_response

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    config = write_uisp_config(tmp_path)
    export = tmp_path / "links.json"
    export.write_text(json.dumps({"generated_at": "2026-07-01T00:00:00+00:00", "links": []}))

    built: list[str] = []

    def fake_builder(config_obj: object, console: object, err: object, *, label: str) -> object:
        built.append(label)
        fake_config = chat_config(tmp_path)
        return VoiceWebApp(
            runtime=chat_runtime(fake_config, responses=[text_response("up.")]),
            settings=fake_config.voice,
            transcriber=FakeTranscriber(
                results=[TranscriptionResult(text="hi", gate="ok", seconds=1.0, level=0.2)]
            ),
            render=lambda text: b"RIFF",
            store=ConversationStore(idle_minutes=30),
        )

    monkeypatch.setattr(cli_main, "_build_voice_web_app", fake_builder)

    def immediate_interrupt(self: object, poll_interval: float = 0.5) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("http.server.ThreadingHTTPServer.serve_forever", immediate_interrupt)
    result = runner.invoke(
        app,
        [
            "web",
            "serve",
            "--scope",
            str(config),
            "--export",
            str(export),
            "--port",
            "0",
            "--bind",
            "127.0.0.1",
        ],
    )
    assert result.exit_code == 0, result.output
    assert built == ["web voice"], "the shared builder mounts the one engine, once"
    flat = result.output.replace("\n", "")
    assert flat.count("cloud reasoning model") == 1  # the disclosure, exactly once
    records = [
        json.loads(line)
        for line in (tmp_path / "audit.jsonl").read_text().splitlines()
        if line
    ]
    serve_records = [r for r in records if r.get("action") == "web_serve"]
    assert serve_records and serve_records[-1]["params"]["voice"] is True


def test_viewer_js_contains_no_geodesy(served: str) -> None:
    """Principle III tripwire: the viewer must consume precomputed ENU, not compute geo.

    Earth radius, degree-radian conversion of lat/lon, or haversine in viewer.js would mean
    business logic leaked into the interface layer.
    """

    body = httpx.get(f"{served}/viewer.js", verify=False, timeout=5.0).text
    for forbidden in ("6371", "haversine", "latitude", "toRadians", "Math.PI / 180"):
        assert forbidden not in body, f"viewer.js contains geodesy marker: {forbidden}"

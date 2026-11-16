"""Integration (feature 127): `obi playa locate` / `obi playa fetch` through the CLI.

Locate drives the real engine over datasets written into the test's XDG-isolated
default playa dir; fetch monkeypatches the one network seam (playa.fetch_dataset —
itself pinned against httpx.MockTransport in test_playa.py). Nothing here touches
the internet.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from obi.cli.main import app
from obi.core.config import default_playa_state_path

runner = CliRunner()


def _config(tmp_path: Path, body: str = "") -> Path:
    path = tmp_path / "config.toml"
    playa = '[playa]\nbase_url = "https://api.example.net"\n\n'  # 137: no built-in API host
    path.write_text(f'[logs]\ndb_path = "{tmp_path}/logs.db"\n\n{playa}{body}')
    path.chmod(0o600)
    return path


def _seed_camps(*entries: dict) -> Path:
    path = default_playa_state_path() / "camp.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(list(entries)))
    return path


def test_locate_answers_a_camp_through_the_full_stack(tmp_path: Path) -> None:
    _seed_camps(
        {"name": "Snuggles", "location_string": "G & 9:15", "landmark": "Glowing Tree Tree"}
    )
    result = runner.invoke(app, ["playa", "locate", "snuggles", "--config", str(_config(tmp_path))])
    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    assert "Snuggles is at 9:15 & Golf." in flat
    assert "Look for: Glowing Tree Tree." in flat


def test_locate_without_data_points_at_fetch(tmp_path: Path) -> None:
    result = runner.invoke(app, ["playa", "locate", "anything", "--config", str(_config(tmp_path))])
    assert result.exit_code == 0
    assert "No playa data downloaded yet" in result.output


def test_fetch_downloads_both_datasets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from obi.core import playa

    calls: list[tuple[str, int, Path]] = []

    def fake_fetch(
        kind: str, *, year: int, key: str, out_path: Path, base_url: str, timeout: float = 30.0
    ) -> int:
        assert key == "sekrit-key"
        calls.append((kind, year, out_path))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("[]")
        return {"art": 351, "camp": 1184}[kind]

    monkeypatch.setattr(playa, "fetch_dataset", fake_fetch)
    key_file = tmp_path / "key.txt"
    key_file.write_text("sekrit-key\n")
    result = runner.invoke(
        app,
        [
            "playa",
            "fetch",
            "--key-file",
            str(key_file),
            "--year",
            "2026",
            "--config",
            str(_config(tmp_path)),
        ],
    )
    assert result.exit_code == 0, result.output
    assert [(kind, year) for kind, year, _p in calls] == [("art", 2026), ("camp", 2026)]
    # No [meshtastic] section ⇒ art goes to the shared playa default alongside camps.
    assert calls[0][2] == default_playa_state_path() / "art.json"
    assert calls[1][2] == default_playa_state_path() / "camp.json"
    flat = " ".join(result.output.split())
    assert "351 art entries" in flat and "1184 camp entries" in flat
    assert "sekrit" not in flat


def test_fetch_honors_the_meshtastic_art_path_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from obi.core import playa

    seen: dict[str, Any] = {}

    def fake_fetch(kind: str, **kwargs: Any) -> int:
        seen[kind] = kwargs["out_path"]
        return 1

    monkeypatch.setattr(playa, "fetch_dataset", fake_fetch)
    key_file = tmp_path / "key.txt"
    key_file.write_text("k")
    art_override = tmp_path / "elsewhere" / "art.json"
    body = f'[meshtastic]\nart_path = "{art_override}"\n'
    result = runner.invoke(
        app,
        ["playa", "fetch", "--key-file", str(key_file), "--config", str(_config(tmp_path, body))],
    )
    assert result.exit_code == 0, result.output
    assert seen["art"] == art_override
    assert seen["camp"] == default_playa_state_path() / "camp.json"


def test_fetch_rejected_key_exits_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from obi.core import playa

    def fake_fetch(kind: str, **kwargs: Any) -> int:
        request = httpx.Request("GET", "https://api.example/api/art")
        raise httpx.HTTPStatusError(
            "401", request=request, response=httpx.Response(401, request=request)
        )

    monkeypatch.setattr(playa, "fetch_dataset", fake_fetch)
    key_file = tmp_path / "key.txt"
    key_file.write_text("bad")
    result = runner.invoke(
        app, ["playa", "fetch", "--key-file", str(key_file), "--config", str(_config(tmp_path))]
    )
    assert result.exit_code == 1
    flat = " ".join(result.output.split())
    assert "HTTP 401" in flat and "rejected the key" in flat


def test_fetch_missing_key_file_exits_two(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "playa",
            "fetch",
            "--key-file",
            str(tmp_path / "absent.txt"),
            "--config",
            str(_config(tmp_path)),
        ],
    )
    assert result.exit_code == 2
    assert "cannot read key file" in result.output

"""T015: the discover command passes the config flag through to crawl (feature 004, US2)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import obi.core.crawl as crawl_mod
from obi.cli.main import app
from obi.core.inventory import Inventory


def _write_config(tmp_path: Path, *, enabled: bool | None) -> Path:
    flag = "" if enabled is None else f"resolve_link_local_via_ospf = {str(enabled).lower()}\n"
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[discovery]\n"
        'seeds = ["172.25.110.1"]\n'
        'ranges = ["172.25.110.0/24"]\n'
        f"{flag}"
        "[routeros]\n"
        'username = "x"\n'
        'password = "y"\n'
        "use_tls = false\n"
        "[inventory]\n"
        f'path = "{tmp_path / "inv.json"}"\n'
        "[audit]\n"
        f'path = "{tmp_path / "audit.jsonl"}"\n'
    )
    return cfg


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, enabled: bool | None) -> bool:
    captured: dict[str, object] = {}

    def fake_crawl(scope, driver, **kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return Inventory(scope=scope)

    monkeypatch.setattr(crawl_mod, "crawl", fake_crawl)
    monkeypatch.setattr("obi.cli.main.MikrotikDriver", lambda *a, **k: object())

    cfg = _write_config(tmp_path, enabled=enabled)
    result = CliRunner().invoke(app, ["routeros", "discover", "--scope", str(cfg)])
    assert result.exit_code == 0, result.output
    return bool(captured["resolve_link_local_via_ospf"])


def test_flag_true_is_passed_to_crawl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run(tmp_path, monkeypatch, enabled=True) is True


def test_flag_false_is_passed_to_crawl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run(tmp_path, monkeypatch, enabled=False) is False


def test_flag_absent_defaults_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run(tmp_path, monkeypatch, enabled=None) is False

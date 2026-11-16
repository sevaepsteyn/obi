"""Shared helpers for the UISP CLI integration tests (feature 003).

Not a test module (leading underscore) — provides a config writer and a transport patcher so
``obi status`` can be exercised against the recorded ``/devices`` fixture with no real controller.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from obi.core.drivers import uisp as uisp_mod
from tests.fakes import load_uisp_devices


def write_uisp_config(tmp_path: Path, *, extra: str = "", routeros: bool = False) -> Path:
    """Write a config.toml with a [uisp] section (optionally also [routeros]); perms 0600."""

    lines: list[str] = []
    if routeros:
        lines += [
            "[discovery]",
            'seeds = ["172.18.81.1"]',
            "[routeros]",
            'username = "admin"',
            'password = "fakesecret44028f"',
            "use_tls = true",
        ]
    lines += [
        "[uisp]",
        'base_url = "https://uisp.example.com"',
        'token = "UISP-TOKEN-supersecret"',
    ]
    if extra:
        lines.append(extra)
    lines += [
        "[audit]",
        f'path = "{tmp_path / "audit.jsonl"}"',
        "[inventory]",
        f'path = "{tmp_path / "inventory.json"}"',
    ]
    config = tmp_path / "config.toml"
    config.write_text("\n".join(lines) + "\n")
    config.chmod(0o600)
    return config


def patch_transport(
    monkeypatch: pytest.MonkeyPatch,
    *,
    devices: list[dict[str, Any]] | None = None,
    error: Exception | None = None,
) -> None:
    """Patch ``UispController._default_transport`` to serve fixtures or raise ``error``."""

    payload = load_uisp_devices() if devices is None else devices

    def _transport(self: object, timeout: float) -> list[dict[str, Any]]:
        if error is not None:
            raise error
        return list(payload)

    monkeypatch.setattr(uisp_mod.UispController, "_default_transport", _transport)
    monkeypatch.setenv("COLUMNS", "240")


def patch_geo_transport(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fixtures: dict[str, list[dict[str, Any]]] | None = None,
    error: Exception | None = None,
) -> None:
    """Patch ``UispController._default_geo_transport`` (feature 034: `obi uisp links`).

    Serves the untrimmed link-health captures per path, or raises ``error`` for every read.
    """

    from tests.fakes import load_uisp_links_fixture

    payload = fixtures or {
        "/nms/api/v2.1/data-links": load_uisp_links_fixture("data-links"),
        "/nms/api/v2.1/devices": load_uisp_links_fixture("devices"),
    }

    def _geo_transport(self: object, path: str, timeout: float) -> list[dict[str, Any]]:
        if error is not None:
            raise error
        return list(payload[path])

    monkeypatch.setattr(uisp_mod.UispController, "_default_geo_transport", _geo_transport)
    monkeypatch.setenv("COLUMNS", "240")

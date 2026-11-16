"""Shared helpers for the UniFi Network CLI integration tests (feature 011).

Not a test module (leading underscore) — a config writer and a transport patcher so ``obi status``
/ ``obi unifi status`` can be exercised against the recorded UniFi fixtures with no real console.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from obi.core.drivers import unifi_network as unifi_mod
from tests.fakes import load_unifi_controller, load_unifi_official


def write_unifi_config(
    tmp_path: Path,
    *,
    extra: str = "",
    api_key: bool = True,
    token: bool = True,
    routeros: bool = False,
) -> Path:
    """Write a config.toml with a [unifi-network] section (and optionally [routeros]); 0600."""

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
    lines += ["[unifi-network]", 'base_url = "https://console.unifi-hosting.example.com"']
    if api_key:
        lines.append('api_key = "UNIFI-KEY-supersecret"')
    if token:
        lines.append('token = "UNIFI-TOKEN-supersecret"')
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


def patch_transports(
    monkeypatch: pytest.MonkeyPatch,
    *,
    official: list[dict[str, Any]] | None = None,
    controller: list[dict[str, Any]] | None = None,
    official_error: Exception | None = None,
    controller_error: Exception | None = None,
) -> None:
    """Patch both ``UnifiNetworkController`` default transports to serve fixtures or raise."""

    off = load_unifi_official() if official is None else official
    ctrl = load_unifi_controller() if controller is None else controller

    def _official(self: object, timeout: float) -> list[dict[str, Any]]:
        if official_error is not None:
            raise official_error
        return list(off)

    def _controller(self: object, timeout: float) -> list[dict[str, Any]]:
        if controller_error is not None:
            raise controller_error
        return list(ctrl)

    monkeypatch.setattr(unifi_mod.UnifiNetworkController, "_default_official_transport", _official)
    monkeypatch.setattr(
        unifi_mod.UnifiNetworkController, "_default_controller_transport", _controller
    )
    monkeypatch.setenv("COLUMNS", "240")

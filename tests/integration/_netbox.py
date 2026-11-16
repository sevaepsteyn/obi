"""Shared helpers for the NetBox CLI integration tests (feature 007).

Not a test module (leading underscore) — provides a config writer and a transport patcher so
``obi netbox inventory`` can be exercised against the recorded device fixture with no real
NetBox instance.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from obi.core.drivers import netbox as netbox_mod
from tests.fakes import load_netbox_devices


def write_netbox_config(tmp_path: Path, *, extra: str = "", routeros: bool = False) -> Path:
    """Write a config.toml with a [netbox] section (optionally also [routeros]); perms 0600."""

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
        "[netbox]",
        'base_url = "https://netbox.example.com"',
        'token = "NETBOX-TOKEN-supersecret"',
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
    """Patch ``NetboxSource._default_transport`` to serve fixtures or raise ``error``."""

    payload = load_netbox_devices() if devices is None else devices

    def _transport(self: object, timeout: float) -> list[dict[str, Any]]:
        if error is not None:
            raise error
        return list(payload)

    monkeypatch.setattr(netbox_mod.NetboxSource, "_default_transport", _transport)
    monkeypatch.setenv("COLUMNS", "240")

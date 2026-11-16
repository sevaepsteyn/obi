"""Shared helpers for the Siklu CLI integration tests (feature 020).

Not a test module (leading underscore) — provides a config writer and a session-factory patcher so
``obi siklu status`` / ``obi status`` can be exercised against the recorded fixtures with no
real radio.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from obi.core.drivers import siklu as siklu_mod
from obi.core.drivers.siklu import SikluSession
from tests.fakes import RecordingSshSession, load_siklu_fixture

# The two real radios and their management addresses.
SHOWERS_ADDR = "172.18.89.130"
MOUNTAIN_ADDR = "172.18.83.224"


def write_siklu_config(
    tmp_path: Path,
    *,
    hosts: list[str] | None = None,
    extra: str = "",
    routeros: bool = False,
) -> Path:
    """Write a config.toml with a [siklu] section (optionally also [routeros]); perms 0600."""

    roster = hosts if hosts is not None else [SHOWERS_ADDR, MOUNTAIN_ADDR]
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
    quoted = ", ".join(f'"{h}"' for h in roster)
    lines += [
        "[siklu]",
        f"hosts = [{quoted}]",
        'username = "admin"',
        'password = "RadioSecret"',
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


def patch_sessions(
    monkeypatch: pytest.MonkeyPatch,
    *,
    failures: dict[str, Exception] | None = None,
) -> None:
    """Patch ``SikluDriver._default_session`` to serve the recorded tamarack/basswood fixtures.

    ``failures`` maps a host → an exception the factory should raise (auth/timeout/refused), so the
    unreachable/auth/timeout mix can be exercised. Any other host yields an empty (UNIDENTIFIED)
    session.
    """

    bundles = {
        SHOWERS_ADDR: load_siklu_fixture("tamarack-ptp-basswood-70"),
        MOUNTAIN_ADDR: load_siklu_fixture("basswood-ptp-tamarack-70"),
    }
    fails = failures or {}

    def _session(self: object, host: str, timeout: float) -> SikluSession:
        if host in fails:
            raise fails[host]
        return RecordingSshSession(bundles.get(host, {}))

    monkeypatch.setattr(siklu_mod.SikluDriver, "_default_session", _session)
    monkeypatch.setenv("COLUMNS", "240")

"""Regression (feature 134, analyze review): every configured credential is a known secret."""

from __future__ import annotations

from pathlib import Path

from obi.core.audit import _all_known_secrets
from obi.core.config import load_config

FIXTURE = Path(__file__).parent.parent / "fixtures" / "demo" / "fixture.toml"


def test_paging_and_siklu_passwords_are_known_secrets(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(FIXTURE.read_text().replace("@OUTPUT_DIR@", str(tmp_path / "out")))
    secrets = _all_known_secrets(load_config(path))
    assert "fakepagerpw" in secrets
    assert "fakesiklupw" in secrets
    assert "fakepass99" in secrets

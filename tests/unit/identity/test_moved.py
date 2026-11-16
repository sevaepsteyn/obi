"""The moved-value registry (feature 137, T045) stays in lock-step with the contract + config."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from obi.core.identity.guard import scan_text
from obi.core.identity.moved import MOVED, migration_rows
from obi.core.identity.tokens import build_tokens
from obi.core.toolset import build_operator_tools

ROOT = Path(__file__).resolve().parents[3]
CONTRACT = ROOT / "specs" / "137-identity-externalize" / "contracts" / "config-keys.md"


def _contract_keys() -> set[str]:
    keys: set[str] = set()
    if not CONTRACT.exists():  # specs/ is private-only: absent from a public clone
        return keys
    for line in CONTRACT.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| `["):
            continue
        cell = line.split("|")[1]
        section = re.search(r"`\[([^\]]+)\]", cell)
        names = re.findall(r"`([a-z0-9_]+)`", cell.split("]", 1)[1]) if section else []
        for name in names:
            keys.add(f"{section.group(1)}.{name}")
    return keys


def test_registry_matches_the_contract_table() -> None:
    contract = _contract_keys()
    if not contract:
        return
    registry = {m.key.replace(".*", "").replace("devices.description", "descriptions")
                for m in MOVED}  # fmt: skip
    # the contract writes `[wled.devices.<name>] description`; the registry keeps a wildcard
    contract = {k.replace("wled.devices.<name>.description", "wled.descriptions") for k in contract}
    missing = {k for k in contract if k not in registry and k != "web.brc"}
    assert missing == set()


def test_every_key_parses_in_a_config(tmp_path: Path) -> None:
    from obi.core.config import load_config

    body = (Path(__file__).parent / "moved_full.toml").read_text(encoding="utf-8")
    path = tmp_path / "config.toml"
    path.write_text(body.replace("@TMP@", str(tmp_path)))
    path.chmod(0o600)
    load_config(path)  # raises on any unknown/invalid moved key
    rows = migration_rows(tomllib.loads(path.read_text()))
    assert {r.value.key: r.state for r in rows if r.state != "SET"} == {}


def test_absent_and_unset_states() -> None:
    rows = {r.value.key: r.state for r in migration_rows({"dash": {}, "logs": {}})}
    assert rows["dash.ping_router"] == "UNSET"
    assert rows["weather.latitude"] == "SECTION ABSENT"
    assert rows["site.timezone"] == "SECTION ABSENT"


def test_off_features_hide_their_tools() -> None:
    requires = {t.name: t.requires for t in build_operator_tools()}
    assert requires["weather_forecast"] == ("weather",)
    assert requires["gate_traffic"] == ("traffic",)
    assert requires["playa_locate"] == ("brc",)


def test_descriptions_never_carry_a_value() -> None:
    tokens = build_tokens(denylist=None, mapping=None, config=None)
    for moved in MOVED:
        text = f"{moved.was} {moved.unset_behaviour} {moved.toml}"
        assert scan_text(text, moved.key, tokens) == [], moved.key

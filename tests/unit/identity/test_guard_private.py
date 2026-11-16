"""Checks driven by the private inputs (T017), with FICTIONAL stand-ins for real values."""

from __future__ import annotations

from pathlib import Path

from obi.core.identity.guard import scan_paths
from obi.core.identity.tokens import build_tokens

LINES = {
    "denylist": "# handled by quillon on shift",
    "place": "host = 'northfield-gw-01'",
    "address": "router = '10.99.4.7'",
    "coord": "pin = (45.0123, -100.0456)",
    "callsign": "callers = ['Zephyrine']",
    "event_term": "# out on the playa",
}


def _scan(tree, denylist: Path, mapping: Path, config: Path):
    root = tree({f"tests/{k}.py": v + "\n" for k, v in LINES.items()})
    tokens = build_tokens(denylist=denylist, mapping=mapping, config=config)
    return scan_paths(None, root, tokens, dictionary=frozenset({"the", "on", "out", "shift"}))


def test_private_tokens_are_fatal_with_locations(
    tree, fake_denylist: Path, fake_mapping: Path, fake_config: Path
) -> None:
    result = _scan(tree, fake_denylist, fake_mapping, fake_config)
    fatal_files = {f.file for f in result.fatal}
    for kind in ("denylist", "place", "address", "coord", "callsign"):
        assert f"tests/{kind}.py" in fatal_files, kind
    assert all(f.locator == "line 1" for f in result.fatal)
    assert "tests/event_term.py" not in fatal_files
    assert any(f.file == "tests/event_term.py" for f in result.warnings)


def test_report_and_json_never_carry_values(
    tree, fake_denylist: Path, fake_mapping: Path, fake_config: Path
) -> None:
    result = _scan(tree, fake_denylist, fake_mapping, fake_config)
    text = result.report() + result.to_json()
    for needle in ("quillon", "northfield", "10.99.4.7", "45.0123", "Zephyrine", "zephyrine"):
        assert needle not in text


def test_private_mode_header(tree, fake_denylist: Path, fake_mapping: Path, fake_config: Path):
    result = _scan(tree, fake_denylist, fake_mapping, fake_config)
    assert "denylist ✓" in result.header() and "mapping ✓" in result.header()
    assert "config ✓" in result.header()


def test_key_written_coordinates_split_across_lines(
    tree, fake_denylist: Path, fake_mapping: Path, fake_config: Path
) -> None:
    text = '{\n  "site": "x",\n  "latitude": 45.0123,\n  "elev": 3,\n  "longitude": -100.0456\n}\n'
    far = '{"latitude": 12.5, "longitude": 30.25}\n'
    root = tree(
        {"fx/near.json": text, "fx/far.json": far, "fx/near.py": "pin(lat=45.01, lon=-100.02)\n"}
    )
    tokens = build_tokens(denylist=fake_denylist, mapping=fake_mapping, config=fake_config)
    result = scan_paths(None, root, tokens, dictionary=frozenset())
    hits = sorted((f.file, f.locator) for f in result.fatal if f.category == "coord")
    assert hits == [("fx/near.json", "line 3"), ("fx/near.py", "line 1")]
    assert "45.0123" not in result.report()


def test_file_names_are_checked(
    tree, fake_denylist: Path, fake_mapping: Path, fake_config: Path
) -> None:
    root = tree({"tests/fixtures/northfield-gw-01-ospf.json": "{}\n"})
    tokens = build_tokens(denylist=fake_denylist, mapping=fake_mapping, config=fake_config)
    result = scan_paths(None, root, tokens, dictionary=frozenset())
    assert [(f.locator, f.category) for f in result.fatal] == [("path", "place")]


def test_delimited_coordinate_rows_are_fatal(
    tree, fake_denylist: Path, fake_mapping: Path, fake_config: Path
) -> None:
    root = tree(
        {
            "fx/near.txt": "id|name|lat|lon\n1|Peak|45.0123|-100.0456\n",
            "fx/far.txt": "1|Peak|12.5000|30.2500\n",
        }
    )
    tokens = build_tokens(denylist=fake_denylist, mapping=fake_mapping, config=fake_config)
    result = scan_paths(None, root, tokens, dictionary=frozenset({"peak", "id", "name"}))
    hits = sorted((f.file, f.locator, f.check) for f in result.fatal if f.category == "coord")
    assert hits == [("fx/near.txt", "line 2", "delimited")]

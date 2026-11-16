"""Contract (feature 121): the mesh driver never opens the radio station's serial device.

Full contract: specs/121-mesh-hardening/contracts/mesh-port-safety.md.

What makes this a *contract* test rather than a unit test: the property spans two features
that must not know about each other. The mesh driver has to refuse the radio station's device
without importing the radio package, which means the knowledge has to arrive as data from the
one place that sees both config sections. G7 pins that shape, because the obvious fix — import
radio into the driver and ask it — would work and would quietly couple a vendor adapter to the
RF station's lifecycle.

The behavioural guarantees (G1-G6, G8) are exercised in tests/unit/test_mesh_driver.py.
"""

from __future__ import annotations

import ast
from pathlib import Path

_DRIVER = Path(__file__).parent.parent.parent / "src" / "obi" / "core" / "drivers" / "meshtastic.py"


def _imported_modules(source: Path) -> set[str]:
    """Every module named by an import statement, at any nesting depth (lazy imports count)."""

    tree = ast.parse(source.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_mesh_driver_does_not_import_the_radio_package() -> None:
    """G7: the port exclusion is data, not a dependency.

    Walks the AST rather than checking sys.modules so a *lazy* import inside a function —
    which is how this module legitimately imports meshtastic and pyserial — would still be
    caught.
    """

    offenders = {name for name in _imported_modules(_DRIVER) if name.split(".")[0] == "obi"}
    radio_imports = {name for name in offenders if name.startswith("obi.radio")}
    assert not radio_imports, (
        f"the mesh driver imports {sorted(radio_imports)}. The radio-station port exclusion "
        "must reach it as data on MeshtasticSettings.excluded_ports, resolved in "
        "core/config.py where both sections are in scope — see contract G7."
    )


def test_mesh_driver_owns_its_vendor_id_allowlist() -> None:
    """G1's durability: the vendor ids live here, not in the vendor library.

    meshtastic.util.findPorts filters by the same ids but falls back to returning every
    non-blacklisted port when it matches nothing — which is how discovery reached the
    walkie-talkie's FTDI on 2026-08-31. Obi must not call it.
    """

    from obi.core.drivers import meshtastic as driver

    assert 0x239A in driver._MESH_VENDOR_IDS  # RAK4631 / Adafruit nRF52
    assert 0x0403 not in driver._MESH_VENDOR_IDS  # FTDI — the PTT half

    # The driver may *name* findPorts in prose (it explains why it does not use it), so match
    # on the import itself rather than on the text.
    imported_names = {
        alias.name
        for node in ast.walk(ast.parse(_DRIVER.read_text(encoding="utf-8")))
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "findPorts" not in imported_names, (
        "obi must not import meshtastic.util.findPorts: when its vendor filter matches "
        "nothing it returns every non-blacklisted serial port, which is how discovery "
        "reached the walkie-talkie's FTDI on 2026-08-31."
    )

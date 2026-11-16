"""Pins for contract O1 (feature 123): only the daemon opens the mesh serial port.

Two layers: the DEFAULTS pin (engines construct the daemon client when no driver is
injected) and the REPO-SCAN pin (no module outside the daemon/driver/tests constructs a
``MeshDriver`` — the class whose factory opens serial devices).
"""

from __future__ import annotations

import re
from pathlib import Path

from obi.core.config import MeshtasticSettings
from obi.core.mesh import mesh_access
from obi.core.meshclient import MeshDaemonClient

_SRC = Path(__file__).parent.parent.parent / "src" / "obi"

#: The only modules allowed to construct a MeshDriver (contracts/port-ownership.md O1).
_ALLOWED_DRIVER_CONSTRUCTORS = {
    _SRC / "core" / "meshdaemon.py",
    _SRC / "core" / "drivers" / "meshtastic.py",
}


def test_mesh_access_returns_the_daemon_client() -> None:
    access = mesh_access(MeshtasticSettings())
    assert isinstance(access, MeshDaemonClient)


def test_locator_default_driver_is_the_client(tmp_path: Path) -> None:
    from obi.core.audit import AuditTrail
    from obi.core.mesh import MeshLocator

    locator = MeshLocator(MeshtasticSettings(), AuditTrail(tmp_path / "audit.jsonl"))
    assert isinstance(locator._driver, MeshDaemonClient)


def test_no_stray_meshdriver_construction() -> None:
    offenders: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        if path in _ALLOWED_DRIVER_CONSTRUCTORS:
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"\bMeshDriver\(", text):
            offenders.append(str(path.relative_to(_SRC)))
    assert offenders == [], (
        "MeshDriver may only be constructed by the daemon and the driver module "
        f"(contract O1); found in: {offenders}"
    )


def test_no_stray_serial_open() -> None:
    """The raw factory (`_open_serial`) is referenced only where the port may be opened."""

    offenders: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        if path in _ALLOWED_DRIVER_CONSTRUCTORS:
            continue
        text = path.read_text(encoding="utf-8")
        if "_open_serial" in text:
            offenders.append(str(path.relative_to(_SRC)))
    assert offenders == []

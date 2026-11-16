"""FR-006 guard (feature 050): the declared controller collector tags must stay in sync with what
the ingest code actually assigns.

`source="syslog"` is resolved by EXCLUSION of :data:`logstore.CONTROLLER_COLLECTOR_TAGS`. If a
future ingest path tags rows with a NEW literal collector (e.g. ``collectors=["netbox"]``) without
declaring its family, those rows would be silently absorbed into "syslog". This test scans obi's
source for ``LogEvent(..., collectors=[<literals>])`` assignments and fails unless every literal tag
is declared. The device-syslog ingest tags rows with the collector HOSTNAME (a variable, not a
literal), so it is correctly excluded from the scan — a new syslog collector host needs no code
change (SC-004).

No device, network, or live-DB contact.
"""

from __future__ import annotations

import ast
from pathlib import Path

import obi
from obi.core import logstore

_SRC_ROOT = Path(obi.__file__).resolve().parent


def _iter_source_files() -> list[Path]:
    return sorted(p for p in _SRC_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def _logevent_collectors_lists() -> list[tuple[Path, ast.List]]:
    """Every ``collectors=[...]`` list passed to a ``LogEvent(...)`` call in obi's source."""

    found: list[tuple[Path, ast.List]] = []
    for path in _iter_source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute)
                else None
            )
            if name != "LogEvent":
                continue
            for kw in node.keywords:
                if kw.arg == "collectors" and isinstance(kw.value, ast.List):
                    found.append((path, kw.value))
    return found


def _literal_tags(list_node: ast.List) -> list[str] | None:
    """The string literals of a ``[...]`` list, or ``None`` if any element is not a str constant
    (e.g. the device-syslog ingest's ``[collector]`` — a variable)."""

    tags: list[str] = []
    for elt in list_node.elts:
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
            tags.append(elt.value)
        else:
            return None
    return tags


def test_every_literal_collector_tag_is_declared() -> None:
    """The core invariant: no ingest path writes a literal collector tag that the query layer would
    silently treat as syslog. Every literal ``LogEvent(collectors=[...])`` tag must be declared in
    ``CONTROLLER_COLLECTOR_TAGS``."""

    literal_tags: set[str] = set()
    for _path, list_node in _logevent_collectors_lists():
        tags = _literal_tags(list_node)
        if tags is not None:
            literal_tags.update(tags)

    undeclared = literal_tags - logstore.CONTROLLER_COLLECTOR_TAGS
    assert not undeclared, (
        f"ingest assigns literal collector tag(s) {sorted(undeclared)} not declared in "
        "logstore.CONTROLLER_COLLECTOR_TAGS — declare the family (or the query layer will treat "
        "those rows as 'syslog' by exclusion). See feature 050 / FR-006."
    )


def test_scan_actually_finds_the_known_controller_tags() -> None:
    """Self-check: the scan is not vacuously passing — it must see the two controller ingests
    (unifi in drivers/unifi_common.py, uisp in drivers/uisp.py)."""

    literal_tags: set[str] = set()
    for _path, list_node in _logevent_collectors_lists():
        tags = _literal_tags(list_node)
        if tags is not None:
            literal_tags.update(tags)

    assert {"unifi", "uisp"} <= literal_tags, literal_tags
    # And those exact literals are what the declared set contains today.
    assert sorted(logstore.CONTROLLER_COLLECTOR_TAGS) == ["uisp", "unifi"]


def test_device_syslog_ingest_uses_a_variable_collector_not_a_literal() -> None:
    """The syslog family is open-ended: device-syslog rows are tagged with the collector HOSTNAME
    (a variable), so a new mirror host is picked up by ``source=syslog`` with no code change and is
    never a declared 'controller' tag."""

    has_variable_collector = False
    for _path, list_node in _logevent_collectors_lists():
        if _literal_tags(list_node) is None and list_node.elts:
            # a non-empty list whose elements are not all str literals — the [collector] hostname.
            has_variable_collector = True
    assert has_variable_collector, (
        "expected at least one LogEvent(collectors=[<variable>]) — the device-syslog ingest that "
        "carries a collector hostname; without it the syslog family has no dynamic members."
    )

"""Unit (feature 028): the read-only path gate — the STATIC Principle-I guarantee.

`routeros_query` only ever issues `print`, so a command word is refused pre-flight, before any
device contact. These tests pin the normalization (leading slash, trailing `print`) and the
default-deny verb list.
"""

from __future__ import annotations

import pytest

from obi.core.drivers.mikrotik import (
    ReadOnlyPathError,
    assert_read_path,
    is_read_path,
    normalize_segments,
)

_READS = [
    "interface",
    "/interface",
    "interface/print",
    "/interface/print",
    "ip/route",
    "ip/firewall/filter",
    "interface/wireless/registration-table",
    "routing/ospf/neighbor",
    # feature 052: the OSPF topology read surface (all print-only; no allowlist change).
    "routing/ospf/lsa",
    "routing/ospf/instance",
    "routing/ospf/area",
    "routing/ospf/interface-template",
    "routing/ospf/interface",
    "interface/vlan",
    # 053: configured assignments — one-sided ghost peer resolution (print-only, unchanged).
    "ip/address",
    # feature 095: the dash cycle's netwatch table read (print-only; no allowlist change).
    "tool/netwatch",
]

_COMMANDS = [
    "system/reboot",
    "interface/set",
    "ip/address/add",
    "ip/address/remove",
    "tool/fetch",
    "file/remove",
    "system/script/run",
    "system/shutdown",
    "certificate/import",
    "interface/wireless/scan",
    # feature 052: a command verb appended to an OSPF read path is still refused.
    "routing/ospf/interface-template/set",
    "routing/ospf/instance/remove",
    "interface/vlan/add",
]


@pytest.mark.parametrize("path", _READS)
def test_read_paths_are_allowed(path: str) -> None:
    segments = normalize_segments(path)
    assert is_read_path(segments)
    assert_read_path(segments)  # does not raise


@pytest.mark.parametrize("path", _COMMANDS)
def test_command_paths_are_refused(path: str) -> None:
    segments = normalize_segments(path)
    assert not is_read_path(segments)
    with pytest.raises(ReadOnlyPathError):
        assert_read_path(segments)


def test_normalization_strips_slash_and_trailing_print() -> None:
    assert normalize_segments("/interface/print") == ("interface",)
    assert normalize_segments("  ip / route ") == ("ip", "route")
    assert normalize_segments("interface print") == ("interface",)
    # a trailing 'print' is dropped, but 'print' as the whole path is empty (refused)
    assert normalize_segments("print") == ()


def test_empty_path_is_refused() -> None:
    assert not is_read_path(())
    with pytest.raises(ReadOnlyPathError):
        assert_read_path(())

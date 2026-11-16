"""Unit (feature 096): the Linux probe's pure parsers over the recorded core-vms-1 fixture.

CPU is a two-sample /proc/stat delta (counter-wrap ⇒ None, never a fake number); meminfo
yields total/available; df keeps only real /dev/* filesystems (the fixture's efivarfs
pseudo-fs at 59% must NOT become a disk reading); hwmon maps each temp input to its chip
label, and its absence is a note-worthy state, not an error.
"""

from __future__ import annotations

import json
from pathlib import Path

from obi.core.drivers.linux_health import (
    parse_df,
    parse_hwmon,
    parse_meminfo,
    parse_proc_stat_delta,
)

_FIXTURE = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "linux_health" / "core-vms-1.json")
    .read_text()
)["commands"]
_STAT_CMD = "cat /proc/stat && sleep 2 && cat /proc/stat"
_HWMON_CMD = (
    "grep . /sys/class/hwmon/hwmon*/name /sys/class/hwmon/hwmon*/temp*_input "
    "2>/dev/null || true"
)


def test_cpu_delta_from_fixture_is_a_sane_percentage() -> None:
    util = parse_proc_stat_delta(_FIXTURE[_STAT_CMD])
    assert util is not None
    assert 0.0 <= util <= 100.0


def test_cpu_delta_math_is_busy_over_total() -> None:
    text = (
        "cpu  100 0 100 800 0 0 0 0 0 0\nother lines\n"
        "cpu  150 0 150 1300 0 0 0 0 0 0\n"
    )
    # deltas: busy (user+system) 100, total 600 → 16.666…%
    util = parse_proc_stat_delta(text)
    assert util is not None
    assert abs(util - 100 * 100 / 600) < 0.01


def test_cpu_counter_wrap_yields_none_never_a_number() -> None:
    text = "cpu  500 0 500 1000 0 0 0 0 0 0\ncpu  100 0 100 200 0 0 0 0 0 0\n"
    assert parse_proc_stat_delta(text) is None
    assert parse_proc_stat_delta("cpu  1 2 3 4 5 6 7 8 0 0\n") is None  # single sample
    assert parse_proc_stat_delta("") is None


def test_meminfo_total_and_available() -> None:
    mem = parse_meminfo(_FIXTURE["cat /proc/meminfo"])
    assert mem is not None
    total_kb, available_kb = mem
    assert total_kb == 32215988
    assert 0 < available_kb < total_kb
    assert parse_meminfo("garbage\n") is None


def test_df_keeps_only_real_devices_and_computes_decimal_percent() -> None:
    disks = dict(parse_df(_FIXTURE["df -P -k -l -x tmpfs -x devtmpfs"]))
    assert "/" in disks and "/home" in disks
    assert "/sys/firmware/efi/efivars" not in disks  # efivarfs is not a disk
    # root: 20852276 used / (20852276+52482508) ⇒ 28.4% ⇒ stored ×10
    assert disks["/"] == 284
    # long mapper device names parse fine
    assert disks["/boot"] == round(1000 * 415952 / (415952 + 567088))


def test_hwmon_labels_each_temp_with_its_chip() -> None:
    temps = parse_hwmon(_FIXTURE[_HWMON_CMD])
    by_instance = {t[0]: t for t in temps}
    assert by_instance["hwmon3/temp1"][1] == "coretemp"
    assert by_instance["hwmon3/temp1"][2] == 340  # 34000 milli-°C → deci-°C
    assert by_instance["hwmon0/temp1"][1] == "acpitz"
    assert len(temps) == 7  # every temp*_input in the fixture


def test_hwmon_absent_is_empty_not_error() -> None:
    assert parse_hwmon("") == []
    assert parse_hwmon("\n") == []

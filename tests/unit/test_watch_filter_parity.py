"""FR-005 gate: the SQL candidate query admits EXACTLY what the Python backstop admits.

``logstore.query_watch_candidates`` (filter-before-LIMIT, A1) must select the same rows as
``watch._filter_window`` over the same corpus and config, so pushing the exclusions into SQL never
changes what the judge sees — only where the LIMIT is applied. A divergence here would silently
widen or narrow the window, so this parity is the load-bearing test for A1 (research D1/D2).
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path

from obi.core import logstore
from obi.core import watch as watchmod
from tests.fakes import seed_logs, uisp_settings, watch_config, watch_inventory

NOW = datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC)

# (host, topics, message, collector) — a corpus touching every exclusion rule + edge cases.
_CORPUS = [
    ("core-1", "system,info,account", "user admin logged in", "core"),
    ("core-1", "dhcp,info", "assigned 172.25.110.5", "core"),
    ("core-1", "interface,warning", "ether1 link down", "core"),
    ("core-1", "system,error,critical", "no storage space", "core"),
    ("core-2", "system,info", "reboot", "core"),
    ("Random-Host", "system,info", "unmanaged noise", "core"),  # non-RouterOS, mixed case
    ("Alder", "unifi_device,info", "AP adopted", "unifi"),
    ("aspen", "uisp,log", "device offline", "uisp"),
    ("office-uisp", "uisp,log", "device online", "uisp"),
]


def _seed_corpus(cfg) -> None:  # type: ignore[no-untyped-def]
    rows_by_collector: dict[str, list] = {}
    for i, (host, topics, message, collector) in enumerate(_CORPUS):
        rows_by_collector.setdefault(collector, []).append(
            (host, NOW - timedelta(minutes=len(_CORPUS) - i), topics, message)
        )
    for collector, rows in rows_by_collector.items():
        seed_logs(cfg.logs.mirror_root, cfg.logs.db_path, rows, collector=collector)


def _assert_parity(cfg) -> None:  # type: ignore[no-untyped-def]
    conn = logstore.connect_readonly(cfg.logs.db_path)
    try:
        all_raw = logstore.query_since(conn, after_id=0, limit=10_000)
        upto = max(event_id for event_id, _ in all_raw)
        sql = logstore.query_watch_candidates(
            conn, after_id=0, upto_id=upto, limit=10_000,
            exclusions=watchmod._window_exclusions(cfg),
        )
    finally:
        conn.close()
    python = watchmod._filter_window(all_raw, cfg)
    assert [i for i, _ in sql] == [i for i, _ in python]


def test_parity_no_exclusions(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path)  # routeros_only=False, no ignores
    _seed_corpus(cfg)
    _assert_parity(cfg)


def test_parity_ignore_topics(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path, ignore_topics=["dhcp,info", "system,info,account"])
    _seed_corpus(cfg)
    _assert_parity(cfg)


def test_parity_ignore_collectors(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path, ignore_collectors=["unifi"])
    _seed_corpus(cfg)
    _assert_parity(cfg)


def test_parity_routeros_only_gate(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path, routeros_only=True)
    watch_inventory(cfg.inventory_path, ["core-1", "core-2"])
    _seed_corpus(cfg)
    _assert_parity(cfg)


def test_parity_routeros_only_empty_inventory(tmp_path: Path) -> None:
    # Inventory missing ⇒ _routeros_hosts() is an empty set ⇒ the gate admits only unifi/uisp.
    cfg = watch_config(tmp_path, routeros_only=True)
    _seed_corpus(cfg)
    _assert_parity(cfg)


def test_parity_uisp_ignore_names(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path)
    cfg = dataclasses.replace(cfg, uisp=uisp_settings(ignore_names=["aspen"]))
    _seed_corpus(cfg)
    _assert_parity(cfg)


def test_parity_combined(tmp_path: Path) -> None:
    cfg = watch_config(
        tmp_path, routeros_only=True, ignore_topics=["dhcp,info"], ignore_collectors=["unifi"]
    )
    cfg = dataclasses.replace(cfg, uisp=uisp_settings(ignore_names=["aspen"]))
    watch_inventory(cfg.inventory_path, ["core-1", "core-2"])
    _seed_corpus(cfg)
    _assert_parity(cfg)


def test_parity_135_maintenance(tmp_path: Path) -> None:
    from obi.core.config import MaintenanceSettings

    cfg = watch_config(tmp_path, routeros_only=True)
    watch_inventory(cfg.inventory_path, ["core-1", "core-2"])
    cfg.maintenance = MaintenanceSettings(
        sites=("core", "random"), devices=("alder",), uisp_sites=("office-uisp",)
    )
    _seed_corpus(cfg)
    _assert_parity(cfg)


def test_parity_135_maintenance_token_edges(tmp_path: Path) -> None:
    from obi.core.config import MaintenanceSettings

    cfg = watch_config(tmp_path)
    cfg.maintenance = MaintenanceSettings(sites=("host", "uisp", "1"))  # middle/end/tail tokens
    _seed_corpus(cfg)
    _assert_parity(cfg)

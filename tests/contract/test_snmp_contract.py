"""T008: the SNMP adapter satisfies MetricsSource, the report matches its schema, profiles are
well-formed (feature 012)."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema

from obi.core.drivers.base import MetricsSource
from obi.core.drivers.snmp import SnmpMetricsSource
from obi.core.drivers.snmp_profiles import PROFILES, get_profile
from obi.core.models import MetricKind, SnmpOutcome
from obi.core.serialize import metric_report_to_dict
from tests.fakes import snmp_settings, snmp_source, system_agent

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "tests" / "schemas" / "snmp-report.schema.json"
)


def test_adapter_satisfies_metrics_source_protocol() -> None:
    source = SnmpMetricsSource(snmp_settings())
    assert isinstance(source, MetricsSource)
    assert source.name == "snmp"


def test_metric_report_conforms_to_schema() -> None:
    from obi.core.models import MetricTarget

    source = snmp_source({"172.18.84.1": system_agent()})
    report = source.fetch_metrics(
        MetricTarget(address="172.18.84.1", identity="core-gw-1", source="mikrotik-roster"),
        profile=get_profile("system"),
        timeout=1.0,
    )
    assert report.outcome is SnmpOutcome.REACHABLE
    payload = metric_report_to_dict(report)
    jsonschema.validate(instance=payload, schema=json.loads(_SCHEMA_PATH.read_text()))


def test_profiles_are_well_formed() -> None:
    assert set(PROFILES) == {"system", "interfaces", "health", "optics"}  # 096 adds two
    for profile in PROFILES.values():
        for definition in profile.scalars:
            assert definition.oid and all(part.isdigit() for part in definition.oid.split("."))
            assert isinstance(definition.kind, MetricKind)
        for column in profile.table:
            assert column.oid and all(part.isdigit() for part in column.oid.split("."))
            assert isinstance(column.kind, MetricKind)

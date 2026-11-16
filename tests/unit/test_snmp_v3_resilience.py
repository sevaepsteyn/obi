"""T023 (US3): v3 credential resolution, per-address overrides, and distinct failure outcomes
(feature 012)."""

from __future__ import annotations

from obi.core.config import Secret, SnmpOverride
from obi.core.drivers.snmp import _resolve_auth
from obi.core.drivers.snmp_profiles import get_profile
from obi.core.models import MetricTarget, SnmpOutcome
from tests.fakes import FakeSnmpAgent, snmp_settings, snmp_source

SYSTEM = get_profile("system")


def test_v3_credentials_resolve() -> None:
    settings = snmp_settings(
        version="v3",
        community=None,
        v3_username="obi-ro",
        v3_auth_key=Secret("authpass"),
        v3_priv_key=Secret("privpass"),
    )
    auth = _resolve_auth(settings, MetricTarget(address="172.18.84.1"))

    assert auth.version == "v3"
    assert auth.username == "obi-ro"
    assert auth.auth_key is not None and auth.auth_key.reveal() == "authpass"
    assert auth.priv_key is not None and auth.priv_key.reveal() == "privpass"


def test_per_address_override_applied() -> None:
    settings = snmp_settings(
        community=Secret("default-community"),
        overrides={"172.18.84.5": SnmpOverride(community=Secret("site-specific"), version="v2c")},
    )
    overridden = _resolve_auth(settings, MetricTarget(address="172.18.84.5"))
    default = _resolve_auth(settings, MetricTarget(address="172.18.84.9"))

    assert overridden.community is not None and overridden.community.reveal() == "site-specific"
    assert default.community is not None and default.community.reveal() == "default-community"


def test_auth_failure_is_distinct_from_unreachable_and_timeout() -> None:
    target = MetricTarget(address="172.18.84.1", source="mikrotik-roster")
    for outcome in (SnmpOutcome.AUTH_FAILED, SnmpOutcome.UNREACHABLE, SnmpOutcome.TIMED_OUT):
        source = snmp_source({"172.18.84.1": FakeSnmpAgent(fail=outcome)})
        report = source.fetch_metrics(target, profile=SYSTEM, timeout=1.0)
        assert report.outcome is outcome  # each maps to its own distinct verdict

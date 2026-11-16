"""Unit tests for the crawl boundary predicate (T004 → T005, FR-004)."""

from __future__ import annotations

from obi.core.models import DiscoveryScope
from obi.core.scope import scope_contains


def test_exact_seed_match() -> None:
    scope = DiscoveryScope(seeds=["172.18.81.1", "border-gw.local"])
    assert scope_contains(scope, "172.18.81.1")
    assert scope_contains(scope, "border-gw.local")  # hostnames match verbatim
    assert not scope_contains(scope, "172.18.81.2")


def test_cidr_containment() -> None:
    scope = DiscoveryScope(ranges=["172.18.0.0/16"])
    assert scope_contains(scope, "172.18.81.1")
    assert scope_contains(scope, "172.18.174.99")
    assert not scope_contains(scope, "10.27.0.1")  # outside the range
    assert not scope_contains(scope, "172.21.165.1")


def test_bare_host_range() -> None:
    scope = DiscoveryScope(ranges=["172.18.81.1/32"])
    assert scope_contains(scope, "172.18.81.1")
    assert not scope_contains(scope, "172.18.81.2")


def test_invalid_range_is_ignored() -> None:
    scope = DiscoveryScope(ranges=["not-a-cidr", "172.18.0.0/16"])
    assert scope_contains(scope, "172.18.81.1")  # the valid range still matches
    assert not scope_contains(scope, "10.99.0.1")


def test_non_ip_address_not_in_range() -> None:
    scope = DiscoveryScope(ranges=["172.18.0.0/16"])
    assert not scope_contains(scope, "some-hostname")  # not a seed, not an IP


def test_ipv4_address_does_not_match_ipv6_range() -> None:
    scope = DiscoveryScope(ranges=["fe80::/10"])
    assert not scope_contains(scope, "172.18.81.1")
    assert scope_contains(scope, "fe80::1")

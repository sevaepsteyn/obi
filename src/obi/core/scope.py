"""Scope membership: the crawl's containment boundary (feature 002, FR-004).

The topology crawl follows a neighbor only if its address lies within the operator-defined
discovery scope. This predicate IS the boundary — there is no separate allow-list and no
hop-count limit (resolved in the spec's Clarifications). The operator widens the crawl's reach
by widening the configured scope.
"""

from __future__ import annotations

import ipaddress

from obi.core.models import DiscoveryScope


def scope_contains(scope: DiscoveryScope, address: str) -> bool:
    """Return ``True`` if ``address`` is within ``scope`` (an exact seed, or inside any range).

    Seeds are matched verbatim (they may be hostnames). Ranges are CIDR-matched against the
    parsed IP address; a non-IP address that is not an exact seed is out of scope. Invalid
    ranges in the scope are ignored (consistent with discovery's tolerance).
    """

    if address in scope.seeds:
        return True
    try:
        addr = ipaddress.ip_address(address)
    except ValueError:
        return False
    for cidr in scope.ranges:
        try:
            network = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        if addr.version == network.version and addr in network:
            return True
    return False

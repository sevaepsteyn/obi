"""Scope-bounded, read-only device discovery (FR-010, FR-013, FR-014).

Expands the operator-defined :class:`DiscoveryScope` into candidate addresses, probes each
via the vendor driver (read-only), and returns the probe results that belong in the report.
Discovery is confined to the configured scope and never probes beyond it.
"""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed

from obi.core.drivers.base import ProbeOutcome, ProbeResult, VendorDriver
from obi.core.models import DiscoveryScope

logger = logging.getLogger("obi")

# Safety cap on how many addresses a single run will expand from CIDR ranges, so a wide
# range (e.g. a typo'd /8) cannot turn a bounded run into an unbounded scan.
MAX_RANGE_HOSTS = 4096

# For addresses that came from a CIDR range (not an explicit seed), only keep those where
# something is actually listening on the management port — otherwise a /24 would list 250+
# dead IPs. Explicit seeds are always reported (the operator named them).
_RANGE_KEEP = frozenset(
    {ProbeOutcome.IDENTIFIED, ProbeOutcome.UNIDENTIFIED, ProbeOutcome.AUTH_FAILED}
)


def expand_scope(scope: DiscoveryScope) -> list[tuple[str, bool]]:
    """Expand a scope into ``(address, is_seed)`` candidates, de-duplicated, capped.

    Seeds are preserved verbatim (they may be hostnames). Ranges are expanded to host
    addresses. Returns seeds first, then range hosts not already covered by a seed.
    """

    seen: set[str] = set()
    candidates: list[tuple[str, bool]] = []

    for seed in scope.seeds:
        if seed not in seen:
            seen.add(seed)
            candidates.append((seed, True))

    budget = MAX_RANGE_HOSTS
    for cidr in scope.ranges:
        try:
            network = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            logger.warning("skipping invalid range in scope", extra={"range": cidr})
            continue
        hosts: Iterable[ipaddress._BaseAddress]
        # A bare /32 (or /128) has no "hosts"; treat the network address itself as the host.
        hosts = network.hosts() if network.num_addresses > 1 else [network.network_address]
        for host in hosts:
            if budget <= 0:
                logger.warning(
                    "discovery range cap reached; remaining addresses skipped",
                    extra={"cap": MAX_RANGE_HOSTS},
                )
                break
            address = str(host)
            if address not in seen:
                seen.add(address)
                candidates.append((address, False))
                budget -= 1
        if budget <= 0:
            break

    return candidates


def _keep(result: ProbeResult, is_seed: bool) -> bool:
    if result.outcome is ProbeOutcome.NOT_MINE:
        return False
    if is_seed:
        return True
    return result.outcome in _RANGE_KEEP


def discover(
    scope: DiscoveryScope,
    driver: VendorDriver,
    *,
    timeout: float,
    concurrency: int,
) -> list[ProbeResult]:
    """Probe the scope concurrently (read-only) and return the in-report probe results."""

    candidates = expand_scope(scope)
    if not candidates:
        return []

    results: list[ProbeResult] = []
    max_workers = max(1, min(concurrency, len(candidates)))
    executor = ThreadPoolExecutor(max_workers=max_workers)
    try:
        future_map = {
            executor.submit(driver.probe, address, timeout=timeout): (address, is_seed)
            for address, is_seed in candidates
        }
        for future in as_completed(future_map):
            address, is_seed = future_map[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 - a probe failure is a result, not a crash
                result = ProbeResult(address, ProbeOutcome.UNREACHABLE, detail=str(exc))
            if _keep(result, is_seed):
                results.append(result)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    # Stable, address-sorted output for deterministic reports.
    results.sort(key=lambda r: r.address)
    return results

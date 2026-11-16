"""Prefix-preserving IP remap into non-real ranges (feature 134, research R3).

Private IPv4 space moves block-by-block: every real ``/16`` lands on its own fictional ``/16``
inside ``172.16.0.0/12``, the third octet is XORed with a seeded per-block mask (so the real
per-site octet numbering does not survive — and, unlike a shuffle, XOR maps every aligned
``/17``–``/24`` block onto an aligned block of the same size, so subnet membership is preserved at
every prefix length) and the host octet is kept (``.1`` gateways stay recognisable). CGNAT
(``100.64.0.0/10``) addresses — overlay networks scatter them across the whole ``/10`` — map host
by host into the reserved ``172.31.0.0/16``. Public IPv4 keeps its ``/24`` grouping inside the
documentation ``/24``s, then the RFC 2544 benchmarking ``/15``. IPv6 global/ULA moves into
``2001:db8::/32`` by ``/48``. Addresses that identify nothing (link-local, loopback, multicast,
netmask-shaped values, the target ranges themselves) pass through unchanged.

Assignments are persisted by the caller (:mod:`~obi.core.demo.mapping`) and only ever appended,
so a later snapshot reproduces every earlier mapping exactly.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
from typing import Any

from obi.core.demo import ProfileError

#: An IPv4 literal (optional CIDR) that is NOT part of a longer dotted-numeric run — SNMP OIDs
#: (``1.3.6.1.4.1.14988``) and 4+-part version strings never match (analyze C2).
_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
IPV4_LITERAL = re.compile(
    rf"(?<![\d.])(?<!\d\.){_OCTET}(?:\.{_OCTET}){{3}}(?:/(?:3[0-2]|[12]?\d))?(?!\.?\d)"
)

#: A conservative IPv6 candidate (validated by :mod:`ipaddress` before use): hex groups with at
#: least two colons, optional CIDR. MACs and clock times fail validation and are left alone.
IPV6_CANDIDATE = re.compile(
    r"(?<![0-9A-Fa-f:.])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?:/\d{1,3})?(?![0-9A-Fa-f:])"
)

_PRIVATE_POOLS = (ipaddress.IPv4Network("172.16.0.0/12"),)
#: CGNAT (RFC 6598) — real overlay/carrier addresses (e.g. Tailscale) are scattered across the
#: whole /10, so they are remapped HOST by host into one reserved fictional /16 instead of by block.
_CGNAT = ipaddress.IPv4Network("100.64.0.0/10")
_CGNAT_POOL = ipaddress.IPv4Network("172.31.0.0/16")
_DOC_V4 = (
    ipaddress.IPv4Network("192.0.2.0/24"),
    ipaddress.IPv4Network("198.51.100.0/24"),
    ipaddress.IPv4Network("203.0.113.0/24"),
)
_BENCH_V4 = ipaddress.IPv4Network("198.18.0.0/15")
_DOC_V6 = ipaddress.IPv6Network("2001:db8::/32")

#: Ranges whose addresses identify nothing — never remapped (and never a gate finding).
_UNCHANGED_V4 = tuple(
    ipaddress.IPv4Network(n)
    for n in (
        "0.0.0.0/8",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "224.0.0.0/4",
        "240.0.0.0/4",
    )
)

#: Every range a sanitized dataset may legitimately contain (the gate's IP allowlist).
ALLOWED_V4 = (*_PRIVATE_POOLS, *_DOC_V4, _BENCH_V4, *_UNCHANGED_V4)


def _is_netmask_shaped(addr: ipaddress.IPv4Address) -> bool:
    """Contiguous-ones masks (255.255.255.0) and their wildcard inverses (0.0.0.255)."""

    value = int(addr)
    inverse = value ^ 0xFFFFFFFF
    return (value | (value - 1)) & 0xFFFFFFFF == 0xFFFFFFFF or (inverse & (inverse + 1)) == 0


def is_unchanged_v4(addr: ipaddress.IPv4Address) -> bool:
    """True when ``addr`` passes through the remap verbatim (identifies nothing)."""

    return any(addr in net for net in _UNCHANGED_V4) or _is_netmask_shaped(addr)


def is_allowed_v4(addr: ipaddress.IPv4Address) -> bool:
    """True when ``addr`` may appear in a sanitized dataset (gate IP allowlist)."""

    return any(addr in net for net in ALLOWED_V4) or _is_netmask_shaped(addr)


def is_allowed_v6(addr: ipaddress.IPv6Address) -> bool:
    return (
        addr in _DOC_V6
        or addr.is_link_local
        or addr.is_loopback
        or addr.is_multicast
        or addr.is_unspecified
    )


def _source_is_private(addr: ipaddress.IPv4Address) -> bool:
    return any(
        addr in ipaddress.IPv4Network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    )


class IpRemapper:
    """Deterministic, append-only, prefix-preserving IP remap (research R3)."""

    def __init__(self, seed: bytes, state: dict[str, Any] | None = None) -> None:
        self._seed = seed
        state = state or {}
        self._block16: dict[str, str] = dict(state.get("block16", {}))
        self._octet3: dict[str, int] = {k: int(v) for k, v in state.get("octet3", {}).items()}
        self._public24: dict[str, str] = dict(state.get("public24", {}))
        self._v6_48: dict[str, str] = dict(state.get("v6_48", {}))
        self._cgnat: dict[str, str] = dict(state.get("cgnat", {}))

    # -- persistence --------------------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        return {
            "block16": dict(sorted(self._block16.items())),
            "octet3": dict(sorted(self._octet3.items())),
            "public24": dict(sorted(self._public24.items())),
            "v6_48": dict(sorted(self._v6_48.items())),
            "cgnat": dict(sorted(self._cgnat.items())),
        }

    def check_pools_disjoint(self, real_networks: list[str]) -> None:
        """Refuse a profile whose target pools overlap any real prefix (FR-005)."""

        for text in real_networks:
            try:
                net = ipaddress.ip_network(text, strict=False)
            except ValueError:
                continue
            if not isinstance(net, ipaddress.IPv4Network):
                continue
            for pool in (*_PRIVATE_POOLS, *_DOC_V4, _BENCH_V4):
                if net.overlaps(pool):
                    raise ProfileError(
                        "a real network overlaps a demo target pool; the remap would reuse a "
                        "real prefix (FR-005)"
                    )

    # -- core ---------------------------------------------------------------------------------

    def _digest(self, *parts: str) -> int:
        mac = hmac.new(self._seed, "\x1f".join(("ip", *parts)).encode(), hashlib.sha256)
        return int.from_bytes(mac.digest()[:8], "big")

    def _slot16(self, real_block: str) -> str:
        if real_block in self._block16:
            return self._block16[real_block]
        taken = set(self._block16.values())
        candidates: list[str] = []
        for pool in _PRIVATE_POOLS:
            candidates.extend(str(sub) for sub in pool.subnets(new_prefix=16) if sub != _CGNAT_POOL)
        start = self._digest("block16", real_block) % len(candidates)
        order = candidates[start:] + candidates[:start]
        for candidate in order:
            if candidate not in taken:
                self._block16[real_block] = candidate
                self._octet3[candidate] = 1 + self._digest("octet3", candidate) % 255  # never 0
                return candidate
        raise ProfileError("more real /16 blocks than the demo private pools can hold")

    def _map_private_v4(self, addr: ipaddress.IPv4Address) -> ipaddress.IPv4Address:
        o = addr.packed
        real_block = f"{o[0]}.{o[1]}.0.0/16"
        fake_block = self._slot16(real_block)
        base = ipaddress.IPv4Network(fake_block).network_address.packed
        third = o[2] ^ self._octet3[fake_block]
        return ipaddress.IPv4Address(bytes((base[0], base[1], third, o[3])))

    def _map_cgnat_v4(self, addr: ipaddress.IPv4Address) -> ipaddress.IPv4Address:
        real = str(addr)
        fake = self._cgnat.get(real)
        if fake is None:
            taken = set(self._cgnat.values())
            if len(taken) >= _CGNAT_POOL.num_addresses - 2:
                raise ProfileError("more real CGNAT hosts than the demo CGNAT pool can hold")
            n = self._digest("cgnat", real) % (_CGNAT_POOL.num_addresses - 2) + 1
            while str(_CGNAT_POOL.network_address + n) in taken:
                n = n % (_CGNAT_POOL.num_addresses - 2) + 1
            fake = str(_CGNAT_POOL.network_address + n)
            self._cgnat[real] = fake
        return ipaddress.IPv4Address(fake)

    def _map_public_v4(self, addr: ipaddress.IPv4Address) -> ipaddress.IPv4Address:
        o = addr.packed
        real24 = f"{o[0]}.{o[1]}.{o[2]}.0/24"
        fake24 = self._public24.get(real24)
        if fake24 is None:
            taken = set(self._public24.values())
            pool = [str(n) for n in _DOC_V4] + [str(n) for n in _BENCH_V4.subnets(new_prefix=24)]
            fake24 = next((p for p in pool if p not in taken), None)
            if fake24 is None:
                raise ProfileError("more real public /24s than the demo public pool can hold")
            self._public24[real24] = fake24
        base = ipaddress.IPv4Network(fake24).network_address.packed
        return ipaddress.IPv4Address(bytes((*base[:3], o[3])))

    def map_v4(self, addr: ipaddress.IPv4Address) -> ipaddress.IPv4Address:
        if is_unchanged_v4(addr) or any(addr in net for net in ALLOWED_V4):
            return addr
        if addr in _CGNAT:
            return self._map_cgnat_v4(addr)
        if _source_is_private(addr):
            return self._map_private_v4(addr)
        return self._map_public_v4(addr)

    def map_v6(self, addr: ipaddress.IPv6Address) -> ipaddress.IPv6Address:
        if is_allowed_v6(addr):
            return addr
        packed = addr.packed
        real48 = packed[:6].hex()
        fake48 = self._v6_48.get(real48)
        if fake48 is None:
            taken = set(self._v6_48.values())
            n = self._digest("v6_48", real48) % 0x10000
            while f"{n:04x}" in taken:
                n = (n + 1) % 0x10000
            fake48 = f"{n:04x}"
            self._v6_48[real48] = fake48
        prefix = _DOC_V6.network_address.packed[:4] + bytes.fromhex(fake48)
        return ipaddress.IPv6Address(prefix + packed[6:])

    def map_ip(self, text: str) -> str:
        """Map one address or CIDR string (v4 or v6); unparseable text is returned unchanged."""

        addr_text, _, plen = text.partition("/")
        try:
            addr = ipaddress.ip_address(addr_text)
        except ValueError:
            return text
        mapped: ipaddress.IPv4Address | ipaddress.IPv6Address = (
            self.map_v4(addr) if isinstance(addr, ipaddress.IPv4Address) else self.map_v6(addr)
        )
        if not plen:
            return str(mapped)
        length = int(plen)
        if isinstance(addr, ipaddress.IPv6Address) and length < 32:
            return text  # an IPv6 aggregate wider than any allocation (bogons): identifies nothing
        if ipaddress.ip_network(text, strict=False).network_address == addr and mapped != addr:
            # A network (host bits zero) stays a canonical network. An aggregate shorter than the
            # /16 remap unit cannot keep its length inside the pools: it becomes the whole pool.
            if isinstance(addr, ipaddress.IPv4Address) and length < 16:
                pool = next(p for p in _PRIVATE_POOLS if mapped in p)
                return (
                    str(pool)
                    if length <= pool.prefixlen
                    else str(ipaddress.ip_network(f"{mapped}/{length}", strict=False))
                )
            return str(ipaddress.ip_network(f"{mapped}/{length}", strict=False))
        return f"{mapped}/{plen}"

    def rewrite_text(self, text: str) -> str:
        """Remap every IPv4/IPv6 literal in free text (OIDs and versions untouched)."""

        text = IPV4_LITERAL.sub(lambda m: self.map_ip(m.group(0)), text)
        if ":" in text:
            text = IPV6_CANDIDATE.sub(self._v6_sub, text)
        return text

    def _v6_sub(self, match: re.Match[str]) -> str:
        candidate = match.group(0)
        addr_text = candidate.partition("/")[0]
        if addr_text.count(":") < 2:
            return candidate
        try:
            ipaddress.IPv6Address(addr_text)
        except ValueError:
            return candidate
        return self.map_ip(candidate)

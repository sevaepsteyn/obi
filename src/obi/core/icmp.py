"""Host-originated ICMP reachability collector (feature 072).

The active counterpart to feature 030's passive SNMP-dark inference: this pings every device **from
the obi host** and records whether it answered. ``obi icmp run`` (a ``*/5`` cron) resolves the
targets — every ``present_in_latest`` device in the discovery inventory plus the ``[siklu]`` radios
— probes them concurrently with the host's system ``ping`` (a read-only diagnostic; no credentials,
no raw sockets, no new dependency), and folds each result into the current-state store
(``icmpstore``). A device is declared *down* only after ``down_after_cycles`` consecutive missed
cycles, and a cycle is a *miss* only when all ``pings_per_cycle`` probes are lost — so a single
dropped packet on a lossy wireless hop never flips a device.

The ``[watch.icmp]`` pass (in ``watch.py``) reads the store this writes and alerts on up→down
transitions. This module holds no alerting logic: it only maintains the reachability facts.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

from obi.core import icmpstore
from obi.core.config import IcmpSettings
from obi.core.icmpstore import DeviceState
from obi.core.inventory import Inventory

if TYPE_CHECKING:
    import sqlite3

# The `ping` binary lives at /sbin/ping on macOS and /bin/ping (or /usr/bin/ping) on Linux — NOT on
# cron's minimal PATH (/usr/bin:/bin), so the collector MUST resolve it by absolute path or every
# probe from cron throws FileNotFoundError and reads as down (the whole fleet false-down).
_PING_CANDIDATES = ("/sbin/ping", "/bin/ping", "/usr/bin/ping", "/usr/sbin/ping")


@cache
def _ping_binary() -> str:
    """Absolute path to ``ping``, robust to cron's minimal PATH (falls back to `"ping"`)."""

    found = shutil.which("ping")
    if found:
        return found
    for candidate in _PING_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    return "ping"  # last resort — a bare name that will error (miss), but we tried every known path


@dataclass(frozen=True)
class Target:
    """One device to probe: a stable ``device`` identity and the ``address`` to ping."""

    device: str
    address: str


@dataclass(frozen=True)
class CollectSummary:
    """Counts from one collector cycle, for the structured log line."""

    probed: int
    up: int
    down: int
    new_down: int  # devices that flipped up→down this cycle
    recovered: int  # devices that flipped down→up this cycle
    pruned: int  # rows removed for devices no longer in the target set


# Both ping dialects summarize with a `min/avg/max` line; group 2 is the average in ms:
#   macOS:   round-trip min/avg/max/stddev = 1.234/2.345/3.456/0.512 ms
#   iputils: rtt min/avg/max/mdev = 0.045/0.052/0.061/0.006 ms
_RTT_LINE = re.compile(rb"min/avg/max[^=]*=\s*[\d.]+/([\d.]+)/")


def _parse_rtt_us(stdout: bytes) -> int | None:
    """The summary line's average round-trip in µs; ``None`` when the output has none."""

    match = _RTT_LINE.search(stdout)
    if match is None:
        return None
    try:
        return round(float(match.group(1)) * 1000)
    except ValueError:  # pragma: no cover — the regex only matches digits and dots
        return None


def probe_host(
    host: str, *, count: int, per_ping_timeout_ms: int, deadline_s: float
) -> tuple[bool, int | None]:
    """Send ``count`` ICMP echoes to ``host``; return ``(any_replied, avg_rtt_us | None)``.

    Read-only diagnostic (Principle I). Uses the platform's ``ping`` flags (macOS is the deployment
    target; a Linux branch keeps tests/CI honest). ``ping`` exits 0 when at least one echo reply
    arrives, non-zero otherwise; any error or timeout is treated as a miss — the prober must never
    raise (Field Resilience: an unreachable host is the normal case). The RTT (feature 095) is the
    summary line's average, ``None`` on a miss or an unparseable output — up/down is never affected
    by the parse.
    """

    deadline = max(1, int(deadline_s))
    ping = _ping_binary()
    if sys.platform == "darwin":
        # macOS: -W waittime is MILLISECONDS per reply; -t is a total timeout in seconds.
        cmd = [ping, "-c", str(count), "-W", str(per_ping_timeout_ms), "-t", str(deadline), host]
    else:
        # Linux iputils: -W per-reply timeout is SECONDS; -w is the overall deadline in seconds.
        per_reply_s = max(1, round(per_ping_timeout_ms / 1000))
        cmd = [ping, "-c", str(count), "-W", str(per_reply_s), "-w", str(deadline), host]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=deadline_s + 2)
    except (subprocess.TimeoutExpired, OSError):
        return False, None
    if proc.returncode != 0:
        return False, None
    return True, _parse_rtt_us(proc.stdout)


def _short_host_name(host: str) -> str:
    """Short display identity for a hand-listed host, matching the convention used everywhere else.

    RouterOS devices are keyed by their short identity (``core-gw-1``); the hand-maintained lists —
    ``[siklu].hosts`` radios and ``[icmp].extra_hosts`` (e.g. the Linux collector/NetBox VMs) — hold
    DNS names (``siteb-ptp-sitea-70.net.example.net``, ``collector-1.net.example.net``).
    Strip the domain to the first label — the same transform ``geo.py`` applies to Siklu identities
    — so the ICMP store lines up with the map, ``obi siklu status``, and ``[logs].collectors``. A
    bare IP address is kept whole.
    """

    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host.split(".")[0]  # hostname → first label (drop the DNS domain)
    return host  # an IP literal — never split it


def load_uisp_flagged(
    settings: IcmpSettings,
    *,
    snapshot_path: Path | None = None,
    ignore_names: list[str] | None = None,
) -> tuple[set[str], bool, str | None]:
    """The UISP controller's maintenance-flagged identities (feature 135, research D3).

    A thin view over :func:`load_uisp_ptp_targets` — same snapshot, same cache, same strict
    ``is True`` read — for callers that want only the flags (the watch cycle). Returns
    ``(flagged, from_cache, note)``: ``from_cache`` when the snapshot was unusable and the
    flags (if any) came from the cached roster; no snapshot AND no cache ⇒ an empty set
    (nothing in maintenance — fail toward alarming).
    """

    _targets, flagged, note = load_uisp_ptp_targets(
        settings, snapshot_path=snapshot_path, ignore_names=ignore_names
    )
    return flagged, note is not None, note


def load_uisp_ptp_targets(
    settings: IcmpSettings,
    *,
    snapshot_path: Path | None = None,
    ignore_names: list[str] | None = None,
) -> tuple[list[Target], set[str], str | None]:
    """UISP backhaul-radio probe targets from the web snapshot, held through snapshot gaps.

    Feature 095 (research D10): the Wave/backhaul radios come from the existing 5-minute
    ``uisp.json`` snapshot — devices whose identity carries the ``-ptp-`` naming-convention
    marker (the geometry pipeline's own rule) minus ``[uisp] ignore_names`` — so the prober
    needs zero new controller traffic. The derived roster is CACHED beside ``db_path``
    (``icmp-uisp-targets.json``) and a missing/unreadable snapshot reuses the cache instead of
    returning an empty set: a snapshot-cron hiccup must never shrink the probe roster, prune
    the radios' store rows, and reset their down-debounce (analysis finding U1). Returns
    ``(targets, maintenance_identities, note)`` — maintenance is the controller's operator-set
    flag (rides the same cache); the note (when set) belongs on the pipeline tile, not in an
    error.
    """

    from obi.core.config import default_web_dir
    from obi.core.filter import name_excludes
    from obi.core.geo import is_ptp_name

    path = snapshot_path or default_web_dir() / "snapshots" / "uisp.json"
    cache_path = settings.db_path.parent / "icmp-uisp-targets.json"
    excludes = list(ignore_names or [])

    def _from_cache(note: str) -> tuple[list[Target], set[str], str | None]:
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return [], set(), note + "; no cached roster — wave radios not probed"
        targets = [
            Target(device=str(row["device"]), address=str(row["address"]))
            for row in cached
            if isinstance(row, dict) and row.get("device") and row.get("address")
        ]
        maintenance = {
            str(row["device"])
            for row in cached
            if isinstance(row, dict) and row.get("device") and row.get("maintenance")
        }
        return targets, maintenance, note + " — using the cached wave roster"

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return _from_cache("uisp snapshot absent")
    except ValueError:
        return _from_cache("uisp snapshot unreadable")

    devices = payload.get("data", {}).get("devices", []) if isinstance(payload, dict) else []
    targets = []
    maintenance: set[str] = set()
    for device in devices:
        if not isinstance(device, dict):
            continue
        identity = device.get("identity")
        address = device.get("address")
        if not identity or not address or not is_ptp_name(str(identity)):
            continue
        if name_excludes(str(identity), excludes):
            continue
        targets.append(Target(device=str(identity), address=str(address)))
        if device.get("maintenance") is True:
            maintenance.add(str(identity))
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                [
                    {
                        "device": t.device,
                        "address": t.address,
                        "maintenance": t.device in maintenance,
                    }
                    for t in targets
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, cache_path)
    except OSError:
        pass  # a cache write failure never blocks the cycle; the next snapshot read retries
    return targets, maintenance, None


def resolve_targets(
    inventory: Inventory | None,
    siklu_hosts: list[str],
    *,
    ignore: list[str],
    include_siklu: bool,
    extra_hosts: list[str] | None = None,
    uisp_targets: list[Target] | None = None,
) -> list[Target]:
    """Build the probe target set: present RouterOS inventory devices + hand-listed hosts.

    Hand-listed hosts are the ``[siklu]`` radios (when ``include_siklu``) plus
    ``[icmp].extra_hosts`` (the Linux collector/NetBox VMs) — both keyed by their short name (domain
    stripped) and pinged by their full DNS name/IP. ``uisp_targets`` (feature 095) are the
    snapshot-derived backhaul radios from :func:`load_uisp_ptp_targets`, already filtered — they
    join last and dedupe by address like everything else, and the shared ``ignore`` list applies.
    Drops ``present_in_latest=False`` roster entries (decommissioned / absent in the latest crawl),
    anything matched by ``ignore`` (case-insensitive substring on name or address), and any
    duplicate address.
    """

    ignore_lc = [p.lower() for p in ignore]

    def _ignored(device: str, address: str) -> bool:
        hay = f"{device} {address}".lower()
        return any(p in hay for p in ignore_lc)

    targets: list[Target] = []
    seen: set[str] = set()
    if inventory is not None:
        for entry in inventory.entries:
            if not entry.present_in_latest:
                continue
            device = entry.identity or entry.address
            if _ignored(device, entry.address) or entry.address in seen:
                continue
            seen.add(entry.address)
            targets.append(Target(device=device, address=entry.address))

    # Hand-maintained hosts (Siklu radios + extra Linux hosts) get the same short-name treatment.
    named_hosts = (list(siklu_hosts) if include_siklu else []) + list(extra_hosts or [])
    for host in named_hosts:
        device = _short_host_name(host)
        if _ignored(device, host) or host in seen:
            continue
        seen.add(host)
        targets.append(Target(device=device, address=host))

    # 095: the snapshot-derived UISP backhaul radios join last — the Siklu FQDNs above already
    # cover the e-band radios, so address-dedup quietly drops any overlap.
    for target in uisp_targets or []:
        if _ignored(target.device, target.address) or target.address in seen:
            continue
        seen.add(target.address)
        targets.append(target)
    return targets


def _next_state(
    target: Target,
    reachable: bool,
    prev: DeviceState | None,
    *,
    now_us: int,
    down_after: int,
    rtt_us: int | None = None,
) -> DeviceState:
    """Fold one probe result into the device's next state (the debounced up/down machine).

    ``rtt_us`` is this probe's measurement — a miss stores ``None`` (the column means "most
    recent probe", never "last known good"; the dashboard shows down-since instead).
    """

    last_seen: int | None
    if reachable:
        up = True
        misses = 0
        last_seen = now_us
    else:
        up_before = prev.up if prev is not None else True
        misses = (prev.consecutive_misses if prev is not None else 0) + 1
        up = False if misses >= down_after else up_before
        last_seen = prev.last_seen_us if prev is not None else None
        rtt_us = None

    last_change = now_us if prev is None or prev.up != up else prev.last_change_us

    return DeviceState(
        device=target.device,
        address=target.address,
        up=up,
        consecutive_misses=misses,
        last_seen_us=last_seen,
        last_change_us=last_change,
        updated_us=now_us,
        last_rtt_us=rtt_us,
    )


def run_collect(
    settings: IcmpSettings,
    targets: list[Target],
    *,
    conn: sqlite3.Connection,
    now_us: int,
    probe: Callable[[str], tuple[bool, int | None]] | None = None,
) -> CollectSummary:
    """Probe every target concurrently and persist the folded state; return the cycle summary.

    ``probe`` is injectable (defaults to the real :func:`probe_host` bound to ``settings``) so tests
    drive the state machine with scripted ``(reachable, rtt_us)`` results and no network. Prunes
    rows for devices no longer in ``targets`` (decommissioned) and commits the cycle atomically.
    """

    if probe is None:

        def probe(host: str) -> tuple[bool, int | None]:
            return probe_host(
                host,
                count=settings.pings_per_cycle,
                per_ping_timeout_ms=settings.ping_timeout_ms,
                deadline_s=settings.deadline_s,
            )

    prev_by_device = {s.device: s for s in icmpstore.load_states(conn)}

    if targets:
        with ThreadPoolExecutor(max_workers=settings.concurrency) as pool:
            results = list(pool.map(lambda t: (t, probe(t.address)), targets))
    else:
        results = []

    rows: list[DeviceState] = []
    up = down = new_down = recovered = 0
    for target, (reachable, rtt_us) in results:
        prev = prev_by_device.get(target.device)
        state = _next_state(
            target,
            reachable,
            prev,
            now_us=now_us,
            down_after=settings.down_after_cycles,
            rtt_us=rtt_us,
        )
        rows.append(state)
        if state.up:
            up += 1
            if prev is not None and not prev.up:
                recovered += 1
        else:
            down += 1
            if prev is None or prev.up:
                new_down += 1

    icmpstore.upsert_state(conn, rows)
    pruned = icmpstore.prune_absent(conn, [t.device for t in targets])
    conn.commit()

    return CollectSummary(
        probed=len(results), up=up, down=down, new_down=new_down, recovered=recovered, pruned=pruned
    )

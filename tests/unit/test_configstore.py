"""Unit (feature 026): the git-backed config store — normalization, commit-only-on-change,
history/diff/as-of retrieval, and coverage, all against a real tmp git repo."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from obi.core import configstore as cs

_W = datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC)

_EXPORT = (
    "# 2026-07-05 12:00:00 by RouterOS 7.15\n"
    "# software id = ABCD-EFGH\n"
    "/ip address\n"
    "add address=172.25.110.1/24 interface=ether1\n"
)
# Same config, later run — ONLY the dated header differs.
_EXPORT_RERUN = (
    "# 2026-07-06 09:30:00 by RouterOS 7.15\n"
    "# software id = ABCD-EFGH\n"
    "/ip address\n"
    "add address=172.25.110.1/24 interface=ether1\n"
)
# A real config change.
_EXPORT_CHANGED = (
    "# 2026-07-07 08:00:00 by RouterOS 7.16\n"
    "# software id = ABCD-EFGH\n"
    "/ip address\n"
    "add address=172.25.110.2/24 interface=ether1\n"
)


def _record(repo: Path, content: str, when: datetime, identity: str = "core-gw-1") -> str | None:
    return cs.record_version(
        repo, identity, content, address="172.25.110.1", router_version="7.15", when=when
    )


def test_normalize_strips_dated_header_and_is_idempotent() -> None:
    out = cs.normalize_export(_EXPORT)
    assert "by RouterOS" not in out
    assert out.startswith("# software id = ABCD-EFGH")
    assert out.endswith("\n")
    assert cs.normalize_export(out) == out
    # The header is the ONLY difference between the two captures → identical normalized bytes.
    assert cs.normalize_export(_EXPORT) == cs.normalize_export(_EXPORT_RERUN)


def test_parse_router_version() -> None:
    assert cs.parse_router_version(_EXPORT) == "7.15"
    assert cs.parse_router_version(_EXPORT_CHANGED) == "7.16"
    assert cs.parse_router_version("/ip address\n") is None


def test_redact_export_scrubs_snmp_community() -> None:
    # non-terse: section header then an indented set line (the real RouterOS /export shape)
    non_terse = "/snmp community\nset [ find x ] name=fakesecret3d41d7\n/ip a\nadd name=ether1\n"
    out = cs.redact_export(non_terse)
    assert "fakesecret3d41d7" not in out
    assert "name=<redacted-by-obi>" in out
    # a `name=` OUTSIDE /snmp community (e.g. an interface name) is left untouched
    assert "name=ether1" in out

    # terse: the whole command on one line
    terse = "/snmp community set [ find default=yes ] name=fakesecret3d41d7 addresses=::/0\n"
    assert "fakesecret3d41d7" not in cs.redact_export(terse)

    # SNMPv3 auth/priv passwords are scrubbed too (belt-and-suspenders)
    v3 = "/snmp community set authentication-password=hunter2 encryption-password=swordfish\n"
    red = cs.redact_export(v3)
    assert "hunter2" not in red and "swordfish" not in red

    # the trap-community in /snmp set (a DIFFERENT field/section than /snmp community) is scrubbed
    trap = "/snmp set enabled=yes trap-community=fakesecret3d41d7\n"
    assert "fakesecret3d41d7" not in cs.redact_export(trap)


def test_redact_export_value_backstop() -> None:
    # A known secret is scrubbed wherever it literally appears — even where the field-list misses.
    text = "/system script add name=s source=\":put fakesecret3d41d7\"\n"
    out = cs.redact_export(text, extra_secrets=("fakesecret3d41d7",))
    assert "fakesecret3d41d7" not in out
    # trivially short literals are ignored (would otherwise shred unrelated config)
    assert cs.redact_export("add name=ab\n", extra_secrets=("ab",)) == "add name=ab\n"


def test_record_version_stores_redacted(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    raw = "# 2026-07-05 12:00:00 by RouterOS 7\n/snmp community\nset [ find x ] name=SECRET123\n"
    cs.record_version(repo, "gw", raw, address="172.25.110.1", router_version="7", when=_W)
    stored = cs.current_content(repo, "gw") or ""
    assert "SECRET123" not in stored  # SC-005: no secret in the store
    assert "<redacted-by-obi>" in stored


def test_first_capture_commits_then_header_only_rerun_does_not(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    sha1 = _record(repo, _EXPORT, _W)
    assert sha1  # first version committed
    # Re-store content that differs only in the volatile header → NO new commit (FR-004 / SC-002).
    assert _record(repo, _EXPORT_RERUN, _W + timedelta(days=1)) is None
    hist, _ = cs.history(repo, "core-gw-1", 10)
    assert len(hist) == 1


def test_real_change_commits_once(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _record(repo, _EXPORT, _W)
    sha2 = _record(repo, _EXPORT_CHANGED, _W + timedelta(days=2))
    assert sha2
    hist, truncated = cs.history(repo, "core-gw-1", 10)
    assert [h.commit for h in hist][0] == sha2
    assert len(hist) == 2
    assert truncated is False
    # newest change touched one add line: one insertion, one deletion
    assert hist[0].insertions == 1 and hist[0].deletions == 1
    assert hist[0].captured_at == _W + timedelta(days=2)


def test_history_truncation_flag(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    for i in range(4):
        content = (
            f"# 2026-07-0{i + 1} 00:00:00 by RouterOS 7\n/ip a\nadd address=172.25.110.{i}/24\n"
        )
        _record(repo, content, _W + timedelta(days=i))
    hist, truncated = cs.history(repo, "core-gw-1", 2)
    assert len(hist) == 2
    assert truncated is True


def test_content_at_and_resolve_ref(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _record(repo, _EXPORT, _W)
    _record(repo, _EXPORT_CHANGED, _W + timedelta(days=2))

    assert "172.25.110.2/24" in (cs.current_content(repo, "core-gw-1") or "")
    # As of just after the first capture, we should see the FIRST config.
    ref = cs.resolve_ref(repo, "core-gw-1", "1d", now=_W + timedelta(days=1))
    assert ref is not None
    assert "172.25.110.1/24" in (cs.content_at(repo, "core-gw-1", ref) or "")
    assert cs.resolve_ref(repo, "core-gw-1", "current") == "HEAD"


def test_diff_between_versions(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _record(repo, _EXPORT, _W)
    _record(repo, _EXPORT_CHANGED, _W + timedelta(days=2))
    hist, _ = cs.history(repo, "core-gw-1", 10)
    text = cs.diff(repo, "core-gw-1", hist[1].commit, "HEAD")
    assert "-add address=172.25.110.1/24" in text
    assert "+add address=172.25.110.2/24" in text


def test_coverage_and_unknown_device(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _record(repo, _EXPORT, _W, identity="core-gw-1")
    _record(repo, _EXPORT, _W, identity="edge-gw-2")
    cov = {d.identity: d for d in cs.coverage(repo)}
    assert set(cov) == {"core-gw-1", "edge-gw-2"}
    assert cov["core-gw-1"].versions == 1
    assert cov["core-gw-1"].last_changed_at == _W
    # unknown device / empty queries never raise
    assert cs.current_content(repo, "nope") is None
    assert cs.history(repo, "nope", 10) == ([], False)
    assert cs.resolve_ref(repo, "nope", "current") is None


def test_empty_repo_reads_are_total(tmp_path: Path) -> None:
    repo = tmp_path / "empty"
    assert cs.coverage(repo) == []
    assert cs.current_content(repo, "x") is None
    assert cs.history(repo, "x", 5) == ([], False)


def test_path_identity_roundtrip_and_sanitization() -> None:
    assert cs.path_for_identity("core-gw-1") == "core-gw-1.rsc"
    # unsafe characters are replaced, never allowing traversal
    assert cs.path_for_identity("a/b c") == "a_b_c.rsc"
    assert "/" not in cs.path_for_identity("../etc/passwd")
    assert cs.identity_for_path("core-gw-1.rsc") == "core-gw-1"


# --- feature 099: reorder-only detection + transient-warning stripping -------------------------


def test_is_line_permutation_multiset_semantics() -> None:
    a = "/ip pool add name=guest\n/ip dhcp-server add pool=guest\n/ip dhcp-client add i=v\n"
    moved = "/ip pool add name=guest\n/ip dhcp-client add i=v\n/ip dhcp-server add pool=guest\n"
    assert cs.is_line_permutation(a, moved)
    assert cs.is_line_permutation(moved, a)
    assert not cs.is_line_permutation(a, a)  # identical bytes are not a "reorder"
    assert not cs.is_line_permutation(a, a.replace("guest", "lan"))  # edited line
    assert not cs.is_line_permutation(a, "/ip pool add name=guest\n/ip dhcp-client add i=v\n")
    # multiset, not set: losing one of two identical lines is a real change
    assert not cs.is_line_permutation("dup\ndup\nx\n", "x\ndup\n")
    assert not cs.is_line_permutation("", "")


# The verbatim shapes from basswood-gw commit 2a8fec4 (research.md R2): during the netwatch
# startup-delay window an entry exports as THREE lines (menu / warning / command); entries past
# the window stay terse — both forms in the same export. Warning presence must be byte-invisible.
_WARN_FORM = (
    "# 2026-08-24 05:00:00 by RouterOS 7.15.3\n"
    "# model = RB5009UPr+S+\n"
    "/tool netwatch\n"
    "# Warning: probe waiting startup-delay=5m; 2m11s remaining\n"
    "add comment=basswood-zelkova-60 disabled=no host=169.254.1.3 type=simple\n"
    "/tool netwatch add comment=basswood-hotel-05 disabled=yes host=169.254.2.0 type=simple\n"
    "/tool netwatch\n"
    "# Warning: probe waiting startup-delay=5m; 2m11s remaining\n"
    "add comment=basswood-google host=8.8.8.8 name=basswood-google type=simple\n"
)
_TERSE_FORM = (
    "# 2026-08-24 06:00:00 by RouterOS 7.15.3\n"
    "# model = RB5009UPr+S+\n"
    "/tool netwatch add comment=basswood-zelkova-60 disabled=no host=169.254.1.3 type=simple\n"
    "/tool netwatch add comment=basswood-hotel-05 disabled=yes host=169.254.2.0 type=simple\n"
    "/tool netwatch add comment=basswood-google host=8.8.8.8 name=basswood-google type=simple\n"
)


def test_normalize_strips_warnings_and_rejoins_split_entries() -> None:
    out = cs.normalize_export(_WARN_FORM)
    assert "# Warning:" not in out
    assert "/tool netwatch add comment=basswood-zelkova-60" in out
    # the flip/flip-back invariant: warned and unwarned captures of the same config are equal
    assert out == cs.normalize_export(_TERSE_FORM)
    # order preserved (mixed warned/unwarned entries keep their relative positions)
    lines = out.splitlines()
    idx = [i for i, ln in enumerate(lines) if ln.startswith("/tool netwatch add ")]
    assert [lines[i].split("comment=")[1].split(" ")[0] for i in idx] == [
        "basswood-zelkova-60",
        "basswood-hotel-05",
        "basswood-google",
    ]
    assert cs.normalize_export(out) == out  # idempotent on the new output


def test_normalize_drops_stray_warning_without_joining() -> None:
    # a warning NOT in the split shape (previous kept line is a full terse command) is dropped
    # without touching its neighbors
    raw = (
        "/ip address add address=172.25.110.1/24 interface=ether1\n"
        "# Warning: something transient\n"
        "/ip route add dst-address=0.0.0.0/0 gateway=172.25.110.254\n"
    )
    assert cs.normalize_export(raw) == (
        "/ip address add address=172.25.110.1/24 interface=ether1\n"
        "/ip route add dst-address=0.0.0.0/0 gateway=172.25.110.254\n"
    )


def test_normalize_keeps_stable_comment_lines() -> None:
    # stable device-generated comments (research.md R3) are NOT warnings — kept verbatim
    raw = (
        "#\n"
        "# model = CRS318-16P-2S+\n"
        "/interface bridge port\n"
        "# bond-core-gw-2 not a bridge port\n"
        "add bridge=bridge interface=ether2\n"
    )
    assert cs.normalize_export(raw) == raw


def test_record_version_ignores_warning_only_flips(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    assert _record(repo, _TERSE_FORM, _W, identity="basswood-gw") is not None
    # same config re-captured inside the startup-delay window → no new version
    assert _record(repo, _WARN_FORM, _W + timedelta(hours=1), identity="basswood-gw") is None
    # a real edit in warning form still commits
    changed = _WARN_FORM.replace("host=8.8.8.8", "host=9.9.9.9")
    assert _record(repo, changed, _W + timedelta(hours=2), identity="basswood-gw") is not None

"""``obi identity`` — keep the tracked tree free of deployment identity (feature 137).

A thin CLI adapter over :mod:`obi.core.identity`. No subcommand ever prints a matched or real
value, and the audit records carry counts only (contracts/cli.md). Exit codes: 0 ok, 1 findings
or a failed check, 2 usage/configuration error.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.markup import escape

from obi.core.config import Config, ConfigError, default_config_path, load_config

identity_app = typer.Typer(
    add_completion=False,
    help=(
        "Deployment identity (feature 137): scan the tracked tree for real identifiers, scrub "
        "them to fictional values, migrate moved settings into config, and prove the live host "
        "behaves the same. Never prints a real value."
    ),
)

ConfigOpt = Annotated[Path | None, typer.Option("--config", help="Path to config.toml.")]


def _err() -> Console:
    return Console(stderr=True)


def _load(config_path: Path | None) -> Config:
    path = config_path or default_config_path()
    try:
        return load_config(path)
    except ConfigError as exc:
        _err().print(f"[red]config error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc


def _audit(config: Config | None, action: str, reason: str, params: dict[str, Any]) -> None:
    """Counts-only audit record; silently skipped when no config is available (a public clone)."""

    if config is None:
        return
    from obi.core.audit import AuditTrail
    from obi.core.models import AuditOutcome, AuditRecord

    AuditTrail(config.audit_path).record(
        AuditRecord(
            timestamp=datetime.now(UTC),
            action=f"identity.{action}",
            target="repository",
            reason=reason,
            outcome=AuditOutcome.OK,
            params=params,
        )
    )


@identity_app.command("parity")
def identity_parity_command(
    record_label: Annotated[
        str | None, typer.Option("--record", help="Render all surfaces and save as LABEL.")
    ] = None,
    compare_labels: Annotated[
        tuple[str, str] | None,
        typer.Option("--compare", help="Compare two recorded snapshots: A B."),
    ] = None,
    config_path: ConfigOpt = None,
) -> None:
    """Record or compare live-continuity snapshots (private; diffs shown on this terminal only)."""

    from obi.core.identity import parity

    if (record_label is None) == (compare_labels is None):
        _err().print("give exactly one of --record LABEL or --compare A B")
        raise typer.Exit(code=2)
    config = _load(config_path)
    if record_label is not None:
        try:
            path = parity.record(config, record_label)
        except ValueError as exc:
            _err().print(f"[red]{escape(str(exc))}[/red]")
            raise typer.Exit(code=2) from exc
        surfaces = parity.load(record_label)
        errors = sum(1 for v in surfaces.values() if v.startswith("<render error"))
        _audit(config, "parity", "record a live-continuity snapshot (feature 137)",
               {"surfaces": len(surfaces), "render_errors": errors})  # fmt: skip
        Console().print(f"recorded {len(surfaces)} surfaces → {escape(str(path))}")
        if errors:
            _err().print(f"[yellow]{errors} surface(s) failed to render[/yellow]")
            raise typer.Exit(code=1)
        return
    assert compare_labels is not None
    a_label, b_label = compare_labels
    try:
        results = parity.compare(parity.load(a_label), parity.load(b_label))
    except (OSError, ValueError, KeyError) as exc:
        _err().print(f"[red]cannot load snapshots:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc
    out = Console()
    counts: dict[str, int] = {}
    for item in results:
        counts[item.status] = counts.get(item.status, 0) + 1
        out.print(f"{item.status:>9}  {escape(item.surface)}")
        if item.status in ("different", "intended"):
            for line in item.diff:
                out.print(f"           {escape(line)}", highlight=False)
    _audit(config, "parity", "compare live-continuity snapshots (feature 137)", counts)
    if counts.get("different") or counts.get("missing"):
        raise typer.Exit(code=1)


def _repo_root() -> Path:
    import subprocess

    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], check=True, capture_output=True, text=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        _err().print("[red]not inside a git checkout[/red]")
        raise typer.Exit(code=2) from exc
    return Path(out)


def _tokens(no_private: bool) -> Any:
    from obi.core.identity.tokens import build_tokens

    try:
        if no_private:
            return build_tokens(denylist=None, mapping=None, config=None)
        return build_tokens()
    except ConfigError as exc:
        _err().print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=2) from exc


def _optional_config() -> Config | None:
    try:
        return load_config(default_config_path())
    except ConfigError:
        return None


@identity_app.command("scan")
def identity_scan_command(
    paths: Annotated[
        list[str] | None, typer.Argument(help="Repo-relative paths (default: every tracked file).")
    ] = None,
    include_warnings: Annotated[
        bool, typer.Option("--include-warnings", help="List warning findings too.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    no_private: Annotated[
        bool,
        typer.Option("--no-private", help="Shape checks only — what a public clone enforces."),
    ] = False,
) -> None:
    """Scan the tracked tree for real identifiers (findings name file/line/category, not values)."""

    from obi.core.identity.guard import scan_paths

    root = _repo_root()
    tokens = _tokens(no_private)
    try:
        result = scan_paths(paths or None, root, tokens)
    except ConfigError as exc:
        _err().print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=2) from exc
    if as_json:
        print(result.to_json())
    else:
        Console().print(escape(result.report(include_warnings=include_warnings)), highlight=False)
    _audit(
        None if no_private else _optional_config(),
        "scan",
        "scan the tracked tree for deployment identity (feature 137)",
        {
            "files": result.files,
            "fatal": len(result.fatal),
            "warnings": len(result.warnings),
            "inputs": result.inputs,
        },  # fmt: skip
    )
    if result.fatal:
        raise typer.Exit(code=1)


@identity_app.command("migrate-check")
def identity_migrate_check_command(config_path: ConfigOpt = None) -> None:
    """Which moved settings (feature 137) this config sets — never prints a value.

    Exit 1 when a key whose section IS present is unset: the old built-in default used to
    apply there, so behaviour would change silently."""

    import tomllib

    from obi.core.identity.moved import migration_rows

    path = config_path or default_config_path()
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        _err().print(f"[red]cannot read config:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc
    rows = migration_rows(raw)
    out = Console()
    for row in rows:
        moved = row.value
        out.print(
            f"{row.state:>14}  {escape(moved.key):<34} {escape(moved.feature)} — if unset: "
            f"{escape(moved.unset_behaviour)}",
            highlight=False,
        )
    unset = [r for r in rows if r.blocking]
    if unset:
        out.print("\nadd to the config (fill in each <value>):")
        for row in unset:
            out.print(escape(row.value.toml), highlight=False)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.state] = counts.get(row.state, 0) + 1
    _audit(_optional_config(), "migrate-check", "moved-settings check (feature 137)", counts)
    if unset:
        raise typer.Exit(code=1)


def _private_tokens_or_exit() -> Any:
    from obi.core.identity.tokens import LOADED

    tokens = _tokens(False)
    if tokens.inputs.get("mapping") != LOADED or tokens.inputs.get("denylist") != LOADED:
        _err().print(
            "[red]the private denylist and 134 mapping are both required[/red] "
            f"(inputs: {tokens.inputs})"
        )
        raise typer.Exit(code=2)
    return tokens


@identity_app.command("scrub")
def identity_scrub_command(
    paths: Annotated[list[str] | None, typer.Argument(help="Repo-relative paths to scrub.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Count only; write nothing.")] = False,
    init_geo: Annotated[
        bool,
        typer.Option("--init-geo", help="Assign the private fixture longitude offset (once)."),
    ] = False,
) -> None:
    """Replace real identity in tracked files with the private mapping's fakes (feature 137).

    Originals are archived owner-only under the state dir first; files still fatal afterwards
    are listed (exit 1). Never prints a real value."""

    from obi.core.identity.scrub import ScrubError, init_fixture_offset, scrub_paths
    from obi.core.identity.tokens import default_mapping_path

    tokens = _private_tokens_or_exit()
    if init_geo:
        before = tokens.mapping.fixture_dlon
        init_fixture_offset(tokens.mapping)
        tokens.mapping.save(default_mapping_path())
        Console().print("fixture offset " + ("already set" if before is not None else "assigned"))
        if not paths:
            return
    if not paths:
        _err().print("give at least one path to scrub")
        raise typer.Exit(code=2)
    try:
        result = scrub_paths(
            paths, _repo_root(), tokens, dry_run=dry_run, mapping_path=default_mapping_path()
        )
    except ScrubError as exc:
        _err().print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=2) from exc
    Console().print(escape(result.report()), highlight=False)
    _audit(
        _optional_config(),
        "scrub",
        "scrub deployment identity from tracked files (feature 137)",
        {"files": len(result.files), "need_attention": len(result.failed), "dry_run": dry_run},
    )
    if result.failed:
        raise typer.Exit(code=1)


callsign_app = typer.Typer(
    add_completion=False, help="Curated fictional radio/mesh callsigns (never prints real ones)."
)
identity_app.add_typer(callsign_app, name="callsign")


def _callsign_rows(tokens: Any) -> list[tuple[int, str, str | None]]:
    curated = tokens.mapping.assignments("callsign")
    return [(i, real, curated.get(real)) for i, real in enumerate(sorted(tokens.callsigns))]


@callsign_app.command("list")
def identity_callsign_list_command() -> None:
    """Opaque refs and their curated fakes ('—' when not curated yet)."""

    for ref, _real, fake in _callsign_rows(_private_tokens_or_exit()):
        Console().print(f"{ref:>3}  {escape(fake) if fake else '—'}", highlight=False)


@callsign_app.command("set")
def identity_callsign_set_command(
    ref: Annotated[int, typer.Argument(help="The ref from `obi identity callsign list`.")],
    fake: Annotated[str, typer.Argument(help="A fictional single-token callsign.")],
) -> None:
    """Curate the fake for one callsign (saved in the private mapping)."""

    from obi.core.identity.tokens import default_mapping_path

    tokens = _private_tokens_or_exit()
    rows = {r: real for r, real, _f in _callsign_rows(tokens)}
    if ref not in rows:
        _err().print(f"no callsign ref {ref}")
        raise typer.Exit(code=2)
    try:
        tokens.mapping.set_callsign(rows[ref], fake)
    except ValueError as exc:
        _err().print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=2) from exc
    tokens.mapping.save(default_mapping_path())
    Console().print(f"{ref:>3}  {escape(fake)}")


@callsign_app.command("check")
def identity_callsign_check_command(config_path: ConfigOpt = None) -> None:
    """Prove the curated fakes keep every fuzzy-match decision band (refs only on failure)."""

    from obi.core.identity.callsign import check

    tokens = _private_tokens_or_exit()
    rows = [(r, real, fake) for r, real, fake in _callsign_rows(tokens) if fake]
    variants = ["obi", "obie", "obee", "o b", "opie", "hobi"]
    config = _optional_config() if config_path is None else _load(config_path)
    if config is not None and config.radio is not None:
        variants = [*config.radio.name_variants, *config.radio.name_variants_weak]
    violations = check([(real, fake) for _r, real, fake in rows], variants)
    uncurated = len(tokens.callsigns) - len(rows)
    for v in violations:
        right = rows[v.right][0] if v.right >= 0 else f"station variant {-1 - v.right}"
        Console().print(f"FAIL  {v.rule}: ref {rows[v.left][0]} vs {right}", highlight=False)
    Console().print(f"{len(rows)} curated, {uncurated} not yet, {len(violations)} violation(s)")
    if violations or uncurated:
        raise typer.Exit(code=1)


_HOOK_MARK = "# obi identity guard (feature 137)"
_HOOK = """#!/bin/sh
{mark}
# Scans only the lines this commit ADDS; prints file/line/category, never a value.
set -e
top="$(git rev-parse --show-toplevel)"
if [ -x "$top/.venv/bin/python" ]; then
    "$top/.venv/bin/python" -m obi.core.identity.hook
elif command -v uv >/dev/null 2>&1; then
    uv run --project "$top" --quiet python -m obi.core.identity.hook
else
    echo "obi identity guard: no python environment found; skipping" >&2
fi
if [ -x "$(dirname "$0")/pre-commit.local" ]; then
    exec "$(dirname "$0")/pre-commit.local" "$@"
fi
"""


@identity_app.command("install-hook")
def identity_install_hook_command(
    force: Annotated[
        bool, typer.Option("--force", help="Chain an existing foreign hook as pre-commit.local.")
    ] = False,
) -> None:
    """Install the identity pre-commit hook in the repository's common hooks dir (all worktrees)."""

    import os
    import subprocess

    root = _repo_root()
    common = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--git-common-dir"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    hooks = (root / common).resolve() / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    target = hooks / "pre-commit"
    if target.exists():
        current = target.read_text(encoding="utf-8", errors="replace")
        if _HOOK_MARK not in current:
            if not force:
                _err().print(
                    f"[red]a different pre-commit hook exists at {escape(str(target))}[/red]; "
                    "re-run with --force to keep it as pre-commit.local and chain it"
                )
                raise typer.Exit(code=2)
            target.rename(hooks / "pre-commit.local")
    target.write_text(_HOOK.format(mark=_HOOK_MARK), encoding="utf-8")
    os.chmod(target, 0o755)
    Console().print(f"installed {escape(str(target))}")

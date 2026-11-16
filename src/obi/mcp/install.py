"""Idempotent registration of the MCP server with Claude Desktop and Claude Code.

Two very different mechanisms, deliberately (research D5/D6):

- **Claude Desktop** has no vendor CLI and documents ``claude_desktop_config.json`` as its
  configuration surface, so obi edits it directly — but surgically: ONLY the
  ``mcpServers.obi`` key is obi's; every foreign entry and top-level key is preserved, the
  write is atomic (temp file + rename), and a file obi cannot parse aborts the target
  untouched — never guess at someone's hand-edited config.
- **Claude Code**'s user-scope registry lives inside ``~/.claude.json``, a private
  mixed-content file owned by another tool — obi never reads or writes it, and instead
  converges through the supported ``claude mcp get/add/remove`` subcommands.

Both halves are convergence operations: run once or N times, same end state, with the
outcome named honestly (``created`` / ``updated`` / ``already registered`` / ``skipped`` /
``error``). ``--dry-run`` computes the same outcomes without writing or mutating anything.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

#: The registered launch entry, shared by both targets: absolute path (clients spawn the
#: server without obi's venv or PATH) plus the serve subcommand.
_SERVER_ARGS = ("mcp", "serve")

Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


@dataclass
class InstallOutcome:
    """One target's result: what happened, in a word, plus a human detail line."""

    target: str  # "claude desktop" | "claude code"
    outcome: str  # created | updated | already registered | skipped | error
    detail: str


def resolve_obi_path() -> Path:
    """The absolute path to the ``obi`` console script, for the registered command.

    The script lives beside the running interpreter in the venv; ``PATH`` is the fallback
    for non-venv installs. Neither resolving is an error the caller must surface — a
    registration pointing nowhere would fail silently inside the client later.
    """

    candidate = Path(sys.executable).parent / "obi"
    if candidate.exists():
        return candidate
    which = shutil.which("obi")
    if which is not None:
        return Path(which)
    raise OSError(
        "could not locate the obi executable — expected it beside the Python interpreter "
        f"({candidate}) or on PATH. Install obi's environment first, then re-run."
    )


def _desktop_config_path(home: Path) -> Path:
    return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"


def desktop_install(obi_path: Path, *, home: Path, dry_run: bool) -> InstallOutcome:
    """Converge ``mcpServers.obi`` in Claude Desktop's config; touch nothing else."""

    target = "claude desktop"
    path = _desktop_config_path(home)
    desired = {"command": str(obi_path), "args": list(_SERVER_ARGS)}

    data: dict[str, object] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return InstallOutcome(
                target,
                "error",
                f"existing config is not valid JSON ({exc}) — fix {path} by hand; "
                "nothing was changed",
            )
        if not isinstance(loaded, dict):
            return InstallOutcome(
                target,
                "error",
                f"existing config is not a JSON object — fix {path} by hand; "
                "nothing was changed",
            )
        data = loaded

    servers = data.get("mcpServers")
    if servers is None:
        servers = {}
    if not isinstance(servers, dict):
        return InstallOutcome(
            target,
            "error",
            f'"mcpServers" in {path} is not an object — fix it by hand; nothing was changed',
        )

    current = servers.get("obi")
    if current == desired:
        return InstallOutcome(target, "already registered", str(path))
    outcome = "updated" if current is not None else "created"
    if dry_run:
        return InstallOutcome(
            target, outcome, f'would set mcpServers.obi = {json.dumps(desired)} in {path}'
        )

    servers["obi"] = desired
    data["mcpServers"] = servers
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replace so a crash mid-write can never leave Desktop a half-written config.
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".obi-install-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    return InstallOutcome(target, outcome, str(path))


def _default_runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)  # noqa: S603


def code_install(obi_path: Path, *, runner: Runner | None = None, dry_run: bool) -> InstallOutcome:
    """Converge the Claude Code user-scope registration through the ``claude`` CLI."""

    target = "claude code"
    run = runner if runner is not None else _default_runner
    add_argv = [
        "claude", "mcp", "add",
        "--scope", "user",
        "--transport", "stdio",
        "obi",
        "--", str(obi_path), *_SERVER_ARGS,
    ]
    manual = " ".join(add_argv)

    try:
        probe = run(["claude", "mcp", "get", "obi"])
    except FileNotFoundError:
        return InstallOutcome(
            target, "skipped", f"claude CLI not found — register by hand with: {manual}"
        )

    if probe.returncode == 0:
        # `get` prints the registration in human form; the path + serve subcommand appearing
        # in it is our sameness check (the CLI offers no machine-readable dump).
        if str(obi_path) in probe.stdout and "serve" in probe.stdout:
            return InstallOutcome(target, "already registered", "user scope")
        outcome = "updated"
    else:
        outcome = "created"
    if dry_run:
        return InstallOutcome(target, outcome, f"would run: {manual}")

    try:
        if outcome == "updated":
            removed = run(["claude", "mcp", "remove", "--scope", "user", "obi"])
            if removed.returncode != 0:
                return InstallOutcome(
                    target, "error", f"claude mcp remove failed: {removed.stderr.strip()}"
                )
        added = run(add_argv)
    except FileNotFoundError:
        return InstallOutcome(
            target, "skipped", f"claude CLI not found — register by hand with: {manual}"
        )
    if added.returncode != 0:
        return InstallOutcome(target, "error", f"claude mcp add failed: {added.stderr.strip()}")
    return InstallOutcome(target, outcome, "user scope, via `claude mcp add`")


def install(
    *,
    dry_run: bool = False,
    home: Path | None = None,
    runner: Runner | None = None,
    obi_path: Path | None = None,
) -> list[InstallOutcome]:
    """Register with both clients; one outcome per target, partial success reported as such."""

    obi = obi_path if obi_path is not None else resolve_obi_path()
    return [
        desktop_install(obi, home=home if home is not None else Path.home(), dry_run=dry_run),
        code_install(obi, runner=runner, dry_run=dry_run),
    ]

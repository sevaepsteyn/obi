"""Config-backup source (feature 134, T028): a FRESH git history, never the real one.

The output repo starts with one baseline commit — the live tree as of the window start (so every
device has a config) — then replays only the in-window commits in order: each file rewritten
(RouterOS secrets structurally scrubbed first), renamed to its fictional identity, committed with
the shifted date and the fixed author ``obi <obi@example.net>``. The live repo's objects, reflog
and author identities never reach the dataset. The git index (stat data: inode, mtime) is removed
afterwards so two runs produce identical bytes (SC-005); obi reads the store via ``git show`` /
``log`` and the working tree, which need no index.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from obi.core import configstore
from obi.core.demo.sources import ABSENT, INCLUDED, SourceContext, SourceReport

NAME = "configs"
AUTHOR = ("obi", "obi@example.net")


def _new_name(ctx: SourceContext, rel: str) -> str:
    return configstore.path_for_identity(ctx.rewriter.ident(configstore.identity_for_path(rel)))


def _files(ctx: SourceContext, files: dict[str, str | None]) -> dict[str, str | None]:
    return {
        _new_name(ctx, rel): (ctx.rewriter.export(text) if text is not None else None)
        for rel, text in sorted(files.items())
    }


def run(ctx: SourceContext) -> SourceReport:
    repo = ctx.state_dir / "config-repo"
    if not (repo / ".git").exists():
        return SourceReport(NAME, ABSENT)
    out = ctx.out_state / "config-repo"
    lo, hi = ctx.window
    since = datetime.fromtimestamp(lo / 1e6, tz=UTC)
    until = datetime.fromtimestamp(hi / 1e6, tz=UTC)
    rows_in = rows_out = 0

    baseline = configstore.last_commit_before(repo, since)
    if baseline is not None:
        configstore.commit_snapshot(
            out,
            _files(ctx, dict(configstore.tree_files(repo, baseline))),
            message="baseline: configs as of the start of the demo window",
            when=ctx.shift.dt(since),
            author_name=AUTHOR[0],
            author_email=AUTHOR[1],
        )
    for sha, when in configstore.commits_between(repo, since, until):
        rows_in += 1
        configstore.commit_snapshot(
            out,
            _files(ctx, configstore.changed_files(repo, sha)),
            message=ctx.rewriter.text(configstore.commit_message(repo, sha).strip()),
            when=ctx.shift.dt(datetime.fromisoformat(when)),
            author_name=AUTHOR[0],
            author_email=AUTHOR[1],
        )
        rows_out += 1
    (out / ".git" / "index").unlink(missing_ok=True)
    for sample in (out / ".git" / "hooks").glob("*.sample"):
        sample.unlink()

    captures = configstore.capture_state_path(repo)
    if captures.exists():
        data = json.loads(captures.read_text(encoding="utf-8"))
        rewritten = ctx.rewriter.json(data)
        configstore.capture_state_path(out).write_text(
            json.dumps(rewritten, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    return SourceReport(NAME, INCLUDED, rows_in=rows_in, rows_out=rows_out)

"""configstore.push_orphan_branch — the Pages publish primitive (feature 136, T038)."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

from obi.core.configstore import push_orphan_branch

WHEN = datetime(2025, 6, 5, 12, 0, tzinfo=UTC)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()  # fmt: skip


def _remote(tmp_path: Path) -> tuple[Path, str]:
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "README").write_text("source\n")
    _git(seed.parent, "init", "-q", "-b", "main", str(seed))
    _git(seed, "add", "README")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
         "commit", "-q", "-m", "source")  # fmt: skip
    _git(seed, "push", "-q", str(bare), "main")
    return bare, _git(bare, "rev-parse", "main")


def test_single_commit_orphan_replaced_main_untouched(tmp_path: Path) -> None:
    bare, main_before = _remote(tmp_path)
    site = tmp_path / "site"
    (site / "data").mkdir(parents=True)
    (site / "index.html").write_text("<html>v1</html>")
    (site / "data" / "a.json").write_text("{}")

    sha = push_orphan_branch(site, str(bare), "gh-pages", "demo v1", WHEN)
    assert _git(bare, "rev-parse", "gh-pages") == sha
    assert _git(bare, "rev-list", "--count", "gh-pages") == "1"
    assert sorted(_git(bare, "ls-tree", "-r", "--name-only", "gh-pages").split()) == [
        "data/a.json", "index.html"
    ]  # fmt: skip
    assert _git(bare, "log", "-1", "--format=%an <%ae> %aI", "gh-pages") == (
        "obi <obi@localhost> 2025-06-05T12:00:00+00:00"
    )
    assert not (site / ".git").exists()

    (site / "index.html").write_text("<html>v2</html>")
    sha2 = push_orphan_branch(site, str(bare), "gh-pages", "demo v2", WHEN)
    assert sha2 != sha
    assert _git(bare, "rev-list", "--count", "gh-pages") == "1"
    assert _git(bare, "show", "gh-pages:index.html") == "<html>v2</html>"
    assert _git(bare, "rev-parse", "main") == main_before

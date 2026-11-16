"""FR-022: public files must not link into, or read from, an excluded path (T019)."""

from __future__ import annotations

from obi.core.identity.guard import scan_paths
from obi.core.identity.tokens import build_tokens

#: Assembled at run time so this tracked file has no literal path into the excluded dir.
SPECS = "spe" + "cs"

NO_PRIVATE = {"denylist": None, "mapping": None, "config": None}


def _links(tree, files: dict[str, str]):
    root = tree(files)
    result = scan_paths(None, root, build_tokens(**NO_PRIVATE), dictionary=frozenset())
    return [(f.file, f.locator) for f in result.fatal if f.category == "link"]


def test_markdown_link_into_specs_is_fatal(tree) -> None:
    hits = _links(
        tree,
        {
            "README.md": f"See [setup]({SPECS}/001-a/quickstart.md) and [docs](docs/x.md).\n",
            "docs/x.md": f"intro\n[deep](../{SPECS}/002-b/plan.md)\n",
        },
    )
    assert hits == [("README.md", "line 1"), ("docs/x.md", "line 2")]


def test_runtime_read_of_specs_is_fatal(tree) -> None:
    hits = _links(
        tree,
        {
            "tests/test_a.py": f'SCHEMA = ROOT / "{SPECS}" / "002-b" / "contracts" / "x.json"\n',
            "src/obi/b.py": f"path = '{SPECS}/088-radio/contracts/cli.md'\n",
        },
    )
    assert hits == [("src/obi/b.py", "line 1"), ("tests/test_a.py", "line 1")]


def test_prose_mentions_and_allowed_files_pass(tree) -> None:
    hits = _links(
        tree,
        {
            "src/obi/c.py": '"""Full contract: see specs/121-mesh/contracts/cli.md."""\n',
            "CLAUDE.md": f"read [the plan]({SPECS}/<feature>/plan.md)\n",
            ".specify/scripts/x.sh": "cat specs/001/plan.md\n",
        },
    )
    assert hits == []

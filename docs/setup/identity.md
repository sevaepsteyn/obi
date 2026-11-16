# Keeping deployment identity out of the repository (`obi identity`)

obi's source, tests and docs carry **no identity of any real deployment**: no addresses,
hostnames, device or site names, people, radio callsigns, chat IDs, coordinates or
event-specific endpoints. Everything deployment-specific lives in the operator's
`~/.config/obi/config.toml`, or in private state under `~/.local/state/obi/`.
Examples use the fictional **Example Valley Net**: tree-named sites (`aspen-gw`, `birch-ptp-cedar-60`),
documentation addresses (`192.0.2.0/24`) and `example.net`.

## The guard

`obi identity scan` checks every tracked file (except the excluded paths listed in
`identity-guard.toml`, each with its reason) and reports **file, line and category — never the
matched value**:

```sh
uv run obi identity scan                # with your private inputs, when present
uv run obi identity scan --no-private   # shape checks only — what a fresh clone enforces
uv run obi identity scan tests/fixtures --json
```

Two kinds of checks run:

- **Shape checks** need no private data: secret and key shapes, tailnet hostnames, chat-ID
  shapes, globally administered MACs, public addresses outside the documentation ranges,
  emails outside the example domains, and markdown links into excluded paths.
- **Private checks** run only on a machine that has the operator's private inputs. They read
  three sources, each owner-only (`chmod 600`):
  - the denylist (`~/.config/obi/demo-denylist.txt`; `!term` is fatal, `term` is a warning,
    `@word` is a fatal place word);
  - the demo mapping (`~/.local/state/obi/demo/mapping.json`, from `obi demo snapshot`);
  - the live config itself.

The test suite runs the guard over the whole tree (`tests/unit/test_identity_tree.py`). A commit
hook runs it over the lines a commit **adds**:

```sh
uv run obi identity install-hook        # once; covers every worktree of the repository
```

## Fixing a finding

`obi identity scrub` replaces real values with the private mapping's fictional ones. It
preserves structure (role tokens, digits, subnets, separators, case), archives each original
privately (`~/.local/state/obi/identity/archive/`), and re-scans its own output:

```sh
uv run obi identity scrub tests/fixtures/uisp --dry-run   # counts only
uv run obi identity scrub tests/fixtures/uisp
```

Radio and mesh callsigns are **hand-curated**, because fuzzy-matching tests depend on their
spelling: `obi identity callsign list | set REF FAKE | check`.

## Moving a value into config

If code needs a deployment-specific value, add a config key. Give it an empty, disabled or
fictional default, and when the feature can't run without it, turn the feature **off** with one
clear "not configured" line. Then add the key to the moved-value registry
(`src/obi/core/identity/moved.py`). `obi identity migrate-check` tells an operator which keys
their config still needs.

## Public export gate

A public export (a fresh-history copy of this tree) must run the guard first and refuse to
publish on any fatal finding:

```sh
uv run obi identity scan              # private mode: denylist, mapping and config all ✓
uv run obi identity scan --no-private # what a public clone enforces
```

Both must report `0 fatal`. The export also leaves out everything the guard excludes as
private-only (`specs/`), so a link into an excluded directory is itself a fatal finding. There is
no export command yet; this section is the contract it must follow.

#!/usr/bin/env python3
"""Sweep the stored radio history through feature 114's addressing rules (SC-004).

Every intelligible received transmission ever stored is replayed through the real
:class:`ExchangeTracker` and :func:`addressed_elsewhere`. What matters is the small
population whose behavior actually changes: transmissions that arrived INSIDE an open
call window and did not fire the trigger, because those are the ones obi would now stay
silent on. Anything on an idle channel was already silent, and anything that fired the
trigger is owned by the trigger.

Run it before deploying a change to the recognition rules. Exactly two rows are expected
— tx 3223 and tx 3228, the 2026-08-29 field incident. A third row means obi would go
silent on a caller it answers correctly today, and blocks the deploy until explained.

    uv run python scripts/radio_addressing_sweep.py

Read-only: the store is opened in SQLite read-only mode and nothing else is touched.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from obi.core.config import load_config
from obi.radio.exchange import ExchangeTracker, addressed_elsewhere


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", type=Path, default=None, help="radio.db path (default: from config)"
    )
    parser.add_argument(
        "--config", type=Path, default=None, help="config.toml path (default: standard)"
    )
    parser.add_argument(
        "--expect",
        type=int,
        default=None,
        help="exit non-zero unless exactly this many in-window rows are flagged",
    )
    args = parser.parse_args(argv)

    config_path = args.config or (Path.home() / ".config" / "obi" / "config.toml")
    settings = load_config(config_path).radio
    db_path = args.db or settings.db_path

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT id, ts_iso, call_id, trigger_fired, trigger_reason, transcript "
        "FROM transmissions WHERE kind='rx' AND stt_gate='ok' ORDER BY id"
    ).fetchall()

    # One tracker for the whole sweep, exactly as the daemon keeps one per process: the
    # alternation state is part of what is being tested.
    tracker = ExchangeTracker(
        roster=[*settings.operator_handles, *settings.known_callers],
        aliases=settings.caller_aliases,
        name_variants=settings.name_variants,
    )

    in_window: list[tuple[int, str, int, str | None, str, str | None, str | None, str]] = []
    already_silent = 0
    for row_id, ts_iso, call_id, fired, reason, transcript in rows:
        verdict = tracker.observe(
            started_us=0, ended_us=0, text=transcript, intelligible=True
        )
        if not addressed_elsewhere(verdict, settings.name_variants):
            continue
        if call_id is not None and not fired:
            in_window.append(
                (
                    row_id, ts_iso, call_id, reason,
                    verdict.form or "?", verdict.addressee, verdict.speaker, transcript,
                )
            )
        else:
            already_silent += 1

    print(f"intelligible receives swept:            {len(rows)}")
    print(f"directed at another station, no change: {already_silent}")
    print(f"directed at another station, IN WINDOW: {len(in_window)}")
    for row_id, ts_iso, call_id, reason, form, addressee, speaker, transcript in in_window:
        print(
            f"  tx {row_id}  {ts_iso}  call={call_id}  was={reason}  "
            f"{form} to={addressee!r} from={speaker!r}"
        )
        print(f"       {transcript[:100]!r}")

    if args.expect is not None and len(in_window) != args.expect:
        print(
            f"\nFAIL: expected {args.expect} in-window rows, found {len(in_window)}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

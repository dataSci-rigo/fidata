#!/usr/bin/env python3
"""fiData/weekly_review_nudge.py — SessionStart hook helper.

Prints a one-line reminder (as hook JSON on stdout) when the weekly review
artifact hasn't been republished in a while, and stays completely silent
otherwise. Offline and instant by design: it only stats a local marker file,
because a hook that phoned the VM would add seconds to every session start.

The marker (data/artifact_published.json) is written by the /weekly-review
skill after it publishes.
"""
import argparse
import json
import os
import sys
from datetime import date

DATA_DIR = os.path.dirname(os.path.abspath(__file__))
MARKER = os.path.join(DATA_DIR, 'data', 'artifact_published.json')
STALE_DAYS = 7


def message(marker: str = MARKER, stale_days: int = STALE_DAYS,
            today: date | None = None) -> str | None:
    today = today or date.today()
    try:
        with open(marker) as f:
            published = str(json.load(f).get('published_at') or '')
        age = (today - date.fromisoformat(published)).days
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 'fiData weekly review has never been published — run /weekly-review'
    if age >= stale_days:
        return (f'fiData weekly review not published in {age} days — '
                f'run /weekly-review')
    return None


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--marker', default=MARKER, help=argparse.SUPPRESS)
    ap.add_argument('--stale-days', type=int, default=STALE_DAYS,
                    help=f'nudge once the page is this old (default {STALE_DAYS})')
    args = ap.parse_args()

    # Hooks receive JSON on stdin; this one ignores it. Drain so the writer
    # never sees a broken pipe.
    if not sys.stdin.isatty():
        try:
            sys.stdin.read()
        except Exception:
            pass

    msg = message(args.marker, args.stale_days)
    if msg:
        print(json.dumps({'systemMessage': msg}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

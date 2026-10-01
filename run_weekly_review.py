#!/usr/bin/env python3
"""fiData/run_weekly_review.py — deeper structured AI review, run Sunday
afternoons (see REFACTOR_PLAN.md Phase 6). Sends a short Telegram digest
with links, and writes the full sections to data/weekly_review_<date>.json
for panel/fidata_routes.py to render at /fidata/.

The model comes from llm.py, so FIDATA_LLM_PROVIDER=openrouter routes this
job through OpenRouter instead of the Anthropic SDK. It also refreshes
data/weekly_input.json — the same facts bundle a Claude session reads when
you want the deeper, Opus-written version of this report.
"""
import argparse
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# run_pipeline loads .env (into os.environ) before its own imports, ahead of
# anything here — importing it first means ai_review/telegram_alert below
# see FI_BOT_ID/OWNER_CHAT_ID/ANTHROPIC_API_KEY correctly even though they
# also read os.environ at their own import time. Don't reorder these imports.
from run_pipeline import DATA_DIR, DATA_STATE_DIR, load_last_run
from ai_review import weekly_deep_review
from app_data_io import panel_url
from telegram_alert import send_telegram
import weekly_input


def _sector_summary_by_gics(combined) -> list[dict]:
    eq = combined[combined.index != 'cash']
    g = eq.groupby('Sector')['Market_Value'].sum().sort_values(ascending=False)
    return [{'Sector': sym, 'Total_Market_Value': f'${v:,.0f}'} for sym, v in g.items()]


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--no-telegram', action='store_true',
                    help="write the review but don't send the digest "
                         '(for re-running on demand without re-notifying)')
    ap.add_argument('--quiet', action='store_true',
                    help='suppress printing the digest to stdout')
    args = ap.parse_args()

    # Read the last pipeline run's output rather than triggering another full
    # refresh. Sector/Cap_Tier/Vol_Tier come straight from combined.json and
    # the MPT metrics are recomputed in-process from historical.csv (~0.05s),
    # so this needs no network at all.
    result = load_last_run()
    combined, sold_df, metrics = result['combined'], result['sold_df'], result['metrics']

    sector_data = {'by_gics': _sector_summary_by_gics(combined)}

    sections = weekly_deep_review(combined, sector_data, metrics, sold_df)

    today_str = str(date.today())
    out_path = os.path.join(DATA_STATE_DIR, f'weekly_review_{today_str}.json')
    os.makedirs(DATA_STATE_DIR, exist_ok=True)
    with open(out_path, 'w') as f:
        json.dump(sections, f, indent=2)

    # This job silently produced four empty sections for weeks: the model had
    # started writing markdown headers ("## Rebalancing:") that the section
    # parser didn't recognize, and "(no content)" x4 still exited 0. Say so
    # loudly instead of shipping a hollow report.
    if not any(sections.values()):
        print('ERROR: every section parsed empty — check ai_review.split_sections '
              'against the model output', file=sys.stderr)

    digest_lines = [f'Weekly portfolio review ({today_str}):']
    if not any(sections.values()):
        digest_lines.append('⚠ the review came back empty — section parsing '
                            'is broken, see the service log')
    for name, text in sections.items():
        first_line = text.splitlines()[0] if text else '(no content)'
        digest_lines.append(f'{name}: {first_line[:120]}')
    # Refresh the facts bundle the panel serves for download and a Claude
    # session reads for the Opus-written version of this report.
    try:
        bundle = weekly_input.build()
        with open(os.path.join(DATA_STATE_DIR, weekly_input.OUT_NAME), 'w') as f:
            json.dump(bundle, f, indent=2, default=str)
    except Exception as e:
        print(f'WARNING: could not write {weekly_input.OUT_NAME}: {e}',
              file=sys.stderr)

    digest_lines.append('')
    artifact = (os.getenv('FIDATA_ARTIFACT_URL') or '').strip()
    if artifact:
        digest_lines.append(f'Full report: {artifact}')
        digest_lines.append(f"Panel: {panel_url(f'fidata/{today_str}')}")
    else:
        digest_lines.append(f"Full report: {panel_url(f'fidata/{today_str}')}")
    digest = '\n'.join(digest_lines)

    if not args.no_telegram:
        send_telegram(digest)
    if not args.quiet:
        print(digest)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

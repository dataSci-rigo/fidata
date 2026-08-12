#!/usr/bin/env python3
"""fiData/run_weekly_review.py — deeper structured Claude review, run Sunday
afternoons (see REFACTOR_PLAN.md Phase 6). Sends a short Telegram digest
with a link, and writes the full sections to data/weekly_review_<date>.json
for panel/fidata_routes.py to render at /fidata/.
"""
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# run_pipeline loads .env (into os.environ) before its own imports, ahead of
# anything here — importing it first means ai_review/telegram_alert below
# see FI_BOT_ID/OWNER_CHAT_ID/ANTHROPIC_API_KEY correctly even though they
# also read os.environ at their own import time. Don't reorder these imports.
from run_pipeline import DATA_DIR, DATA_STATE_DIR, run as run_pipeline
from ai_review import weekly_deep_review
from telegram_alert import send_telegram

PANEL_URL = os.getenv('FIDATA_PANEL_URL', 'http://localhost:9000/fidata')


def _sector_summary_by_gics(combined) -> list[dict]:
    eq = combined[combined.index != 'cash']
    g = eq.groupby('Sector')['Market_Value'].sum().sort_values(ascending=False)
    return [{'Sector': sym, 'Total_Market_Value': f'${v:,.0f}'} for sym, v in g.items()]


if __name__ == '__main__':
    # run_pipeline()'s run() already computes Sector/Cap_Tier/Vol_Tier and MPT
    # metrics as part of its normal flow — reuse them rather than refetching.
    result = run_pipeline()
    combined, sold_df, metrics = result['combined'], result['sold_df'], result['metrics']

    sector_data = {'by_gics': _sector_summary_by_gics(combined)}

    sections = weekly_deep_review(combined, sector_data, metrics, sold_df)

    today_str = str(date.today())
    out_path = os.path.join(DATA_STATE_DIR, f'weekly_review_{today_str}.json')
    os.makedirs(DATA_STATE_DIR, exist_ok=True)
    with open(out_path, 'w') as f:
        json.dump(sections, f, indent=2)

    digest_lines = [f'Weekly portfolio review ({today_str}):']
    for name, text in sections.items():
        first_line = text.splitlines()[0] if text else '(no content)'
        digest_lines.append(f'{name}: {first_line[:120]}')
    digest_lines.append(f'\nFull report: {PANEL_URL}/{today_str}')
    digest = '\n'.join(digest_lines)

    send_telegram(digest)
    print(digest)

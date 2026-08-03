#!/usr/bin/env python3
"""fiData/run_pipeline.py — headless refresh + Telegram alerting.

Meant to run 3x/day via a systemd timer (see REFACTOR_PLAN.md Phase 6), not
as a long-running daemon. No interactive input anywhere in this path —
that's what bug fix #3 (removing the notebook's input() prompt) made
possible: infer_missing_trades() always logs unresolved trades to
unknown_trades.csv instead of blocking on a human.
"""
import os
import sys

import pandas as pd
from dotenv import dotenv_values

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from parsers import load_positions
from parsers.transactions import load_realized_lots, load_transactions
from analytics import (
    CUTOFF, DEFAULT_BUY, collect_snapshots, compute_cost_basis, export_app_data,
    infer_missing_trades, load_fallback_cost_basis, merge_accounts,
)
from enrich import earnings_and_recommendations, refresh_historical
from alerts import detect_alerts, save_snapshot
from telegram_alert import send_telegram

DATA_DIR = os.path.dirname(os.path.abspath(__file__))
ACCOUNTS_DIR = os.path.join(DATA_DIR, 'accounts')
BUYSELL_DIR = os.path.join(DATA_DIR, 'buysell')
PAST_DIR = os.path.join(DATA_DIR, 'past')
APP_DATA = os.path.join(DATA_DIR, 'app_data')
DATA_STATE_DIR = os.path.join(DATA_DIR, 'data')

HIST_FILE = os.path.join(DATA_DIR, 'historical.csv')
EARN_FILE = os.path.join(DATA_DIR, 'earnings.csv')
UNKNOWN_FILE = os.path.join(DATA_DIR, 'unknown_trades.csv')
UPGRADES_FILE = os.path.join(DATA_DIR, 'upgrades.csv')
LAST_SNAPSHOT_FILE = os.path.join(DATA_STATE_DIR, 'last_run_snapshot.csv')
ALERTED_EARNINGS_FILE = os.path.join(DATA_STATE_DIR, 'alerted_earnings.json')

EXCLUDE_FILES = {
    'earnings.csv', 'historical.csv', 'portfolio.csv', 'sectors.csv', 'file_clean.csv',
    'History_for_Account_226998197.csv', 'History_for_Account_236369828.csv',
}


def run() -> dict:
    os.makedirs(DATA_STATE_DIR, exist_ok=True)

    accounts = load_positions(ACCOUNTS_DIR, exclude=EXCLUDE_FILES)
    combined = merge_accounts(accounts)

    tx_df = load_transactions(BUYSELL_DIR)
    sold_df = load_realized_lots(BUYSELL_DIR, CUTOFF)
    fid_cost = load_fallback_cost_basis(ACCOUNTS_DIR)
    combined = compute_cost_basis(combined, tx_df, fid_cost, CUTOFF, DEFAULT_BUY)

    hist_df = refresh_historical(combined.index, HIST_FILE)

    snap_map = collect_snapshots(PAST_DIR, ACCOUNTS_DIR)
    inferred = infer_missing_trades(snap_map, tx_df, hist_df, UNKNOWN_FILE)
    if not inferred.empty:
        tx_df = pd.concat([tx_df, inferred], ignore_index=True).sort_values('Date').reset_index(drop=True)
        combined = compute_cost_basis(combined, tx_df, fid_cost, CUTOFF, DEFAULT_BUY)

    earn_cache, recs_df, _upgrades = earnings_and_recommendations(combined, EARN_FILE)
    for col in recs_df.columns:
        combined[col] = recs_df[col]

    export_app_data(APP_DATA, accounts, combined, earn_file=EARN_FILE, upgrades_file=UPGRADES_FILE)

    messages = detect_alerts(combined, LAST_SNAPSHOT_FILE, earn_cache, ALERTED_EARNINGS_FILE)
    for msg in messages:
        send_telegram(msg)

    save_snapshot(combined, LAST_SNAPSHOT_FILE)

    return {'combined': combined, 'hist_df': hist_df, 'tx_df': tx_df, 'sold_df': sold_df,
            'earn_cache': earn_cache, 'alerts_sent': messages}


if __name__ == '__main__':
    _env = dotenv_values(os.path.join(DATA_DIR, '.env'))
    for k, v in _env.items():
        os.environ.setdefault(k, v)

    result = run()
    print(f"Pipeline complete. {len(result['alerts_sent'])} alert(s) sent.")
    for m in result['alerts_sent']:
        print(f'  {m}')

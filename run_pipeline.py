#!/usr/bin/env python3
"""fiData/run_pipeline.py — headless refresh + Telegram alerting.

Meant to run 3x/day via a systemd timer (see REFACTOR_PLAN.md Phase 6), not
as a long-running daemon. No interactive input anywhere in this path —
that's what bug fix #3 (removing the notebook's input() prompt) made
possible: infer_missing_trades() always logs unresolved trades to
unknown_trades.csv instead of blocking on a human.
"""
import json
import os
import sys

# Must happen before any matplotlib.pyplot import (directly or via
# plotting.py) — headless run, no display available. plotting.py
# deliberately doesn't set this itself so the notebook's own Jupyter-provided
# interactive backend isn't clobbered when it imports the same module.
import matplotlib
matplotlib.use('Agg')

import pandas as pd
from dotenv import dotenv_values

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from parsers import load_positions
from parsers.transactions import load_realized_lots, load_transactions
from analytics import (
    CUTOFF, DEFAULT_BUY, capital_performance, closed_positions_summary,
    collect_snapshots, compute_cost_basis, correlation_matrix,
    efficient_frontier, export_app_data, high_correlation_pairs, infer_missing_trades,
    load_fallback_cost_basis, merge_accounts, mpt_metrics,
)
from enrich import (
    classify_sectors, earnings_and_recommendations, refresh_historical,
    risk_free_rate, symbol_metrics,
)
from plotting import save_correlation_heatmap_plot, save_efficient_frontier_plot, save_mpt_summary
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
SECTOR_CSV = os.path.join(DATA_DIR, 'sectors.csv')
UNKNOWN_FILE = os.path.join(DATA_DIR, 'unknown_trades.csv')
UPGRADES_FILE = os.path.join(DATA_DIR, 'upgrades.csv')
LAST_SNAPSHOT_FILE = os.path.join(DATA_STATE_DIR, 'last_run_snapshot.csv')
ALERTED_EARNINGS_FILE = os.path.join(DATA_STATE_DIR, 'alerted_earnings.json')
EFFICIENT_FRONTIER_PNG = os.path.join(DATA_DIR, 'efficient_frontier.png')
CORRELATION_HEATMAP_PNG = os.path.join(DATA_DIR, 'correlation_heatmap.png')
MPT_SUMMARY_FILE = os.path.join(DATA_STATE_DIR, 'mpt_summary.json')
PORTFOLIO_EXTRAS_FILE = os.path.join(DATA_STATE_DIR, 'portfolio_extras.json')
CORR_TOP_N = 25

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
    rf_annual = risk_free_rate()

    metrics_df = symbol_metrics(combined, hist_df, rf_annual)
    live_prices = metrics_df['Current_Price'].dropna()
    combined.loc[live_prices.index, 'Current_Price'] = live_prices
    combined.loc[combined.index != 'cash', 'Market_Value'] = (
        combined.loc[combined.index != 'cash', 'Quantity']
        * combined.loc[combined.index != 'cash', 'Current_Price'])
    for col in metrics_df.columns:
        if col != 'Current_Price':
            combined[col] = metrics_df[col]

    sector_df = classify_sectors(combined, SECTOR_CSV)
    for col in ('Quote_Type', 'Sector', 'MarketCap', 'Cap_Tier', 'Vol_Tier'):
        combined.loc[combined.index != 'cash', col] = sector_df[col]

    snap_map = collect_snapshots(PAST_DIR, ACCOUNTS_DIR)
    inferred = infer_missing_trades(snap_map, tx_df, hist_df, UNKNOWN_FILE)
    if not inferred.empty:
        tx_df = pd.concat([tx_df, inferred], ignore_index=True).sort_values('Date').reset_index(drop=True)
        combined = compute_cost_basis(combined, tx_df, fid_cost, CUTOFF, DEFAULT_BUY)

    earn_cache, recs_df, _upgrades = earnings_and_recommendations(combined, EARN_FILE)
    for col in recs_df.columns:
        combined[col] = recs_df[col]

    # MPT metrics computed here (before export_app_data) so Beta/Alpha_pct —
    # like Sector/Cap_Tier/Vol_Tier before it — actually make it into
    # combined.json instead of only existing inside this function's return
    # value. The notebook's cell 15 already did this merge; run_pipeline.py
    # previously didn't, so every headless run silently dropped these columns.
    metrics = mpt_metrics(combined, hist_df, rf_annual)
    ef = None
    if len(metrics['symbols']) >= 2 and 'beta_alpha' in metrics:
        ba_df = metrics['beta_alpha']
        combined.loc[ba_df.index, 'Beta'] = ba_df['Beta']
        combined.loc[ba_df.index, 'Alpha_pct'] = ba_df['Alpha_pct']

    export_app_data(APP_DATA, accounts, combined, earn_file=EARN_FILE, upgrades_file=UPGRADES_FILE)

    # Efficient frontier / correlation / "extras" — feeds the /positions panel
    # page. Computed here (not just in the notebook) so that page is
    # refreshed 3x/day like everything else, with no manual notebook run
    # required.
    if len(metrics['symbols']) >= 2:
        ef = efficient_frontier(metrics)
        corr, top_syms = correlation_matrix(combined, metrics['rets'], top_n=CORR_TOP_N)
        pairs = high_correlation_pairs(corr, top_syms)
        save_efficient_frontier_plot(metrics, ef, EFFICIENT_FRONTIER_PNG)
        save_correlation_heatmap_plot(corr, len(top_syms), CORRELATION_HEATMAP_PNG)
        save_mpt_summary(metrics, ef, pairs, MPT_SUMMARY_FILE)

    extras = {
        'closed_positions': closed_positions_summary(sold_df),
        'capital': capital_performance(combined, tx_df, sold_df),
        'risk_contributors': (
            metrics['risk_contrib'].nlargest(15, 'RiskContrib_pct').reset_index().to_dict('records')
            if len(metrics['symbols']) >= 2 else []),
        'max_sharpe_weights': (
            [{'Symbol': sym, 'Weight_pct': round(float(w), 2)}
             for sym, w in (ef['w_max_sharpe'] * 100).sort_values(ascending=False).items()
             if w > 1]
            if ef is not None else []),
    }
    os.makedirs(DATA_STATE_DIR, exist_ok=True)
    with open(PORTFOLIO_EXTRAS_FILE, 'w') as f:
        json.dump(extras, f, indent=2, default=str)

    messages = detect_alerts(combined, LAST_SNAPSHOT_FILE, earn_cache, ALERTED_EARNINGS_FILE)
    for msg in messages:
        send_telegram(msg)

    save_snapshot(combined, LAST_SNAPSHOT_FILE)

    return {'combined': combined, 'hist_df': hist_df, 'tx_df': tx_df, 'sold_df': sold_df,
            'earn_cache': earn_cache, 'alerts_sent': messages,
            'metrics': metrics, 'rf_annual': rf_annual}


if __name__ == '__main__':
    _env = dotenv_values(os.path.join(DATA_DIR, '.env'))
    for k, v in _env.items():
        os.environ.setdefault(k, v)

    result = run()
    print(f"Pipeline complete. {len(result['alerts_sent'])} alert(s) sent.")
    for m in result['alerts_sent']:
        print(f'  {m}')

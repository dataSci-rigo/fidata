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

from dotenv import dotenv_values

_DATA_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _DATA_DIR)

# Must happen before importing telegram_alert (which reads FI_BOT_ID/
# OWNER_CHAT_ID from os.environ at *module import time*) and before any
# matplotlib.pyplot import (directly or via plotting.py — headless run, no
# display available). Loading .env here, ahead of every local import, is
# what makes `python run_pipeline.py` work standalone; running under
# systemd works either way since EnvironmentFile= already populates
# os.environ before the interpreter even starts, which is why this bug
# went unnoticed — every local/manual run silently sent zero Telegram
# alerts despite FI_BOT_ID being set correctly in .env.
for _k, _v in dotenv_values(os.path.join(_DATA_DIR, '.env')).items():
    os.environ.setdefault(_k, _v)

# plotting.py deliberately doesn't set the backend itself so the notebook's
# own Jupyter-provided interactive backend isn't clobbered when it imports
# the same module.
import matplotlib
matplotlib.use('Agg')

import pandas as pd

from parsers import load_positions
from parsers.transactions import load_realized_lots, load_transactions
import splits
from analytics import (
    CUTOFF, DEFAULT_BUY, capital_performance, closed_positions_summary,
    collect_snapshots, compute_cost_basis, correlation_matrix, normalize_snapshots,
    efficient_frontier, export_app_data, high_correlation_pairs, infer_missing_trades,
    load_fallback_cost_basis, merge_accounts, mpt_metrics,
)
from enrich import (
    seed_splits,
    classify_sectors, earnings_and_recommendations, refresh_historical,
    risk_free_rate, symbol_metrics,
)
from plotting import save_correlation_heatmap_plot, save_efficient_frontier_plot, save_mpt_summary
from alerts import detect_alerts, detect_breakouts, save_snapshot
from market_data import load_watchlist
from telegram_alert import send_telegram

DATA_DIR = _DATA_DIR
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
LAST_ALERTS_FILE = os.path.join(DATA_STATE_DIR, 'last_alerts.json')
ALERTED_EARNINGS_FILE = os.path.join(DATA_STATE_DIR, 'alerted_earnings.json')
ALERTED_BREAKOUTS_FILE = os.path.join(DATA_STATE_DIR, 'alerted_breakouts.json')
WATCHLIST_FILE = os.path.join(DATA_DIR, 'watchlist.txt')
SPLITS_FILE = os.path.join(DATA_STATE_DIR, splits.CACHE_NAME)
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

    # Splits: broker exports are restated by the broker, so anything already in
    # an export is consistent — the exposure is any split between the export
    # date and now. Prices are refreshed live, so a stale share count silently
    # produces stale_qty x post-split price. Everything below is normalized
    # into today's share terms before it is used. See splits.py.
    split_table = splits.load_table(SPLITS_FILE)
    split_meta = splits.load_meta(SPLITS_FILE)
    all_symbols = [str(s) for s in merge_accounts(accounts).index if str(s) != 'cash']
    # Watchlist tickers ride the same refresh: refresh_historical backfills
    # 10y for any symbol not yet a historical.csv column, and the breakout
    # alert below screens all_symbols. Downstream portfolio metrics select
    # columns by combined.index, so the extra columns are inert there.
    all_symbols = sorted(set(all_symbols) | set(load_watchlist(WATCHLIST_FILE)))

    # Coverage must reach back to the oldest thing being normalized — the
    # cost-basis cutoff — because the incremental price refresh only downloads
    # dates *after* the last cached one, so its window can never contain a
    # split that already happened. That is exactly the case that matters.
    to_seed = splits.needs_seed(split_meta, all_symbols, CUTOFF)
    if to_seed:
        split_table = seed_splits(to_seed, CUTOFF.date(), split_table)
        split_meta = {'covered_from': str(CUTOFF.date()),
                      'symbols': sorted(set(split_meta.get('symbols') or []) | set(all_symbols))}

    hist_df, split_table = refresh_historical(all_symbols, HIST_FILE,
                                               split_table=split_table)
    splits.save_table(split_table, SPLITS_FILE)
    splits.save_meta(split_meta, SPLITS_FILE)

    as_of = splits.export_dates(ACCOUNTS_DIR, accounts, hist_df, split_table)
    accounts, split_factors, split_msgs = splits.adjust_positions(
        accounts, as_of, split_table, hist_df)
    for m in split_msgs:
        print(f'SPLIT: {m}')

    combined = merge_accounts(accounts)

    # Transactions and snapshots get the same treatment: pre-split fills would
    # otherwise average against post-split prices, and a split between two
    # snapshots looks exactly like a large unexplained BUY.
    tx_df = load_transactions(BUYSELL_DIR, split_table=split_table)
    sold_df = load_realized_lots(BUYSELL_DIR, CUTOFF, ACCOUNTS_DIR)
    fid_cost = load_fallback_cost_basis(ACCOUNTS_DIR)
    combined = compute_cost_basis(combined, tx_df, fid_cost, CUTOFF, DEFAULT_BUY)

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

    snap_map = normalize_snapshots(collect_snapshots(PAST_DIR, ACCOUNTS_DIR), split_table)
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
    #
    # Split_Factor is a visible marker: 1.0 for the untouched majority, the
    # applied ratio for any row whose share count came from us rather than
    # straight off the broker export.
    combined['Split_Factor'] = [float(split_factors.get(str(s), 1.0)) for s in combined.index]

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

    messages = detect_alerts(combined, LAST_SNAPSHOT_FILE, earn_cache,
                             ALERTED_EARNINGS_FILE, split_table=split_table)
    messages.extend(detect_breakouts(hist_df, all_symbols, ALERTED_BREAKOUTS_FILE))
    # A split means the export is stale — worth telling you, since the fix is
    # to re-download it and only you can do that.
    messages.extend(f'⚠ {m}' for m in split_msgs)
    for msg in messages:
        send_telegram(msg)

    # Persist what was sent so run_daily_review.py can report on this run
    # without re-running the whole pipeline for it.
    with open(LAST_ALERTS_FILE, 'w') as f:
        json.dump({'run_at': pd.Timestamp.now().isoformat(), 'messages': messages}, f, indent=2)

    save_snapshot(combined, LAST_SNAPSHOT_FILE)

    return {'combined': combined, 'hist_df': hist_df, 'tx_df': tx_df, 'sold_df': sold_df,
            'earn_cache': earn_cache, 'alerts_sent': messages,
            'metrics': metrics, 'rf_annual': rf_annual}


def load_last_run() -> dict:
    """Same shape as run(), rebuilt from what the last run left on disk.

    The review jobs used to call run() themselves, so the systemd schedule was
    doing 5 full yfinance-heavy refreshes a day (6 on Sunday) instead of 3 —
    each one also re-sending alerts and rewriting last_run_snapshot.csv, which
    made "alerts since last check" mean something different depending on which
    job happened to run. Everything below is read from local files or computed
    in-process; there is no network call in this path.

    Raises FileNotFoundError if the pipeline has never run.
    """
    combined_path = os.path.join(APP_DATA, 'combined.json')
    if not os.path.exists(combined_path):
        raise FileNotFoundError(
            f'{combined_path} not found — run run_pipeline.py before a review job')

    with open(combined_path) as f:
        combined = pd.DataFrame(json.load(f)).set_index('Symbol')
    for col in combined.columns:
        if col not in ('Cost_Basis_Source', 'Quote_Type', 'Sector', 'Cap_Tier',
                       'Vol_Tier', 'Consensus', 'First_Buy_Date'):
            combined[col] = pd.to_numeric(combined[col], errors='ignore')

    if os.path.exists(EARN_FILE):
        earn_cache = pd.read_csv(EARN_FILE, index_col='Symbol', parse_dates=['Next_Earnings'])
    else:
        earn_cache = pd.DataFrame(columns=['Next_Earnings', 'EPS_Est',
                                            'Rev_Est_High', 'Rev_Est_Low'])
        earn_cache.index.name = 'Symbol'

    hist_df = pd.read_csv(HIST_FILE, index_col=0, parse_dates=True) if os.path.exists(HIST_FILE) \
        else pd.DataFrame()

    # rf comes off the last run's summary rather than ^IRX, to keep this path
    # offline; the fallback matches enrich.risk_free_rate()'s own.
    rf_annual = 0.043
    if os.path.exists(MPT_SUMMARY_FILE):
        with open(MPT_SUMMARY_FILE) as f:
            rf_annual = json.load(f).get('rf_annual', rf_annual)

    metrics = {}
    if not hist_df.empty:
        try:
            metrics = mpt_metrics(combined, hist_df, rf_annual)
        except Exception as e:
            print(f'WARNING: could not recompute MPT metrics from cache: {e}')

    alerts_sent = []
    if os.path.exists(LAST_ALERTS_FILE):
        with open(LAST_ALERTS_FILE) as f:
            alerts_sent = json.load(f).get('messages', [])

    split_table = splits.load_table(SPLITS_FILE)
    return {'combined': combined, 'hist_df': hist_df,
            'tx_df': load_transactions(BUYSELL_DIR, split_table=split_table),
            'sold_df': load_realized_lots(BUYSELL_DIR, CUTOFF, ACCOUNTS_DIR),
            'earn_cache': earn_cache, 'alerts_sent': alerts_sent,
            'metrics': metrics, 'rf_annual': rf_annual}


if __name__ == '__main__':
    result = run()
    print(f"Pipeline complete. {len(result['alerts_sent'])} alert(s) sent.")
    for m in result['alerts_sent']:
        print(f'  {m}')

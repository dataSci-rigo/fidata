#!/usr/bin/env python3
"""fiData/refresh_universe.py — build/refresh the discovery-screener universe.

Fetches S&P 500 + Nasdaq-100 constituents (Wikipedia, with committed
fallbacks) plus universe/etf_list.txt, downloads 2y of daily closes/volume in
batches, computes per-symbol metrics (vol, Sharpe, drawdown, trend, beta,
correlation to YOUR portfolio), refreshes a throttled Ticker.info cache, and
merges everything into data/universe/universe.csv — the table the local
viewer's /discover page filters.

Modes:
  (default)      quick — cached constituents, incremental prices, info only
                 for symbols missing from the cache
  --full         live constituents + info refresh for entries older than
                 --max-info-age days (first full run ~20-30 min; weekly runs
                 mostly prices)

Runs on the laptop only (systemd user timer: systemd/user/fidata-universe.*).
Never touches historical.csv, watchlist.txt, or any pipeline state.
"""
import argparse
import json
import os
import sys
import traceback

from dotenv import dotenv_values

_DATA_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _DATA_DIR)

for _k, _v in dotenv_values(os.path.join(_DATA_DIR, '.env')).items():
    os.environ.setdefault(_k, _v)

import pandas as pd

import universe_data as ud
from screener import RF_FALLBACK

MPT_SUMMARY_FILE = os.path.join(_DATA_DIR, 'data', 'mpt_summary.json')
COMBINED_FILE = os.path.join(_DATA_DIR, 'app_data', 'combined.json')


def _rf_annual() -> float:
    try:
        with open(MPT_SUMMARY_FILE) as f:
            return float(json.load(f).get('rf_annual', RF_FALLBACK))
    except (OSError, ValueError, json.JSONDecodeError):
        return RF_FALLBACK


def _portfolio_weights() -> pd.Series:
    """Market_Value per held symbol from the pipeline's combined.json —
    missing artifacts just mean Corr_Portfolio stays NaN."""
    try:
        with open(COMBINED_FILE) as f:
            rows = json.load(f)
        w = {r['Symbol']: float(r.get('Market_Value') or 0)
             for r in rows if r.get('Symbol') and r['Symbol'] != 'cash'}
        return pd.Series(w, dtype=float)
    except (OSError, ValueError, json.JSONDecodeError):
        return pd.Series(dtype=float)


def run(args) -> int:
    universe_dir = args.universe_dir
    status_file = args.status_file or os.path.join(universe_dir, ud.STATUS_FILE)
    os.makedirs(universe_dir, exist_ok=True)

    def status(**kw):
        ud.write_status(status_file, **kw)

    mode = 'full' if args.full else 'quick'
    status(phase='constituents', done=0, total=0, mode=mode, error=None,
           message='resolving universe membership', finished_at=None,
           started_at=pd.Timestamp.now(tz='UTC').isoformat(), pid=os.getpid())
    try:
        cons = ud.build_constituents(universe_dir=universe_dir, live=args.full)
        symbols = list(cons['Symbol'])
        print(f'universe: {len(symbols)} symbols')

        if args.info_only:
            closes = ud._read_wide(os.path.join(universe_dir, ud.CLOSES_FILE))
            volumes = ud._read_wide(os.path.join(universe_dir, ud.VOLUME_FILE))
        else:
            status(phase='prices', message='downloading prices',
                   done=0, total=len(symbols))
            closes, volumes = ud.refresh_prices(
                symbols, universe_dir=universe_dir,
                status_cb=lambda **kw: status(phase='prices', **kw))

        status(phase='metrics', message='computing metrics')
        from market_data import load_history
        try:
            hist_df = load_history()
        except FileNotFoundError:
            hist_df = pd.DataFrame()
        port_rets = ud.portfolio_daily_returns(hist_df, _portfolio_weights())
        if port_rets is None:
            print('NOTE: no portfolio history/weights — Corr_Portfolio will be blank')
        metrics = ud.compute_metrics(closes, volumes, port_rets, _rf_annual())

        cache_path = os.path.join(universe_dir, ud.INFO_CACHE_FILE)
        if args.prices_only:
            cache = ud.load_info_cache(cache_path)
        else:
            cache = ud.load_info_cache(cache_path)
            plan = ud.info_fetch_plan(cache, symbols, full=args.full,
                                      max_age_days=args.max_info_age,
                                      max_n=args.max_info)
            print(f'info: fetching {len(plan)} of {len(symbols)} symbols')
            status(phase='info', done=0, total=len(plan),
                   message='fetching fundamentals')
            cache = ud.refresh_info(
                plan, cache_path, sleep=args.sleep,
                status_cb=lambda **kw: status(phase='info', **kw))

        status(phase='treasuries', message='fetching treasury yields')
        ud.fetch_treasury_yields(os.path.join(universe_dir, ud.TREASURY_FILE))

        status(phase='merge', message='writing universe.csv')
        df = ud.build_universe(cons, metrics, cache,
                               os.path.join(universe_dir, ud.UNIVERSE_FILE))
        n_info = int((df['Name'].fillna('') != '').sum())
        print(f'universe.csv: {len(df)} rows, {n_info} with fundamentals')
        status(phase='done', message=f'{len(df)} symbols',
               finished_at=pd.Timestamp.now(tz='UTC').isoformat())
        return 0
    except Exception as e:
        traceback.print_exc()
        status(phase='error', error=str(e),
               finished_at=pd.Timestamp.now(tz='UTC').isoformat())
        return 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--full', action='store_true',
                    help='live constituents fetch + refresh stale fundamentals')
    ap.add_argument('--prices-only', action='store_true',
                    help='skip the .info pass entirely')
    ap.add_argument('--info-only', action='store_true',
                    help='skip the price download (reuse cached closes)')
    ap.add_argument('--max-info', type=int, default=None, metavar='N',
                    help='cap the .info fetch at N symbols (oldest first)')
    ap.add_argument('--max-info-age', type=float, default=7, metavar='DAYS',
                    help='with --full, refetch info older than this (default 7)')
    ap.add_argument('--sleep', type=float, default=0.4,
                    help='seconds between .info calls (default 0.4)')
    ap.add_argument('--status-file', default=None,
                    help='progress JSON path (default data/universe/universe_status.json)')
    ap.add_argument('--universe-dir', default=ud.UNIVERSE_DIR,
                    help=argparse.SUPPRESS)
    return run(ap.parse_args())


if __name__ == '__main__':
    sys.exit(main())

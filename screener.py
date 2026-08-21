#!/usr/bin/env python3
"""52-week-high breakout screener — revival of projects/turtle_tbt.

Pure functions over a wide close-price frame (historical.csv shape) plus an
argparse CLI. The Telegram alert path lives in alerts.detect_breakouts(),
which calls scan() with near_high=NEAR_HIGH_DEFAULT and tiers the results.

Fixes two bugs from turtle_tbt/pull_data.py:
- cumulative returns used `1 + r.cumsum()` → now `(1 + r).cumprod()`
- Sharpe subtracted a 2% *annual* risk-free rate from a *daily* mean with no
  annualization → now matches enrich.symbol_metrics's convention:
  (r.mean() - rf_annual/252) / r.std() * sqrt(252)
"""
import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

NEAR_HIGH_DEFAULT = 0.85   # candidate band floor (notebook's ratio)
BREAKOUT_LEVEL = 1.0       # >= prior 252d max = true breakout
LOOKBACK = 252
RF_FALLBACK = 0.043        # matches enrich.risk_free_rate()'s fallback


def ema_trend(close: pd.Series) -> tuple[float, float, bool]:
    """(EMA50, EMA200, close > EMA50 > EMA200). min_periods=span so a short
    history yields NaN/False instead of the garbage EMAs the old notebook's
    bare ewm() produced."""
    ema50 = close.ewm(span=50, min_periods=50).mean().iloc[-1]
    ema200 = close.ewm(span=200, min_periods=200).mean().iloc[-1]
    last = close.iloc[-1]
    ok = bool(pd.notna(ema50) and pd.notna(ema200) and last > ema50 > ema200)
    return float(ema50), float(ema200), ok


def perf_metrics(close: pd.Series, rf_annual: float = RF_FALLBACK) -> dict:
    r = close.pct_change().dropna()
    if len(r) < 20 or r.std() == 0:
        return {'Sharpe': float('nan'), 'Max_Drawdown': float('nan')}
    cum = (1 + r).cumprod()
    max_dd = float((cum / cum.cummax() - 1).min())
    sharpe = float((r.mean() - rf_annual / 252) / r.std() * np.sqrt(252))
    return {'Sharpe': round(sharpe, 3), 'Max_Drawdown': round(max_dd, 4)}


def scan(hist_df: pd.DataFrame, symbols: list[str] | None = None,
         near_high: float = NEAR_HIGH_DEFAULT, lookback: int = LOOKBACK,
         rf_annual: float = RF_FALLBACK) -> pd.DataFrame:
    """Screen for symbols at/near their prior 52-week high in an uptrend.

    high_52w is the PRIOR lookback max (excludes the latest close), so
    Ratio >= 1.0 means today printed a fresh 52-week high. Symbols with fewer
    than `lookback` non-NaN closes are skipped.

    Returns a frame indexed by Symbol, sorted by Ratio desc:
    [Close, High_52w, Ratio, EMA50, EMA200, Breakout, Sharpe, Max_Drawdown]
    """
    if symbols is None:
        symbols = list(hist_df.columns)
    rows = {}
    for sym in symbols:
        if sym not in hist_df.columns:
            continue
        close = hist_df[sym].dropna()
        if len(close) < lookback:
            continue
        high_52w = float(close.iloc[-(lookback + 1):-1].max())
        if high_52w <= 0:
            continue
        last = float(close.iloc[-1])
        ratio = last / high_52w
        if ratio < near_high:
            continue
        ema50, ema200, trend_ok = ema_trend(close)
        if not trend_ok:
            continue
        rows[sym] = {
            'Close': round(last, 2), 'High_52w': round(high_52w, 2),
            'Ratio': round(ratio, 4), 'EMA50': round(ema50, 2),
            'EMA200': round(ema200, 2), 'Breakout': ratio >= BREAKOUT_LEVEL,
            **perf_metrics(close, rf_annual),
        }
    out = pd.DataFrame.from_dict(rows, orient='index')
    out.index.name = 'Symbol'
    if not out.empty:
        out = out.sort_values('Ratio', ascending=False)
    return out


def _universe_from_file(path: str) -> list[str]:
    """Ticker list from an .xlsx/.csv — 'Stocks' column if present (the old
    notebook's NASDAQ.xlsx convention), else the first column."""
    df = pd.read_excel(path) if path.endswith('.xlsx') else pd.read_csv(path)
    col = 'Stocks' if 'Stocks' in df.columns else df.columns[0]
    return [str(s).strip().upper() for s in df[col].dropna() if str(s).strip()]


def _filter_fundamentals(df: pd.DataFrame) -> pd.DataFrame:
    """Keep rows with profitMargins > 0 via yf.Ticker().info — one slow,
    rate-limit-prone request per ticker. CLI-only by design."""
    import yfinance as yf
    from enrich import yf_symbol
    keep = []
    for sym in df.index:
        try:
            pm = yf.Ticker(yf_symbol(sym)).info.get('profitMargins')
        except Exception as e:
            print(f'  {sym}: fundamentals lookup failed ({e}) — keeping')
            keep.append(sym)
            continue
        if pm is None or pm > 0:
            keep.append(sym)
        else:
            print(f'  {sym}: dropped (profitMargins {pm:.1%})')
    return df.loc[keep]


def main() -> None:
    import market_data

    ap = argparse.ArgumentParser(description='52-week-high breakout screener')
    ap.add_argument('--universe', default='holdings+watchlist',
                    help="holdings | history | watchlist | holdings+watchlist "
                         "| path to .xlsx/.csv ticker list (default: holdings+watchlist)")
    ap.add_argument('--near-high', type=float, default=NEAR_HIGH_DEFAULT,
                    help=f'minimum ratio vs prior 52-wk high (default {NEAR_HIGH_DEFAULT})')
    ap.add_argument('--fetch-missing', action='store_true',
                    help='yfinance-fetch tickers absent from historical.csv (network)')
    ap.add_argument('--fundamentals', action='store_true',
                    help='drop tickers with negative profit margins (slow: one '
                         'yfinance .info call per surviving ticker)')
    ap.add_argument('--out', help='write CSV here instead of printing')
    args = ap.parse_args()

    hist_df = market_data.load_history()
    if args.universe == 'history':
        symbols = list(hist_df.columns)
    elif args.universe == 'holdings':
        symbols = market_data.holdings_symbols()
    elif args.universe == 'watchlist':
        symbols = market_data.load_watchlist()
    elif args.universe == 'holdings+watchlist':
        symbols = sorted(set(market_data.holdings_symbols())
                         | set(market_data.load_watchlist()))
    else:
        symbols = _universe_from_file(args.universe)

    closes = market_data.get_closes(symbols, hist_df, fetch_missing=args.fetch_missing)
    missing = sorted(set(symbols) - set(closes.columns))
    if missing:
        note = 'fetched none of' if not args.fetch_missing else 'no data for'
        print(f'# {note} {len(missing)} ticker(s) not in historical.csv'
              + ('' if args.fetch_missing else ' (use --fetch-missing)')
              + f': {", ".join(missing[:15])}{"..." if len(missing) > 15 else ""}')

    result = scan(closes, near_high=args.near_high)
    if args.fundamentals and not result.empty:
        result = _filter_fundamentals(result)

    if args.out:
        result.to_csv(args.out)
        print(f'{len(result)} row(s) -> {args.out}')
    elif result.empty:
        print('No symbols at/near their 52-week high with trend intact.')
    else:
        print(result.to_string())


if __name__ == '__main__':
    main()

"""Shared read-side data adapter over historical.csv + parsers.

Used by screener.py (52-week-high scans/alerts) and by
projects/turtle_trading_bt/make_data.py (Turtle backtester data files).
fiData is a flat module dir, so external consumers do
`sys.path.insert(0, '/home/ai1/Documents/fiData')` before importing this —
same trick run_pipeline.py uses on itself.
"""
import os
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

DATA_DIR = os.path.dirname(os.path.abspath(__file__))
HIST_FILE = os.path.join(DATA_DIR, 'historical.csv')
ACCOUNTS_DIR = os.path.join(DATA_DIR, 'accounts')
WATCHLIST_FILE = os.path.join(DATA_DIR, 'watchlist.txt')
OHLC_CACHE_DIR = os.path.join(DATA_DIR, 'data', 'ohlc')

ASIA_INDICES = [('Nikkei', '^N225'), ('Hang Seng', '^HSI'),
                ('Shanghai', '000001.SS'), ('KOSPI', '^KS11')]

OHLC_COLS = ['Open', 'High', 'Low', 'Close', 'Volume']
# Close divergence vs historical.csv beyond this (median, relative) usually
# means a split/adjustment mismatch between the two sources.
CROSS_CHECK_TOL = 0.01


def us_market_open(now: datetime | None = None) -> bool:
    """Regular NYSE session (Mon–Fri 9:30–16:00 ET), with grace to 16:15 so
    the pipeline run scheduled at the closing bell still counts as market
    data. Holidays are not modeled — a holiday run just compares unchanged
    prices and alerts nothing."""
    now = now or datetime.now(ZoneInfo('America/New_York'))
    if now.weekday() >= 5:
        return False
    return dtime(9, 30) <= now.time() < dtime(16, 15)


def asia_open_summary() -> str | None:
    """One-line % change for the major Asian indices, for the Sunday-night
    run once their Monday sessions have opened. None if nothing fetched."""
    import yfinance as yf
    parts = []
    for name, tkr in ASIA_INDICES:
        try:
            fi = yf.Ticker(tkr).fast_info
            last, prev = fi.last_price, fi.previous_close
            if last and prev:
                parts.append(f'{name} {(last - prev) / prev:+.1%}')
        except Exception as e:
            print(f'asia open: {tkr} failed: {e}')
    return '🌏 Asia open: ' + ', '.join(parts) if parts else None


def load_history(hist_file: str = HIST_FILE) -> pd.DataFrame:
    """Wide close-price frame: tz-naive sorted DatetimeIndex, one column per symbol."""
    hist_df = pd.read_csv(hist_file, index_col=0, parse_dates=True)
    hist_df.index.name = 'Date'
    if getattr(hist_df.index, 'tz', None) is not None:
        hist_df.index = hist_df.index.tz_localize(None)
    return hist_df.sort_index()


def holdings_symbols(accounts_dir: str = ACCOUNTS_DIR,
                     exclude: set[str] | None = None) -> list[str]:
    """Currently held symbols from the broker exports (no 'cash')."""
    from parsers import load_positions
    from analytics import merge_accounts
    if exclude is None:
        from run_pipeline import EXCLUDE_FILES as exclude
    accounts = load_positions(accounts_dir, exclude=exclude)
    return [str(s) for s in merge_accounts(accounts).index if str(s) != 'cash']


def load_watchlist(path: str = WATCHLIST_FILE) -> list[str]:
    """One ticker per line; '#' starts a comment; [] if the file is missing."""
    if not os.path.exists(path):
        return []
    symbols = []
    with open(path) as f:
        for line in f:
            sym = line.split('#', 1)[0].strip().upper()
            if sym:
                symbols.append(sym)
    return symbols


def get_closes(symbols: list[str], hist_df: pd.DataFrame | None = None,
               fetch_missing: bool = False, years: int = 10) -> pd.DataFrame:
    """Close columns for `symbols`, from historical.csv where present.

    With fetch_missing=True the absent symbols come from one batched
    yf.download via enrich._download_closes (BRK-B mapping and single-vs-multi
    column handling live there); nothing is written back to historical.csv —
    the pipeline's refresh_historical owns that file.
    """
    if hist_df is None:
        hist_df = load_history()
    present = [s for s in symbols if s in hist_df.columns]
    out = hist_df[present].copy()

    missing = [s for s in symbols if s not in hist_df.columns]
    if missing and fetch_missing:
        from enrich import _download_closes
        start = str(date.today() - timedelta(days=365 * years + 30))
        end = str(date.today() + timedelta(days=1))
        closes, _splits = _download_closes(missing, start, end)
        for sym, series in closes.items():
            out = out.reindex(out.index.union(series.index))
            out[sym] = series
    return out.sort_index()


def _cross_check_close(symbol: str, ohlc: pd.DataFrame, hist_file: str) -> None:
    """Warn when the OHLC Close diverges from historical.csv's column —
    a >1% median gap usually means split/adjustment drift between sources."""
    try:
        hist_df = load_history(hist_file)
    except FileNotFoundError:
        return
    if symbol not in hist_df.columns:
        return
    ref = hist_df[symbol].dropna()
    got = ohlc.set_index('Date')['Close'] if 'Date' in ohlc.columns else ohlc['Close']
    got.index = pd.DatetimeIndex(got.index)
    overlap = ref.index.intersection(got.index)
    if len(overlap) < 20:
        return
    div = ((got.loc[overlap] / ref.loc[overlap]) - 1).abs().median()
    if div > CROSS_CHECK_TOL:
        print(f'WARNING: {symbol} OHLC close diverges from historical.csv '
              f'(median {div:.1%}) — check split/adjustment handling')


def get_daily_ohlc(symbol: str, years: int = 10,
                   cache_dir: str = OHLC_CACHE_DIR, refresh: bool = True,
                   hist_file: str = HIST_FILE) -> pd.DataFrame:
    """Daily OHLCV for one symbol: columns Date,Open,High,Low,Close,Volume.

    Cached at {cache_dir}/{sym}_1d.csv with an incremental tail fetch
    (mirrors refresh_historical's last_date+1 logic, single symbol).
    auto_adjust=True to match historical.csv's adjusted closes.
    """
    import yfinance as yf
    from enrich import yf_symbol

    os.makedirs(cache_dir, exist_ok=True)
    cache_file = os.path.join(cache_dir, f'{yf_symbol(symbol)}_1d.csv')
    today = date.today()
    start_full = today - timedelta(days=365 * years + 30)

    cached = pd.DataFrame()
    if os.path.exists(cache_file):
        cached = pd.read_csv(cache_file, parse_dates=['Date'])

    fetch_start = start_full
    if not cached.empty:
        last_date = cached['Date'].max().date()
        fetch_start = last_date + timedelta(days=1)

    if refresh and fetch_start <= today:
        raw = yf.download(yf_symbol(symbol), start=str(fetch_start),
                          end=str(today + timedelta(days=1)), interval='1d',
                          auto_adjust=True, progress=False)
        if raw is not None and not raw.empty:
            if isinstance(raw.columns, pd.MultiIndex):
                raw.columns = raw.columns.get_level_values(0)
            idx = pd.DatetimeIndex(raw.index)
            if idx.tz is not None:
                idx = idx.tz_localize(None)
            raw.index = idx.normalize()
            new = raw[OHLC_COLS].reset_index().rename(columns={'index': 'Date'})
            new.columns = ['Date'] + OHLC_COLS
            cached = (pd.concat([cached, new], ignore_index=True)
                      .drop_duplicates(subset='Date', keep='last')
                      .sort_values('Date').reset_index(drop=True))
            cached.to_csv(cache_file, index=False)

    if cached.empty:
        raise ValueError(f'no daily OHLC data available for {symbol}')

    _cross_check_close(symbol, cached, hist_file)
    return cached


def get_hourly_ohlc(symbol: str, days: int = 729,
                    cache_dir: str = OHLC_CACHE_DIR) -> pd.DataFrame | None:
    """Hourly bars via yfinance (max ~730 days back). Columns
    Datetime,Open,High,Low,Close. Returns None on failure — callers degrade
    to daily-only stop checking."""
    import yfinance as yf
    from enrich import yf_symbol

    try:
        raw = yf.download(yf_symbol(symbol), period=f'{days}d', interval='1h',
                          auto_adjust=True, progress=False)
    except Exception as e:
        print(f'WARNING: hourly fetch failed for {symbol}: {e}')
        return None
    if raw is None or raw.empty:
        return None
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    idx = pd.DatetimeIndex(raw.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    raw.index = idx
    out = raw[['Open', 'High', 'Low', 'Close']].reset_index()
    out.columns = ['Datetime', 'Open', 'High', 'Low', 'Close']
    return out


def to_turtle_frames(daily: pd.DataFrame, hourly: pd.DataFrame | None = None
                     ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Shape (daily, hourly) into turtle_trading_bt/backtest.py's two files.

    df_1d: Date ('YYYY-MM-DD' str), Open, High, Low, Close
    df_1h: Date (same day-string key), Date_1h, Open_1h, Close_1h

    hourly=None degrades to FOUR pseudo-ticks per daily row, walking the bar
    Open -> High -> Low -> Close. One tick per day cannot work here: the
    entry test compares the previous tick's close against the prior 20-day
    max of daily Highs, and with close-only ticks yesterday's close can never
    exceed a window that contains yesterday's own High — zero trades, ever.
    The OHLC walk lets intraday breakouts and stop hits register. Caveats:
    the high-before-low ordering is assumed (unknowable from daily bars), and
    fills happen at tick midpoints rather than true intrabar prices.
    """
    df_1d = daily.copy()
    df_1d['Date'] = pd.to_datetime(df_1d['Date']).dt.strftime('%Y-%m-%d')
    df_1d = df_1d[['Date', 'Open', 'High', 'Low', 'Close']].reset_index(drop=True)

    if hourly is None:
        # Pseudo-times keep Date_1h parseable (for plotly / trade logs) and
        # ordered within the day.
        ticks = [('Open', '09:30:00', 'Open'), ('High', '11:00:00', 'Open'),
                 ('Low', '13:00:00', 'High'), ('Close', '16:00:00', 'Low')]
        parts = []
        for point, hhmmss, prev_point in ticks:
            parts.append(pd.DataFrame({
                'Date': df_1d['Date'],
                'Date_1h': df_1d['Date'] + ' ' + hhmmss,
                'Open_1h': df_1d[prev_point],
                'Close_1h': df_1d[point],
            }))
        df_1h = (pd.concat(parts, ignore_index=True)
                 .sort_values('Date_1h', kind='stable')
                 .reset_index(drop=True))
    else:
        h = hourly.copy()
        h['Datetime'] = pd.to_datetime(h['Datetime'])
        h['Date'] = h['Datetime'].dt.strftime('%Y-%m-%d')
        # Hourly rows on days the daily frame lacks (half-sessions, partial
        # days at the range edges) would merge to NaN indicators — drop them.
        h = h[h['Date'].isin(set(df_1d['Date']))]
        df_1h = pd.DataFrame({
            'Date': h['Date'],
            'Date_1h': h['Datetime'].dt.strftime('%Y-%m-%d %H:%M:%S'),
            'Open_1h': h['Open'],
            'Close_1h': h['Close'],
        }).reset_index(drop=True)
    return df_1d, df_1h

"""yfinance-backed enrichment: price history, per-symbol metrics, sector/cap/vol
classification, analyst targets, earnings & recommendations.

Replaces notebook cells 4, 8, 9, 10. All caching (historical.csv, sectors.csv,
earnings.csv) is preserved exactly so viewer.py/viewer_app.py and the
existing cache files keep working unmodified.
"""
import re
import warnings
from datetime import date, timedelta

import numpy as np
import pandas as pd
import yfinance as yf

import splits

warnings.filterwarnings('ignore')

_TICKER_RE = re.compile(r'^[A-Z]{1,6}(-[A-Z])?$')

SECTOR_MAP = {
    'Technology': 'Information Technology',
    'Financial Services': 'Financials',
    'Consumer Cyclical': 'Consumer Discretionary',
    'Consumer Defensive': 'Consumer Staples',
    'Basic Materials': 'Materials',
    'Communication Services': 'Communication Services',
    'Healthcare': 'Healthcare',
    'Industrials': 'Industrials',
    'Energy': 'Energy',
    'Real Estate': 'Real Estate',
    'Utilities': 'Utilities',
}

ETF_SECTOR_KEYWORDS = {
    'Technology': 'Information Technology',
    'Financial': 'Financials',
    'Health': 'Healthcare',
    'Energy': 'Energy',
    'Real Estate': 'Real Estate',
    'Consumer': 'Consumer Discretionary',
    'Industrial': 'Industrials',
    'Utilities': 'Utilities',
    'Communication': 'Communication Services',
    'Materials': 'Materials',
    'Europe': 'International',
    'International': 'International',
    'Global': 'International',
    'Emerging': 'International',
    'Large Blend': 'Broad Market',
    'Large Growth': 'Broad Market',
    'Large Value': 'Broad Market',
    'Mid': 'Broad Market',
    'Small': 'Broad Market',
    'Multi-Asset': 'Broad Market',
    'Allocation': 'Broad Market',
    'Bond': 'Fixed Income',
    'High Yield': 'Fixed Income',
    'Gold': 'Commodities',
    'Commodity': 'Commodities',
}

LARGE_CAP = 10e9
MID_CAP = 2e9
HIGH_VOL = 0.35
LOW_VOL = 0.18

DEFAULT_ETF_SKIP = {
    'POCT', 'QTOP', 'VGK', 'PPA', 'RSP', 'EUSA', 'JPXN', 'DRIV',
    'QUAL', 'EAOR', 'DAX', 'EWY', 'QQQ', 'ECH', 'SPYG', 'IWM', 'EUFN',
    'EWP', 'FEZ', 'COLO', 'EFNL', 'EWW', 'EWS', 'IEV', 'IEUR', 'VUG',
    'GDE', 'EEMV', 'EPOL', 'XLF', 'XLP', 'QVMT', 'EWI', 'SPVM', 'USD999997',
    'SPYV', 'OPPJ', 'SPY', 'EWJ', 'RND',
}


def yf_symbol(sym: str) -> str:
    return sym.replace('/', '-')


def risk_free_rate() -> float:
    try:
        return yf.Ticker('^IRX').history(period='5d')['Close'].iloc[-1] / 100
    except Exception:
        return 0.043


def _download_closes(symbols: list[str], start: str, end: str
                      ) -> tuple[dict[str, pd.Series], dict[str, pd.Series]]:
    """Batched close-price fetch. Returns (closes, splits), each keyed by
    portfolio symbol, omitting symbols that came back with nothing.

    yfinance hands back flat columns for a single ticker and a MultiIndex for
    several, and it keys results by the *yahoo* symbol (BRK-B) rather than the
    portfolio one (BRK/B), so both are normalized here.

    `actions=True` makes the same request also carry the split table, so split
    detection costs zero extra round trips. Note yf.download defaults
    ignore_tz=True (naive index) while Ticker.splits is exchange-tz-aware —
    splits.py normalizes either way.
    """
    if not symbols:
        return {}, {}
    ymap = {yf_symbol(s): s for s in symbols}
    try:
        raw = yf.download(list(ymap), start=start, end=end, auto_adjust=True,
                          actions=True, progress=False, threads=True,
                          group_by='column')
    except Exception as e:
        print(f'WARNING (history batch {start}, {len(symbols)} symbols): {e}')
        return {}, {}
    if raw is None or raw.empty:
        return {}, {}

    if isinstance(raw.columns, pd.MultiIndex):
        if 'Close' not in raw.columns.get_level_values(0):
            return {}, {}
        closes = raw['Close']
    else:
        if 'Close' not in raw.columns:
            return {}, {}
        closes = raw[['Close']]
        closes.columns = list(ymap)

    out: dict[str, pd.Series] = {}
    for ysym, sym in ymap.items():
        if ysym not in closes.columns:
            continue
        s = closes[ysym].dropna()
        if s.empty:
            continue
        s = s.rename(sym)
        idx = pd.DatetimeIndex(s.index)
        if idx.tz is not None:
            idx = idx.tz_localize(None)
        s.index = idx.normalize()
        out[sym] = s

    # Map the split table back from yahoo symbols to portfolio symbols.
    raw_splits = splits.extract_from_download(raw)
    if '__single__' in raw_splits and len(ymap) == 1:
        raw_splits = {next(iter(ymap)): raw_splits.pop('__single__')}
    split_out = {ymap[y]: s for y, s in raw_splits.items() if y in ymap}
    return out, split_out


def seed_splits(symbols: list[str], start, split_table: dict | None = None) -> dict:
    """One batched actions-only fetch to establish split coverage back to `start`.

    Needed because the incremental price refresh only ever downloads dates
    after the last cached one, so its window can't contain a split that already
    happened — which is precisely the case that matters (a split between the
    broker export and now). Runs once, then the incremental feed keeps it
    current; see splits.needs_seed.
    """
    table = dict(split_table or {})
    if not symbols:
        return table
    print(f'  seeding split history for {len(symbols)} symbol(s) from {start}')
    _closes, new_splits = _download_closes(list(symbols), str(start),
                                            str(date.today() + timedelta(days=1)))
    return splits.merge_downloaded(table, new_splits)


def refresh_historical(combined_index, hist_file: str, years: int = 10,
                        split_table: dict | None = None) -> tuple[pd.DataFrame, dict]:
    """Load historical.csv, fetch only the missing tail per symbol, save back.

    Returns (hist_df, split_table) — the table is refreshed from the same
    download that fetches prices, so callers get split data for free.
    """
    today = date.today()
    start_full = pd.Timestamp(today - timedelta(days=365 * years + 30))
    split_table = dict(split_table or {})

    try:
        hist_df = pd.read_csv(hist_file, index_col=0, parse_dates=True)
        hist_df.index.name = 'Date'
        hist_df.index = hist_df.index.tz_localize(None)
    except (FileNotFoundError, Exception):
        hist_df = pd.DataFrame()

    symbols = [s for s in combined_index if s != 'cash' and _TICKER_RE.match(str(s))]

    # Group symbols by the date they need fetching from, then issue ONE
    # yf.download per group instead of one Ticker.history() per symbol. On a
    # normal incremental run every symbol shares the same start date, so 122
    # sequential round trips collapse into a single batched call.
    by_start: dict[date, list[str]] = {}
    for sym in symbols:
        if sym in hist_df.columns and not hist_df[sym].dropna().empty:
            last_date = hist_df[sym].dropna().index[-1].date()
            if last_date >= today:
                continue
            # A split makes every cached row for this symbol obsolete:
            # auto_adjust restates the whole series retroactively, but we only
            # ever fetch dates after last_date and combine_first keeps the
            # existing value, so old rows would stay in pre-split units and
            # leave a ~-95% cliff mid-series. Drop the column and refetch it
            # whole. Rare enough to be cheap; silent corruption otherwise.
            if splits.factor_since(split_table, sym, last_date) != 1.0:
                print(f'  {sym}: split since {last_date} — refetching full history')
                hist_df = hist_df.drop(columns=[sym])
                fetch_start = start_full.date()
            else:
                fetch_start = last_date + timedelta(days=1)
        else:
            fetch_start = start_full.date()
        by_start.setdefault(fetch_start, []).append(sym)

    updated = False
    end = str(today + timedelta(days=1))
    for fetch_start, group in sorted(by_start.items()):
        closes, new_splits = _download_closes(group, str(fetch_start), end)
        split_table = splits.merge_downloaded(split_table, new_splits)
        for sym, new_close in closes.items():
            # Reindex FIRST, in both branches. DataFrame.__setitem__ aligns the
            # right-hand side to the frame's *existing* index, so assigning a
            # combine_first() result straight back silently drops every date
            # not already present — and since fetch_start is last_date + 1 day,
            # that is every row we just fetched. The file kept being rewritten
            # identically while Gain_3m/Sharpe_*/Ann_Vol/Beta/MPT quietly froze
            # at whenever the last from-scratch rebuild happened.
            hist_df = hist_df.reindex(hist_df.index.union(new_close.index))
            if sym in hist_df.columns:
                hist_df[sym] = hist_df[sym].combine_first(new_close)
            else:
                hist_df[sym] = new_close
            updated = True

    cutoff = pd.Timestamp(today - timedelta(days=365 * years))
    hist_df = hist_df[hist_df.index >= cutoff].sort_index()
    if updated:
        hist_df.to_csv(hist_file)
    return hist_df, split_table
    return hist_df


def symbol_metrics(combined: pd.DataFrame, hist_df: pd.DataFrame, rf_annual: float) -> pd.DataFrame:
    """Per-symbol Current_Price/PE/targets (live via yfinance .info) plus
    Ann_Vol/Sharpe/Gain windows computed from hist_df. Returns a DataFrame
    indexed by Symbol, meant to be joined onto `combined`."""
    symbols = [s for s in combined.index if s != 'cash' and _TICKER_RE.match(str(s))]
    rf_daily = rf_annual / 252
    windows = {'1yr': 252, '6m': 126, '3m': 63}
    rows = {}

    for sym in symbols:
        ysym = yf_symbol(sym)
        row = {}
        try:
            info = yf.Ticker(ysym).info
            live_price = info.get('currentPrice') or info.get('regularMarketPrice')
            row['Current_Price'] = live_price
            t_pe = info.get('trailingPE')
            t_eps = info.get('trailingEps')
            if t_pe is None and live_price and t_eps:
                t_pe = live_price / t_eps
            row['Trailing_PE'] = round(t_pe, 2) if t_pe else float('nan')
            f_pe = info.get('forwardPE')
            f_eps = info.get('forwardEps')
            if f_pe is None and live_price and f_eps:
                f_pe = live_price / f_eps
            row['Forward_PE'] = round(f_pe, 2) if f_pe else float('nan')
            row['Target_Mean'] = info.get('targetMeanPrice')
            row['Target_Median'] = info.get('targetMedianPrice')
            row['Target_High'] = info.get('targetHighPrice')
            row['Target_Low'] = info.get('targetLowPrice')
            row['Num_Analysts'] = info.get('numberOfAnalystOpinions')
        except Exception as e:
            print(f'WARNING (info) {sym}: {e}')
            for k in ('Current_Price', 'Trailing_PE', 'Forward_PE', 'Target_Mean',
                      'Target_Median', 'Target_High', 'Target_Low', 'Num_Analysts'):
                row[k] = float('nan')

        if sym in hist_df.columns:
            closes = hist_df[sym].dropna()
            daily_ret = closes.pct_change().dropna()
            row['Ann_Vol'] = round(daily_ret.std() * np.sqrt(252), 4) if len(daily_ret) > 20 else float('nan')
            for label, days in windows.items():
                if len(daily_ret) < 20:
                    row[f'Sharpe_{label}'] = float('nan')
                    row[f'Gain_{label}'] = float('nan')
                    continue
                subset = daily_ret.iloc[-days:]
                row[f'Sharpe_{label}'] = round((subset.mean() - rf_daily) / subset.std() * np.sqrt(252), 3)
                n = min(days, len(closes) - 1)
                row[f'Gain_{label}'] = round((closes.iloc[-1] / closes.iloc[-n] - 1) * 100, 2)
        else:
            row['Ann_Vol'] = float('nan')
            for label in windows:
                row[f'Sharpe_{label}'] = float('nan')
                row[f'Gain_{label}'] = float('nan')

        rows[sym] = row

    metrics_df = pd.DataFrame.from_dict(rows, orient='index')
    metrics_df.index.name = 'Symbol'
    return metrics_df


def classify_sectors(combined: pd.DataFrame, sector_cache_file: str) -> pd.DataFrame:
    """Sector/cap/vol classification for equity symbols. Reads+updates the
    sectors.csv cache. Returns a DataFrame indexed by Symbol with columns
    Quote_Type, Sector, MarketCap, Cap_Tier, Vol_Tier."""
    import os
    eq = combined[combined.index != 'cash']
    symbols = eq.index.tolist()

    if os.path.exists(sector_cache_file):
        sec_cache = pd.read_csv(sector_cache_file, index_col='Symbol').to_dict('index')
        # Evict placeholders written by the old failure-caching behavior (a
        # transient 404 used to pin a symbol to Unknown permanently) so they
        # get one more chance.
        sec_cache = {k: v for k, v in sec_cache.items() if v.get('Quote_Type') != 'Unknown'}
    else:
        sec_cache = {}

    needs_fetch = [s for s in symbols if s not in sec_cache]
    for sym in needs_fetch:
        ysym = yf_symbol(sym)
        try:
            info = yf.Ticker(ysym).info
            qtype = info.get('quoteType', '')
            if qtype == 'EQUITY':
                raw_sector = info.get('sector', '')
                sector = SECTOR_MAP.get(raw_sector, raw_sector or 'Unknown')
                mktcap = info.get('marketCap')
            else:
                cat = info.get('category', '') or ''
                sector = next((v for k, v in ETF_SECTOR_KEYWORDS.items() if k.lower() in cat.lower()),
                              'Broad Market')
                mktcap = None
            sec_cache[sym] = {'Quote_Type': qtype, 'Sector': sector, 'MarketCap': mktcap}
        except Exception as e:
            # Deliberately NOT cached. Only symbols missing from the cache are
            # ever fetched, so writing an 'Unknown' placeholder here made a
            # single transient 404 permanent — the symbol was pinned to
            # Sector=Unknown/Cap_Tier=ETF/Fund forever with nothing to retry
            # it. Leaving it absent costs one retry next run.
            print(f'WARNING (sector) {sym}: {e} — not cached, will retry next run')

    sec_df = pd.DataFrame.from_dict(sec_cache, orient='index')
    sec_df.index.name = 'Symbol'
    sec_df.to_csv(sector_cache_file)

    # A symbol whose fetch failed isn't in the cache (so it retries next run),
    # but it still needs a displayable value here rather than a null.
    result = sec_df.reindex(symbols)
    result['Quote_Type'] = result['Quote_Type'].fillna('Unknown')
    result['Sector'] = result['Sector'].fillna('Unknown')

    def cap_tier(row):
        if row['Quote_Type'] != 'EQUITY' or pd.isna(row['MarketCap']):
            return 'ETF/Fund'
        mc = row['MarketCap']
        if mc >= LARGE_CAP:
            return 'Large Cap'
        if mc >= MID_CAP:
            return 'Mid Cap'
        return 'Small Cap'

    def vol_tier(row):
        v = combined.loc[row.name, 'Ann_Vol'] if 'Ann_Vol' in combined.columns else float('nan')
        if pd.isna(v):
            return 'Unknown'
        if v >= HIGH_VOL:
            return 'High Vol'
        if v <= LOW_VOL:
            return 'Low Vol'
        return 'Mid Vol'

    result['Cap_Tier'] = result.apply(cap_tier, axis=1)
    result['Vol_Tier'] = result.apply(vol_tier, axis=1)
    return result


def analyst_targets(combined: pd.DataFrame) -> pd.DataFrame:
    """Target_Upside / Target_Spread for symbols that have analyst target data."""
    tgt = combined[combined.index != 'cash'].copy()
    tgt = tgt[tgt['Target_Median'].notna() & tgt['Current_Price'].notna()]
    tgt['Target_Upside'] = ((tgt['Target_Median'] / tgt['Current_Price'] - 1) * 100).round(2)
    tgt['Target_Spread'] = ((tgt['Target_High'] - tgt['Target_Low']) / tgt['Target_Median'] * 100).round(2)
    return tgt


def earnings_and_recommendations(combined: pd.DataFrame, earn_cache_file: str,
                                  etf_skip: set = None) -> tuple[pd.DataFrame, pd.DataFrame, list]:
    """Refresh earnings.csv cache and fetch analyst recommendations.

    Returns (earn_cache, recs_df, upgrades_rows). `upgrades_rows` is always []
    — the upgrades/downgrades fetch was removed: it cost a yfinance round trip
    per symbol on every run and every caller discarded the result, because
    nothing ever wrote the upgrades.csv that export_app_data reads. Kept in the
    signature so existing 3-tuple unpacking (notebook cell 10) still works.

    Skipping: funds and ADRs have no earnings calendar or analyst coverage, so
    asking for them is 404s and wasted time. That used to be a hand-maintained
    symbol list (DEFAULT_ETF_SKIP) which drifted out of date — 11 held ETFs
    weren't in it. Now it's driven by the Quote_Type already computed in
    classify_sectors, with the list as a fallback for anything unclassified.
    """
    import os
    if etf_skip is None:
        etf_skip = DEFAULT_ETF_SKIP

    today = pd.Timestamp(date.today())
    if os.path.exists(earn_cache_file):
        earn_cache = pd.read_csv(earn_cache_file, index_col='Symbol', parse_dates=['Next_Earnings'])
    else:
        earn_cache = pd.DataFrame(columns=['Next_Earnings', 'EPS_Est', 'Rev_Est_High', 'Rev_Est_Low'])
        earn_cache.index.name = 'Symbol'

    symbols = combined.index[combined.index != 'cash'].tolist()
    quote_types = (combined['Quote_Type'] if 'Quote_Type' in combined.columns
                   else pd.Series(dtype=object))
    recs_rows = {}
    upgrades: list = []

    for sym in symbols:
        qtype = quote_types.get(sym)
        if pd.notna(qtype) and qtype != '':
            if qtype != 'EQUITY':
                continue
        elif sym in etf_skip:
            continue
        ysym = yf_symbol(sym)

        needs_earn = True
        if sym in earn_cache.index:
            cached_date = earn_cache.loc[sym, 'Next_Earnings']
            if pd.notna(cached_date) and pd.Timestamp(cached_date) > today:
                needs_earn = False

        if needs_earn:
            try:
                cal = yf.Ticker(ysym).calendar
                if cal and 'Earnings Date' in cal and cal['Earnings Date']:
                    next_earn = pd.Timestamp(cal['Earnings Date'][0])
                    earn_cache.loc[sym] = {
                        'Next_Earnings': next_earn,
                        'EPS_Est': cal.get('Earnings Average'),
                        'Rev_Est_High': cal.get('Revenue High'),
                        'Rev_Est_Low': cal.get('Revenue Low'),
                    }
                else:
                    earn_cache.loc[sym, 'Next_Earnings'] = pd.NaT
            except Exception:
                earn_cache.loc[sym, 'Next_Earnings'] = pd.NaT

        try:
            rs = yf.Ticker(ysym).recommendations_summary
            if rs is not None and not rs.empty:
                cur = rs[rs['period'] == '0m'].iloc[0]
                total = cur[['strongBuy', 'buy', 'hold', 'sell', 'strongSell']].sum()
                recs_rows[sym] = {
                    'Strong_Buy': int(cur['strongBuy']),
                    'Buy': int(cur['buy']),
                    'Hold': int(cur['hold']),
                    'Sell': int(cur['sell']),
                    'Strong_Sell': int(cur['strongSell']),
                    'Consensus': (
                        'Strong Buy' if cur['strongBuy'] / max(total, 1) > 0.4 else
                        'Buy' if (cur['strongBuy'] + cur['buy']) / max(total, 1) > 0.5 else
                        'Hold' if cur['hold'] / max(total, 1) > 0.4 else
                        'Sell' if (cur['sell'] + cur['strongSell']) / max(total, 1) > 0.4 else
                        'Mixed'
                    ),
                }
        except Exception:
            pass

    earn_cache.to_csv(earn_cache_file)

    recs_df = pd.DataFrame.from_dict(recs_rows, orient='index')
    recs_df.index.name = 'Symbol'

    return earn_cache, recs_df, upgrades

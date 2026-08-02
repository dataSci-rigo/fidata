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


def refresh_historical(combined_index, hist_file: str, years: int = 10) -> pd.DataFrame:
    """Load historical.csv, fetch only the missing tail per symbol, save back."""
    today = date.today()
    start_full = pd.Timestamp(today - timedelta(days=365 * years + 30))

    try:
        hist_df = pd.read_csv(hist_file, index_col=0, parse_dates=True)
        hist_df.index.name = 'Date'
        hist_df.index = hist_df.index.tz_localize(None)
    except (FileNotFoundError, Exception):
        hist_df = pd.DataFrame()

    symbols = [s for s in combined_index if s != 'cash' and _TICKER_RE.match(str(s))]

    updated = False
    for sym in symbols:
        ysym = yf_symbol(sym)
        if sym in hist_df.columns and not hist_df[sym].dropna().empty:
            last_date = hist_df[sym].dropna().index[-1].date()
            if last_date >= today:
                continue
            fetch_start = last_date + timedelta(days=1)
        else:
            fetch_start = start_full.date()
        try:
            raw = yf.Ticker(ysym).history(start=str(fetch_start), end=str(today + timedelta(days=1)),
                                           auto_adjust=True)
            if raw.empty:
                continue
            new_close = raw['Close'].rename(sym)
            new_close.index = new_close.index.tz_localize(None).normalize()
            if sym in hist_df.columns:
                hist_df[sym] = hist_df[sym].combine_first(new_close)
            else:
                hist_df = hist_df.reindex(hist_df.index.union(new_close.index))
                hist_df[sym] = new_close
            updated = True
        except Exception as e:
            print(f'WARNING (history) {sym}: {e}')

    cutoff = pd.Timestamp(today - timedelta(days=365 * years))
    hist_df = hist_df[hist_df.index >= cutoff].sort_index()
    if updated:
        hist_df.to_csv(hist_file)
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
            print(f'WARNING (sector) {sym}: {e}')
            sec_cache[sym] = {'Quote_Type': 'Unknown', 'Sector': 'Unknown', 'MarketCap': None}

    sec_df = pd.DataFrame.from_dict(sec_cache, orient='index')
    sec_df.index.name = 'Symbol'
    sec_df.to_csv(sector_cache_file)

    result = sec_df.reindex(symbols)

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
    """Refresh earnings.csv cache, fetch analyst recommendations + recent
    upgrades/downgrades. Returns (earn_cache, recs_df, upgrades_rows)."""
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
    recs_rows = {}
    upgrades = []

    for sym in symbols:
        if sym in etf_skip:
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

        try:
            ud = yf.Ticker(ysym).upgrades_downgrades
            if ud is not None and not ud.empty:
                cutoff = pd.Timestamp(today - timedelta(days=90), tz='UTC')
                ud.index = pd.to_datetime(ud.index, utc=True)
                recent = ud[ud.index >= cutoff].copy()
                if not recent.empty:
                    recent.insert(0, 'Symbol', sym)
                    upgrades.append(recent.reset_index()[
                        ['Symbol', 'GradeDate', 'Firm', 'ToGrade', 'FromGrade', 'Action', 'currentPriceTarget']])
        except Exception:
            pass

    earn_cache.to_csv(earn_cache_file)

    recs_df = pd.DataFrame.from_dict(recs_rows, orient='index')
    recs_df.index.name = 'Symbol'

    return earn_cache, recs_df, upgrades

"""Is a position earning its keep?

Cost basis answers "am I up on this", which is a question about when you
bought. This module answers the different question — "has holding this beaten
the alternatives" — by comparing each position's price return over 1, 3 and 5
years against both SPY and its own sector ETF. A position lagging both is dead
money; lagging only SPY may just be an unloved sector.

The comparison is on PRICE, deliberately: it asks what the capital did while
it sat there, independent of what you paid for it. SBUX is the case that
prompted this — held since before 2023 and up 71% on cost, but flat over five
years while SPY compounded ~89%.

The sector ETFs need 10 years of history, so run_pipeline refreshes them into
historical.csv alongside the holdings. They are deliberately kept out of the
breakout-alert universe (see BENCHMARKS' use in run_pipeline) — they exist to
be measured against, not to be traded.
"""
import pandas as pd

# GICS sector (as enrich.SECTOR_MAP/ETF_SECTOR_KEYWORDS emit it) -> proxy ETF
SECTOR_ETF = {
    'Information Technology': 'XLK',
    'Financials': 'XLF',
    'Consumer Discretionary': 'XLY',
    'Consumer Staples': 'XLP',
    'Materials': 'XLB',
    'Communication Services': 'XLC',
    'Healthcare': 'XLV',
    'Industrials': 'XLI',
    'Energy': 'XLE',
    'Real Estate': 'XLRE',
    'Utilities': 'XLU',
    'Broad Market': 'SPY',
    'International': 'VEA',
    'Fixed Income': 'AGG',
    'Commodities': 'DBC',
}
MARKET = 'SPY'
BENCHMARKS = frozenset(SECTOR_ETF.values()) | {MARKET}
HORIZONS = (1, 3, 5)


def window_return(series: pd.Series, years: int) -> float | None:
    """Price return over the trailing `years`, or None when the history
    doesn't reach back that far."""
    s = series.dropna()
    if len(s) < 10:
        return None
    cutoff = s.index.max() - pd.DateOffset(years=years)
    past = s[s.index <= cutoff]
    if past.empty or past.iloc[-1] <= 0:
        return None
    return round(float(s.iloc[-1] / past.iloc[-1] - 1) * 100, 2)


def benchmark_returns(hist_df: pd.DataFrame,
                      horizons=HORIZONS) -> dict[str, dict[int, float | None]]:
    """{benchmark symbol: {years: return}} for every benchmark present."""
    return {sym: {y: window_return(hist_df[sym], y) for y in horizons}
            for sym in sorted(BENCHMARKS) if sym in hist_df.columns}


def position_performance(hist_df: pd.DataFrame, sectors: dict[str, str],
                         horizons=HORIZONS) -> pd.DataFrame:
    """Per symbol: price return at each horizon, plus excess over SPY and over
    its sector ETF. `sectors` maps symbol -> sector name.

    Columns: Sector, Sector_ETF, Ret_1y/3y/5y, SPY_Excess_5y, Sector_Excess_5y
    """
    bench = benchmark_returns(hist_df, horizons)
    rows = []
    for sym in hist_df.columns:
        if sym in BENCHMARKS and sym not in sectors:
            continue                      # pure benchmark, not a holding
        rets = {y: window_return(hist_df[sym], y) for y in horizons}
        if all(v is None for v in rets.values()):
            continue
        sector = sectors.get(sym) or ''
        etf = SECTOR_ETF.get(sector)
        row = {'Symbol': sym, 'Sector': sector, 'Sector_ETF': etf or ''}
        for y in horizons:
            row[f'Ret_{y}y'] = rets[y]
        long_h = max(horizons)
        mkt = (bench.get(MARKET) or {}).get(long_h)
        sec = (bench.get(etf) or {}).get(long_h) if etf else None
        row[f'SPY_Excess_{long_h}y'] = (round(rets[long_h] - mkt, 2)
                                        if rets[long_h] is not None and mkt is not None
                                        else None)
        row[f'Sector_Excess_{long_h}y'] = (round(rets[long_h] - sec, 2)
                                           if rets[long_h] is not None and sec is not None
                                           else None)
        rows.append(row)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).set_index('Symbol')


def laggards(perf: pd.DataFrame, market_values: dict[str, float],
             horizon: int = 5, min_value: float = 500.0,
             both_only: bool = False) -> list[dict]:
    """Positions trailing the market (and optionally their sector too) over
    `horizon` years, worst first. `both_only` is the strict reading of dead
    money: behind SPY *and* behind its own sector, so the sector can't be
    blamed for it."""
    if perf.empty:
        return []
    spy_col, sec_col = f'SPY_Excess_{horizon}y', f'Sector_Excess_{horizon}y'
    out = []
    for sym, r in perf.iterrows():
        mv = float(market_values.get(sym) or 0)
        if mv < min_value or pd.isna(r.get(spy_col)):
            continue
        behind_spy = r[spy_col] < 0
        behind_sec = pd.notna(r.get(sec_col)) and r[sec_col] < 0
        if not behind_spy or (both_only and not behind_sec):
            continue
        out.append({
            'symbol': str(sym), 'market_value': round(mv, 0),
            'sector': r['Sector'], 'sector_etf': r['Sector_ETF'],
            f'ret_{horizon}y': None if pd.isna(r[f'Ret_{horizon}y']) else r[f'Ret_{horizon}y'],
            'vs_spy': r[spy_col],
            'vs_sector': None if pd.isna(r.get(sec_col)) else r[sec_col],
            'behind_both': bool(behind_spy and behind_sec),
        })
    return sorted(out, key=lambda d: d['vs_spy'])

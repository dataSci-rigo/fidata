"""universe_data: ingestion pieces driven with synthetic data — zero network.
Every filesystem path is passed explicitly (never the module defaults)."""
import os
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import universe_data as ud  # noqa: E402


# ── seed parsing ─────────────────────────────────────────────────────────────

def test_load_etf_list(tmp_path):
    p = tmp_path / 'etf_list.txt'
    p.write_text('# comment only\n\nSPY broad_us  # S&P 500\nTLT bond_treasury\nqqq\n')
    df = ud.load_etf_list(str(p))
    assert list(df['Symbol']) == ['SPY', 'TLT', 'QQQ']
    assert list(df['ETF_Tag']) == ['broad_us', 'bond_treasury', '']
    assert ud.load_etf_list(str(tmp_path / 'missing.txt')).empty


def test_parse_wiki_symbols_bounds_and_normalization():
    good = pd.DataFrame({'Symbol': [f'S{i}' for i in range(499)] + ['BRK.B']})
    junk = pd.DataFrame({'Other': [1, 2]})
    out = ud.parse_wiki_symbols([junk, good], ('Symbol',), (480, 520))
    assert len(out) == 500 and 'BRK-B' in out
    # out-of-bounds table rejected
    small = pd.DataFrame({'Symbol': ['A', 'B']})
    assert ud.parse_wiki_symbols([small], ('Symbol',), (480, 520)) == []


def test_load_fallback(tmp_path):
    p = tmp_path / 'sp.txt'
    p.write_text('# snapshot\nAAPL\nmsft\n\nBRK-B\n')
    assert ud.load_fallback(str(p)) == ['AAPL', 'BRK-B', 'MSFT']
    assert ud.load_fallback(str(tmp_path / 'nope.txt')) == []


def test_build_constituents_fallback_chain(tmp_path):
    (tmp_path / 'sp.txt').write_text('AAPL\nMSFT\nSPY\n')
    (tmp_path / 'ndx.txt').write_text('MSFT\nNVDA\n')
    (tmp_path / 'etf.txt').write_text('SPY broad_us\nTLT bond_treasury\n')
    df = ud.build_constituents(universe_dir=str(tmp_path / 'gen'),
                               etf_file=str(tmp_path / 'etf.txt'),
                               sp500_fallback=str(tmp_path / 'sp.txt'),
                               ndx_fallback=str(tmp_path / 'ndx.txt'),
                               live=False)
    assert set(df['Symbol']) == {'AAPL', 'MSFT', 'NVDA', 'SPY', 'TLT'}
    r = df.set_index('Symbol')
    assert bool(r.loc['MSFT', 'In_SP500']) and bool(r.loc['MSFT', 'In_NDX'])
    assert not bool(r.loc['TLT', 'In_SP500'])
    assert r.loc['TLT', 'ETF_Tag'] == 'bond_treasury'
    assert r.loc['SPY', 'ETF_Tag'] == 'broad_us' and bool(r.loc['SPY', 'In_SP500'])
    # cache written for next time
    assert os.path.exists(tmp_path / 'gen' / ud.CONSTITUENTS_FILE)


# ── prices ───────────────────────────────────────────────────────────────────

def _series(start, n, base=100.0, step=1.0):
    idx = pd.bdate_range(start, periods=n)
    return pd.Series(base + step * np.arange(n), index=idx)


def test_merge_wide_fresh_wins():
    base = pd.DataFrame({'A': _series('2026-01-01', 5)})
    newer = {'A': _series('2026-01-07', 3, base=999),
             'B': _series('2026-01-01', 3)}
    merged = ud._merge_wide(base, newer)
    assert merged['A'].iloc[-1] == 1001.0 and merged['A'].iloc[0] == 100.0
    assert 'B' in merged.columns


def test_refresh_prices_incremental(tmp_path, monkeypatch):
    udir = str(tmp_path)
    old = pd.DataFrame({'AAA': _series('2026-01-01', 10)})
    old.to_csv(os.path.join(udir, ud.CLOSES_FILE), index_label='Date')
    old.to_csv(os.path.join(udir, ud.VOLUME_FILE), index_label='Date')

    calls = []

    def fake_download(symbols, start, end):
        calls.append((tuple(symbols), start))
        s = _series(start, 3, base=500)
        return ({sym: s.rename(sym) for sym in symbols},
                {sym: s.rename(sym) for sym in symbols})

    monkeypatch.setattr(ud, '_download_close_volume', fake_download)
    closes, vols = ud.refresh_prices(['AAA', 'BBB'], universe_dir=udir,
                                     pause=0)
    known_call = next(c for c in calls if c[0] == ('AAA',))
    last_old = old.index.max()
    assert known_call[1] == str((last_old + pd.Timedelta(days=1)).date())
    new_call = next(c for c in calls if c[0] == ('BBB',))
    assert new_call[1] < '2026-01-01'          # full 2y window for new symbol
    assert {'AAA', 'BBB'} <= set(closes.columns)
    # dropped symbols leave the frame
    monkeypatch.setattr(ud, '_download_close_volume', lambda *a: ({}, {}))
    closes2, _ = ud.refresh_prices(['BBB'], universe_dir=udir, pause=0)
    assert 'AAA' not in closes2.columns


# ── portfolio returns + metrics ──────────────────────────────────────────────

def test_portfolio_daily_returns_matches_hand_math():
    idx = pd.bdate_range('2026-01-01', periods=5)
    hist = pd.DataFrame({'AAA': [100, 110, 121, 121, 133.1],
                         'BBB': [50, 50, 50, 50, 50]}, index=idx)
    w = pd.Series({'AAA': 300.0, 'BBB': 100.0})   # 75% / 25%
    r = ud.portfolio_daily_returns(hist, w)
    assert r is not None and len(r) == 4
    assert abs(r.iloc[0] - 0.075) < 1e-9          # 0.75*10% + 0.25*0%
    # BRK/B spelling normalizes onto a BRK-B column
    hist2 = hist.rename(columns={'AAA': 'BRK-B'})
    r2 = ud.portfolio_daily_returns(hist2, pd.Series({'BRK/B': 1.0}))
    assert r2 is not None and abs(r2.iloc[0] - 0.10) < 1e-9
    assert ud.portfolio_daily_returns(pd.DataFrame(), w) is None
    assert ud.portfolio_daily_returns(hist, pd.Series(dtype=float)) is None


def test_compute_metrics_known_values():
    from screener import perf_metrics
    n = 300
    idx = pd.bdate_range('2025-06-01', periods=n)
    rng = np.random.default_rng(7)
    spy = pd.Series(100 * np.cumprod(1 + rng.normal(0.0005, 0.01, n)), index=idx)
    up = pd.Series(np.linspace(50, 100, n), index=idx)          # steady riser
    closes = pd.DataFrame({'SPY': spy, 'UP': up, 'TWIN': spy * 2})
    volumes = pd.DataFrame({'UP': pd.Series(1e6, index=idx)})
    port = spy.tail(ud.LOOKBACK + 1).pct_change().dropna()      # portfolio == SPY

    m = ud.compute_metrics(closes, volumes, port, rf_annual=0.04)
    assert m.loc['SPY', 'Beta'] == 1.0
    assert abs(m.loc['TWIN', 'Beta'] - 1.0) < 0.05              # same returns as SPY
    assert abs(m.loc['TWIN', 'Corr_Portfolio'] - 1.0) < 1e-6
    assert m.loc['UP', 'Uptrend'] and m.loc['UP', 'Above_EMA200']
    assert m.loc['UP', 'High_52w_Ratio'] == 1.0                 # at its high
    expected = perf_metrics(up.tail(ud.LOOKBACK), 0.04)
    assert m.loc['UP', 'Sharpe_1yr'] == expected['Sharpe']
    assert m.loc['UP', 'Max_Drawdown'] == expected['Max_Drawdown']
    gain_1yr = round(float(up.iloc[-1] / up.iloc[-253] - 1) * 100, 2)
    assert m.loc['UP', 'Gain_1yr'] == gain_1yr
    avg_dv = float((up * 1e6).tail(ud.DOLLAR_VOL_WINDOW).mean())
    assert abs(m.loc['UP', 'Avg_Dollar_Vol'] - avg_dv) < 1e-3


def test_compute_metrics_short_overlap_gives_no_corr():
    idx = pd.bdate_range('2026-01-01', periods=30)
    closes = pd.DataFrame({'NEW': pd.Series(np.linspace(10, 20, 30), index=idx)})
    port = pd.Series(0.001, index=idx).iloc[1:]
    m = ud.compute_metrics(closes, pd.DataFrame(), port, 0.04)
    assert 'Corr_Portfolio' not in m.columns or pd.isna(m.loc['NEW', 'Corr_Portfolio'])


# ── info cache ───────────────────────────────────────────────────────────────

def test_extract_info_fields_normalization():
    # Live-cache-verified quirks (2026-09-23): SPY reports dividendYield 0.98
    # meaning 0.98% while KO reports 0.0239 meaning 2.39%; GLD reports
    # netExpenseRatio 0.40 meaning 0.40%. Everything must land as a fraction.
    out = ud.extract_info_fields({
        'quoteType': 'ETF', 'dividendYield': 3.7,          # percent-style
        'annualReportExpenseRatio': 0.15,                  # percent-style (0.15%)
        'longBusinessSummary': 'x' * 1000})
    assert out['dividendYield'] == pytest.approx(0.037)
    assert out['expenseRatio'] == pytest.approx(0.0015)
    assert len(out['summary']) == ud.SUMMARY_MAX
    out2 = ud.extract_info_fields({'netExpenseRatio': 0.4,      # GLD-style
                                   'dividendYield': 0.012})     # fraction kept
    assert out2['expenseRatio'] == pytest.approx(0.004)
    assert out2['dividendYield'] == pytest.approx(0.012)
    out3 = ud.extract_info_fields({'netExpenseRatio': 0.0015,   # true fraction kept
                                   'dividendYield': 0.98})      # SPY-style percent
    assert out3['expenseRatio'] == pytest.approx(0.0015)
    assert out3['dividendYield'] == pytest.approx(0.0098)


def test_info_fetch_plan_missing_then_stale_oldest_first():
    now = datetime.now(timezone.utc)
    cache = {
        'OLD': {'fetched_at': (now - timedelta(days=30)).isoformat()},
        'OLDER': {'fetched_at': (now - timedelta(days=60)).isoformat()},
        'FRESH': {'fetched_at': now.isoformat()},
    }
    plan = ud.info_fetch_plan(cache, ['MISS', 'OLD', 'OLDER', 'FRESH'],
                              full=True, max_age_days=7, max_n=None)
    assert plan == ['MISS', 'OLDER', 'OLD']
    assert ud.info_fetch_plan(cache, ['MISS', 'OLD'], full=False,
                              max_age_days=7, max_n=None) == ['MISS']
    assert ud.info_fetch_plan(cache, ['MISS', 'OLDER'], full=True,
                              max_age_days=7, max_n=1) == ['MISS']


def test_refresh_info_keeps_stale_on_failure(tmp_path):
    cache_path = str(tmp_path / 'info_cache.json')
    ud._atomic_json({'KEEP': {'quoteType': 'EQUITY', 'fetched_at': 'old'}},
                    cache_path)

    def fetcher(sym):
        if sym == 'BOOM':
            raise RuntimeError('rate limited')
        return {'quoteType': 'EQUITY', 'shortName': sym}

    import time as _t
    orig_sleep = _t.sleep
    _t.sleep = lambda *_: None                   # skip the retry backoff
    try:
        cache = ud.refresh_info(['GOOD', 'BOOM', 'KEEP'], cache_path,
                                sleep=0, fetcher=fetcher)
    finally:
        _t.sleep = orig_sleep
    assert cache['GOOD']['shortName'] == 'GOOD' and cache['GOOD']['fetched_at']
    assert 'BOOM' not in cache                   # no failure placeholder
    assert cache['KEEP']['fetched_at'] != 'old'  # refetched fine
    assert ud.load_info_cache(cache_path) == cache


# ── merge + status ───────────────────────────────────────────────────────────

def test_build_universe_merge(tmp_path):
    cons = pd.DataFrame({'Symbol': ['NVDA', 'TLT', 'MYSTERY'],
                         'In_SP500': [True, False, False],
                         'In_NDX': [True, False, False],
                         'ETF_Tag': ['', 'bond_treasury', '']})
    metrics = pd.DataFrame({'Ann_Vol': [0.5, 0.15], 'Beta': [1.8, None]},
                           index=pd.Index(['NVDA', 'TLT'], name='Symbol'))
    info = {'NVDA': {'quoteType': 'EQUITY', 'shortName': 'NVIDIA',
                     'sector': 'Technology', 'marketCap': 3e12,
                     'dividendYield': 0.0003, 'fetched_at':
                     datetime.now(timezone.utc).isoformat()},
            'TLT': {'quoteType': 'ETF', 'shortName': 'iShares 20+',
                    'category': 'Long Government', 'beta': -0.1,
                    'expenseRatio': 0.0015}}
    out_path = str(tmp_path / 'universe.csv')
    df = ud.build_universe(cons, metrics, info, out_path).set_index('Symbol')
    assert df.loc['TLT', 'Asset_Class'] == 'Bond'
    assert df.loc['TLT', 'Sector'] == 'Fixed Income'       # bond tag forces it
    assert df.loc['TLT', 'Beta'] == -0.1                   # info fallback
    assert df.loc['NVDA', 'Asset_Class'] == 'Equity'
    assert df.loc['NVDA', 'Beta'] == 1.8                   # metrics wins
    assert df.loc['NVDA', 'Sector'] == 'Information Technology'  # SECTOR_MAP applied
    assert df.loc['MYSTERY', 'Quote_Type'] == 'EQUITY'     # no info default
    assert os.path.exists(out_path)
    loaded = ud.load_universe(str(tmp_path))
    assert set(loaded['Symbol']) == {'NVDA', 'TLT', 'MYSTERY'}


def test_asset_class_from_tag():
    assert ud.asset_class_from_tag('bond_hy') == 'Bond'
    assert ud.asset_class_from_tag('gold') == 'Commodity'
    assert ud.asset_class_from_tag('intl_country') == 'International'
    assert ud.asset_class_from_tag('covered_call') == 'Mixed'
    assert ud.asset_class_from_tag('real_estate') == 'Real Estate'
    assert ud.asset_class_from_tag('broad_us') == 'Equity'
    assert ud.asset_class_from_tag('') == 'Equity'


def test_status_write_merges_and_survives_garbage(tmp_path):
    p = str(tmp_path / 'status.json')
    ud.write_status(p, phase='prices', done=1, total=10)
    ud.write_status(p, done=5)
    s = ud.read_status(p)
    assert s['phase'] == 'prices' and s['done'] == 5 and s['total'] == 10
    with open(p, 'w') as f:
        f.write('{corrupt')
    assert ud.read_status(p) is None
    assert ud.read_status(str(tmp_path / 'missing.json')) is None

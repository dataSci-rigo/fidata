import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from screener import perf_metrics, scan


@pytest.fixture
def hist_screener(fixtures_dir):
    df = pd.read_csv(os.path.join(fixtures_dir, 'hist_screener.csv'),
                     index_col=0, parse_dates=True)
    df.index.name = 'Date'
    return df


def test_scan_tiers(hist_screener):
    res = scan(hist_screener)
    # BRKUP: fresh 252d high in an uptrend; NEARHI: ~0.9 of prior high, trend ok
    assert list(res.index) == ['BRKUP', 'NEARHI']
    assert bool(res.loc['BRKUP', 'Breakout']) is True
    assert bool(res.loc['NEARHI', 'Breakout']) is False
    # DOWNTR is near its high but EMA50 < EMA200; SPARSE has < 252 closes
    assert 'DOWNTR' not in res.index
    assert 'SPARSE' not in res.index


def test_scan_near_high_threshold(hist_screener):
    res = scan(hist_screener, near_high=0.95)
    assert 'NEARHI' not in res.index          # ratio ~0.899 < 0.95
    assert 'BRKUP' in res.index


def test_scan_symbol_subset(hist_screener):
    res = scan(hist_screener, symbols=['NEARHI', 'NOSUCH'])
    assert list(res.index) == ['NEARHI']


def test_scan_sorted_by_ratio(hist_screener):
    res = scan(hist_screener)
    assert res['Ratio'].is_monotonic_decreasing


def test_max_drawdown_known_value():
    # 100 -> 110 -> 99 -> 105: max drawdown is 99/110 - 1 = -10%
    close = pd.Series([100.0, 110.0, 99.0, 105.0])
    m = perf_metrics(close.repeat(10).reset_index(drop=True))  # >20 returns
    assert m['Max_Drawdown'] == pytest.approx(-0.1, abs=1e-4)


def test_cumprod_not_cumsum():
    # Regression test for pull_data.py bug #1: with a -50% then +100% move the
    # compounded path ends flat (1 * 0.5 * 2 = 1), while the buggy
    # 1 + cumsum(r) path ends at 1.5 and shows a shallower drawdown
    # (-0.5 vs the true -50% low being recovered from a lower base).
    close = pd.Series([100.0] * 15 + [50.0] + [100.0] * 15)
    m = perf_metrics(close)
    assert m['Max_Drawdown'] == pytest.approx(-0.5, abs=1e-4)


def test_sharpe_matches_enrich_convention():
    # Same formula as enrich.symbol_metrics (enrich.py ~line 287):
    # (r.mean() - rf_annual/252) / r.std() * sqrt(252)
    rng = np.random.default_rng(7)
    close = pd.Series(100 * np.cumprod(1 + rng.normal(0.001, 0.01, 300)))
    rf_annual = 0.043
    r = close.pct_change().dropna()
    expected = (r.mean() - rf_annual / 252) / r.std() * np.sqrt(252)
    m = perf_metrics(close, rf_annual)
    assert m['Sharpe'] == pytest.approx(expected, abs=1e-3)


def test_perf_metrics_short_series_nan():
    m = perf_metrics(pd.Series([100.0, 101.0, 102.0]))
    assert np.isnan(m['Sharpe']) and np.isnan(m['Max_Drawdown'])

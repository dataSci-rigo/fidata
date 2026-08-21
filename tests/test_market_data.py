import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import market_data


@pytest.fixture
def ohlc_daily(fixtures_dir):
    return pd.read_csv(os.path.join(fixtures_dir, 'ohlc_daily_small.csv'))


def test_load_history(fixtures_dir):
    df = market_data.load_history(os.path.join(fixtures_dir, 'hist_screener.csv'))
    assert isinstance(df.index, pd.DatetimeIndex)
    assert df.index.tz is None
    assert df.index.is_monotonic_increasing
    assert set(df.columns) == {'BRKUP', 'NEARHI', 'DOWNTR', 'SPARSE'}


def test_load_watchlist(tmp_path):
    wl = tmp_path / 'watchlist.txt'
    wl.write_text('# header comment\n\nnvda\nSPY  # already held\n  \nQQQ\n')
    assert market_data.load_watchlist(str(wl)) == ['NVDA', 'SPY', 'QQQ']


def test_load_watchlist_missing_file(tmp_path):
    assert market_data.load_watchlist(str(tmp_path / 'nope.txt')) == []


def test_get_closes_subset(fixtures_dir):
    hist = market_data.load_history(os.path.join(fixtures_dir, 'hist_screener.csv'))
    out = market_data.get_closes(['BRKUP', 'MISSING'], hist)
    assert list(out.columns) == ['BRKUP']


def test_to_turtle_frames_daily_degraded(ohlc_daily):
    df_1d, df_1h = market_data.to_turtle_frames(ohlc_daily)
    assert list(df_1d.columns) == ['Date', 'Open', 'High', 'Low', 'Close']
    assert list(df_1h.columns) == ['Date', 'Date_1h', 'Open_1h', 'Close_1h']
    # four pseudo-ticks per day, walking Open -> High -> Low -> Close
    assert len(df_1h) == 4 * len(df_1d)
    assert set(df_1h['Date']) == set(df_1d['Date'])      # merge-key invariant
    per_day = df_1h.groupby('Date', sort=True)
    firsts = per_day.first().reset_index()
    lasts = per_day.last().reset_index()
    assert (firsts['Close_1h'].values == df_1d['Open'].values).all()
    assert (lasts['Close_1h'].values == df_1d['Close'].values).all()
    # each day's pseudo-tick path covers the bar's High and Low
    assert (per_day['Close_1h'].max().values == df_1d['High'].values).all()
    assert (per_day['Close_1h'].min().values == df_1d['Low'].values).all()


def test_to_turtle_frames_hourly_alignment(ohlc_daily):
    # Synthetic hourly bars: 3 per day for the first 10 days, plus one day
    # absent from the daily frame (must be dropped).
    days = pd.to_datetime(ohlc_daily['Date']).iloc[:10]
    rows = []
    for d in days:
        for hour in (10, 12, 15):
            rows.append({'Datetime': d + pd.Timedelta(hours=hour),
                         'Open': 100.0, 'High': 101.0, 'Low': 99.0, 'Close': 100.5})
    rows.append({'Datetime': pd.Timestamp('2030-01-01 10:00'),
                 'Open': 1.0, 'High': 1.0, 'Low': 1.0, 'Close': 1.0})
    hourly = pd.DataFrame(rows)

    df_1d, df_1h = market_data.to_turtle_frames(ohlc_daily, hourly)
    assert len(df_1h) == 30                              # orphan day dropped
    assert set(df_1h['Date']) <= set(df_1d['Date'])


def test_turtle_frames_feed_backtest_indicators(ohlc_daily):
    """Replicate backtest.py's indicator block on the adapter output and
    assert N/bounds are NaN-free after the 21-day warmup — the invariant the
    real backtest run depends on."""
    df_1d, df_1h = market_data.to_turtle_frames(ohlc_daily)

    df_1d['Close_1_shift'] = df_1d['Close'].shift(1)
    df_1h['Close_1_shift_1h'] = df_1h['Close_1h'].shift(1)
    df_1d['TR'] = np.abs(df_1d.High - df_1d.Low)
    df_1d['TR'] = np.maximum(
        df_1d['TR'],
        np.maximum(np.abs(df_1d.Close_1_shift - df_1d.High),
                   np.abs(df_1d.Close_1_shift - df_1d.Low)))
    n_array = np.array(df_1d['TR'].values)
    n_array[20] = np.mean(df_1d['TR'][:20])
    for i in range(21, df_1d.shape[0]):
        n_array[i] = (19.0 * n_array[i - 1] + df_1d['TR'][i]) / 20.0
    df_1d['N'] = n_array
    df_1d['upper_bound'] = df_1d['High'].shift(1).rolling(window=20).max()
    df_1d['lower_bound'] = df_1d['Low'].shift(1).rolling(window=10).min()

    df = df_1h.merge(df_1d, on='Date', how='left')
    bars_per_day = max(1, round(len(df) / len(df_1d)))
    warmup = 21 * bars_per_day
    assert bars_per_day == 4
    tail = df.iloc[warmup:]
    assert not tail[['N', 'upper_bound', 'lower_bound', 'Close_1_shift_1h']].isna().any().any()

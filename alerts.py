"""Alert detection for the headless pipeline: big position moves + upcoming
earnings. News alerts are out of scope for v1 (yfinance's news feed is
unreliable — no dedup/freshness guarantees) per user decision; add a real
news API (Finnhub/NewsAPI/etc.) later if still wanted.
"""
import json
import os

import pandas as pd

BIG_MOVE_PCT = 0.05
EARNINGS_WINDOW_DAYS = 3


def load_last_snapshot(path: str) -> pd.DataFrame | None:
    if not os.path.exists(path):
        return None
    return pd.read_csv(path, index_col='Symbol')


def save_snapshot(combined: pd.DataFrame, path: str) -> None:
    combined[['Current_Price', 'Market_Value']].to_csv(path)


def detect_big_moves(combined: pd.DataFrame, prev_snapshot_file: str,
                      threshold: float = BIG_MOVE_PCT,
                      split_table: dict | None = None) -> list[str]:
    """Compare combined['Current_Price'] against the last saved run's prices.
    Returns one alert message per symbol whose price moved beyond `threshold`
    since the last pipeline run (this runs 3x/day, so "since last check" —
    not a daily figure — by design).

    `split_table` restates the stored price into today's share terms first.
    Without it, the first run after an ex-date reads a 20:1 split as
    '📉 KORU: -95.0%' — a false alarm big enough to drown the real ones, and
    exactly the sort of noise that trains you to ignore the alerts.
    """
    prev = load_last_snapshot(prev_snapshot_file)
    if prev is None:
        return []
    snap_date = None
    if os.path.exists(prev_snapshot_file):
        snap_date = pd.Timestamp(os.path.getmtime(prev_snapshot_file), unit='s')

    messages = []
    eq = combined[combined.index != 'cash']
    for sym in eq.index:
        if sym not in prev.index:
            continue
        prev_price = prev.loc[sym, 'Current_Price']
        cur_price = eq.loc[sym, 'Current_Price']
        if pd.isna(prev_price) or pd.isna(cur_price) or prev_price == 0:
            continue
        if split_table:
            import splits as _splits
            factor = _splits.factor_since(split_table, str(sym), snap_date)
            if factor != 1.0:
                prev_price = prev_price / factor
                if prev_price == 0:
                    continue
        pct_change = (cur_price - prev_price) / prev_price
        if abs(pct_change) >= threshold:
            direction = '📈' if pct_change > 0 else '📉'
            messages.append(
                f'{direction} {sym}: {pct_change:+.1%} since last check '
                f'(${prev_price:.2f} -> ${cur_price:.2f})')
    return messages


def detect_upcoming_earnings(earn_cache: pd.DataFrame, combined: pd.DataFrame,
                              alerted_file: str,
                              window_days: int = EARNINGS_WINDOW_DAYS) -> list[str]:
    """Flag holdings with earnings within `window_days`. Dedups via
    `alerted_file` (a small JSON of {symbol: date_string} already alerted)
    so 3x/day runs don't triple-send the same earnings alert."""
    today = pd.Timestamp.now().normalize()
    cutoff = today + pd.Timedelta(days=window_days)

    if os.path.exists(alerted_file):
        with open(alerted_file) as f:
            alerted = json.load(f)
    else:
        alerted = {}

    messages = []
    held = set(combined.index) - {'cash'}
    upcoming = earn_cache[
        earn_cache.index.isin(held) &
        earn_cache['Next_Earnings'].notna() &
        (earn_cache['Next_Earnings'] >= today) &
        (earn_cache['Next_Earnings'] <= cutoff)
    ]

    for sym, row in upcoming.iterrows():
        earn_date = str(pd.Timestamp(row['Next_Earnings']).date())
        if alerted.get(sym) == earn_date:
            continue
        messages.append(f'📅 {sym}: earnings on {earn_date}')
        alerted[sym] = earn_date

    if messages:
        with open(alerted_file, 'w') as f:
            json.dump(alerted, f, indent=2)

    return messages


def detect_alerts(combined: pd.DataFrame, prev_snapshot_file: str,
                   earn_cache: pd.DataFrame, alerted_earnings_file: str,
                   split_table: dict | None = None) -> list[str]:
    """All v1 alert messages: big moves + upcoming earnings."""
    messages = []
    messages.extend(detect_big_moves(combined, prev_snapshot_file, split_table=split_table))
    messages.extend(detect_upcoming_earnings(earn_cache, combined, alerted_earnings_file))
    return messages

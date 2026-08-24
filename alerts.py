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
BREAKOUT_COOLDOWN_DAYS = 30


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
    Without it, the first run after an ex-date reads an N:1 split as a huge
    fake drop — a false alarm large enough to drown the real ones, and
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


def detect_breakouts(hist_df: pd.DataFrame, symbols: list[str],
                     alerted_file: str,
                     cooldown_days: int = BREAKOUT_COOLDOWN_DAYS) -> list[str]:
    """Two-tier 52-week-high alerts over holdings+watchlist:

    - breakout (ratio >= 1.0 vs the prior 252d high): fresh 52-week high
    - near-high (0.85 <= ratio < 1.0): entered the approach band

    Dedup state is {symbol: {"near": iso_date, "breakout": iso_date}} in
    `alerted_file`, one cooldown per tier — without it the 3x/day pipeline
    would re-ping every uptrending holding all week. A breakout also stamps
    the near tier (a breakout implies near-high; don't double-ping).
    File is rewritten only when something fired, matching
    detect_upcoming_earnings.
    """
    from screener import scan, NEAR_HIGH_DEFAULT, BREAKOUT_LEVEL

    if os.path.exists(alerted_file):
        with open(alerted_file) as f:
            alerted = json.load(f)
    else:
        alerted = {}

    today = pd.Timestamp.now().normalize()
    today_str = str(today.date())

    def on_cooldown(sym: str, tier: str) -> bool:
        last = alerted.get(sym, {}).get(tier)
        return last is not None and \
            (today - pd.Timestamp(last)).days < cooldown_days

    messages = []
    result = scan(hist_df, symbols, near_high=NEAR_HIGH_DEFAULT)
    for sym, row in result.iterrows():
        if row['Breakout']:
            if on_cooldown(sym, 'breakout'):
                continue
            messages.append(
                f"🚀 {sym}: new 52-wk high @ ${row['Close']:.2f} "
                f"(prior high ${row['High_52w']:.2f}, Sharpe {row['Sharpe']:.2f})")
            alerted.setdefault(sym, {})['breakout'] = today_str
            alerted[sym]['near'] = today_str
        else:
            if on_cooldown(sym, 'near'):
                continue
            pct_below = (1 - row['Ratio']) * 100
            messages.append(
                f"📈 {sym}: within {pct_below:.1f}% of its 52-wk high "
                f"(${row['Close']:.2f} vs ${row['High_52w']:.2f})")
            alerted.setdefault(sym, {})['near'] = today_str

    if messages:
        with open(alerted_file, 'w') as f:
            json.dump(alerted, f, indent=2)

    return messages


def dedupe_split_messages(split_msgs: list[str], alerted_file: str) -> list[str]:
    """`splits.adjust_positions()` has no memory of its own — it reports the
    same stale-export message every single run until you re-download the
    export, which on a 3x/day schedule means the identical alert forever.
    Dedup by exact message text (matching detect_upcoming_earnings/
    detect_breakouts' pattern): a genuinely new event — a new ratio, a new
    export date — produces different text and still gets through; an
    unresolved old one only fires once."""
    if os.path.exists(alerted_file):
        with open(alerted_file) as f:
            alerted = set(json.load(f))
    else:
        alerted = set()

    new = [m for m in split_msgs if m not in alerted]
    if new:
        alerted |= set(new)
        with open(alerted_file, 'w') as f:
            json.dump(sorted(alerted), f, indent=2)
    return new


def market_closed_notice(state_file: str, today: str) -> str | None:
    """'Markets are not open.' at most once per non-trading day — the
    pipeline still runs 3x on weekends, and without dedup this would just
    replace one kind of repeat message with another."""
    if os.path.exists(state_file):
        with open(state_file) as f:
            if json.load(f).get('date') == today:
                return None
    with open(state_file, 'w') as f:
        json.dump({'date': today}, f)
    return 'Markets are not open.'


def detect_alerts(combined: pd.DataFrame, prev_snapshot_file: str,
                   earn_cache: pd.DataFrame, alerted_earnings_file: str,
                   split_table: dict | None = None) -> list[str]:
    """All v1 alert messages: big moves + upcoming earnings."""
    messages = []
    messages.extend(detect_big_moves(combined, prev_snapshot_file, split_table=split_table))
    messages.extend(detect_upcoming_earnings(earn_cache, combined, alerted_earnings_file))
    return messages

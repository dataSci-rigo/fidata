"""Split normalization: factor arithmetic, the safety check, and export dating.

The failure mode that would do real damage is double-applying a split (22 -> 440
-> 8,800), so idempotence and the verify path get the most attention here.
"""
import os

import pandas as pd
import pytest

import splits
from splits import (already_applied, extract_from_download, factor_since,
                    implied_ratio, infer_date_from_prices, load_table,
                    merge_downloaded, save_table, verify_ratio)

KORU = pd.Series([0.1, 20.0],
                 index=pd.to_datetime(['2025-02-10', '2026-07-15']))
NVDA = pd.Series([10.0], index=pd.to_datetime(['2024-06-10']))
TABLE = {'KORU': KORU, 'NVDA': NVDA}


# ── factor_since ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize('since, expected', [
    ('2026-06-18', 20.0),    # the live case: export predates the 20:1
    ('2026-07-15', 1.0),     # on the ex-date itself: already restated
    ('2026-07-16', 1.0),
    ('2025-06-01', 20.0),    # after the reverse, before the forward
    ('2024-01-01', 2.0),     # both: 0.1 * 20
    ('2026-08-13', 1.0),
])
def test_factor_since_koru(since, expected):
    assert factor_since(TABLE, 'KORU', since) == pytest.approx(expected)


def test_reverse_split_alone():
    assert factor_since(TABLE, 'KORU', '2025-01-01') == pytest.approx(2.0)
    assert factor_since({'X': pd.Series([0.1], index=pd.to_datetime(['2025-02-10']))},
                        'X', '2025-01-01') == pytest.approx(0.1)


def test_factor_is_1_for_unknown_symbol_or_no_events():
    assert factor_since(TABLE, 'AAPL', '2020-01-01') == 1.0
    assert factor_since({}, 'KORU', '2020-01-01') == 1.0


def test_unknown_date_is_treated_as_fresh():
    """None means 'age unknown'. Guessing too new is a no-op; guessing too old
    would double-apply the split."""
    assert factor_since(TABLE, 'KORU', None) == 1.0


def test_tz_aware_since_is_accepted():
    ts = pd.Timestamp('2026-06-18', tz='America/New_York')
    assert factor_since(TABLE, 'KORU', ts) == pytest.approx(20.0)


def test_applying_factor_twice_is_the_bug_we_guard_against():
    """Documents the arithmetic: 22 -> 440, never 8800."""
    qty = 22 * factor_since(TABLE, 'KORU', '2026-06-18')
    assert qty == pytest.approx(440.0)
    # after adjustment the position is in today's units, so re-deriving the
    # factor from a *current* date must be inert
    assert qty * factor_since(TABLE, 'KORU', '2026-08-13') == pytest.approx(440.0)


# ── the safety check ─────────────────────────────────────────────────────────

def test_implied_ratio_measures_the_split_from_the_export():
    # broker: 22 sh @ $1,096.44 = $24,121.68; adjusted close that day = $54.822
    assert implied_ratio(24121.68, 22, 54.822) == pytest.approx(20.0, rel=1e-3)


def test_implied_ratio_is_1_for_an_unsplit_position():
    assert implied_ratio(1000.0, 10, 100.0) == pytest.approx(1.0)


def test_implied_ratio_handles_bad_input():
    assert implied_ratio(100, 0, 10) is None
    assert implied_ratio(100, 'x', 10) is None
    assert implied_ratio(100, 10, float('nan')) is None


def test_verify_accepts_a_measured_ratio_that_matches():
    assert verify_ratio(20.0, 19.8) is True
    assert verify_ratio(0.1, 0.101) is True


def test_verify_rejects_a_restated_export():
    """A reported 20:1 but the export already measures 1:1 — applying the
    factor here would 20x a position that is already correct."""
    assert verify_ratio(20.0, 1.0) is False


def test_verify_rejects_when_unmeasurable():
    assert verify_ratio(20.0, None) is False


def test_already_applied_tolerates_dividend_drift():
    assert already_applied(1.0) is True
    assert already_applied(1.03) is True     # auto_adjust dividend effect
    assert already_applied(20.0) is False
    assert already_applied(None) is False


# ── cache round-trip ─────────────────────────────────────────────────────────

def test_cache_round_trip(tmp_path):
    path = tmp_path / 'splits.csv'
    save_table(TABLE, str(path))
    back = load_table(str(path))
    assert set(back) == {'KORU', 'NVDA'}
    assert factor_since(back, 'KORU', '2026-06-18') == pytest.approx(20.0)
    assert factor_since(back, 'NVDA', '2024-01-01') == pytest.approx(10.0)


def test_load_missing_cache_is_empty(tmp_path):
    assert load_table(str(tmp_path / 'nope.csv')) == {}


def test_merge_keeps_old_events_and_adds_new():
    """A download only covers its own window; cached history must survive."""
    downloaded = {'KORU': pd.Series([20.0], index=pd.to_datetime(['2026-07-15']))}
    merged = merge_downloaded({'KORU': KORU.iloc[:1]}, downloaded)
    assert len(merged['KORU']) == 2
    assert factor_since(merged, 'KORU', '2024-01-01') == pytest.approx(2.0)


def test_merge_is_idempotent():
    downloaded = {'KORU': pd.Series([20.0], index=pd.to_datetime(['2026-07-15']))}
    once = merge_downloaded(TABLE, downloaded)
    twice = merge_downloaded(once, downloaded)
    assert len(twice['KORU']) == len(once['KORU']) == 2
    assert factor_since(twice, 'KORU', '2026-06-18') == pytest.approx(20.0)


def test_extract_from_multi_symbol_download():
    idx = pd.to_datetime(['2026-07-14', '2026-07-15'])
    raw = pd.DataFrame(
        {('Stock Splits', 'KORU'): [0.0, 20.0],
         ('Stock Splits', 'AAPL'): [0.0, 0.0],
         ('Close', 'KORU'): [24.0, 21.8]},
        index=idx)
    raw.columns = pd.MultiIndex.from_tuples(raw.columns)
    out = extract_from_download(raw)
    assert set(out) == {'KORU'}                  # all-zero symbols dropped
    assert out['KORU'].iloc[0] == 20.0


def test_extract_from_empty_or_actionless_download():
    assert extract_from_download(None) == {}
    assert extract_from_download(pd.DataFrame()) == {}
    plain = pd.DataFrame({'Close': [1.0]}, index=pd.to_datetime(['2026-07-15']))
    assert extract_from_download(plain) == {}


# ── export dating ────────────────────────────────────────────────────────────

@pytest.fixture
def hist():
    idx = pd.bdate_range('2026-01-01', periods=150)
    return pd.DataFrame({
        'AAA': [100.0 + i * 0.5 for i in range(len(idx))],
        'BBB': [50.0 + i * 0.25 for i in range(len(idx))],
        'CCC': [10.0 + i * 0.1 for i in range(len(idx))],
    }, index=idx)


def test_infer_date_recovers_the_export_day(hist):
    target = hist.index[-30]
    prices = {c: float(hist.at[target, c]) for c in hist.columns}
    assert infer_date_from_prices(prices, hist) == target


def test_infer_date_tolerates_small_quote_drift(hist):
    target = hist.index[-40]
    prices = {c: float(hist.at[target, c]) * 1.001 for c in hist.columns}
    assert infer_date_from_prices(prices, hist) == target


def test_infer_date_gives_up_when_nothing_matches(hist):
    assert infer_date_from_prices({'AAA': 999999.0, 'BBB': 1.0, 'CCC': 5.0}, hist) is None


def test_infer_date_needs_enough_symbols(hist):
    target = hist.index[-10]
    assert infer_date_from_prices({'AAA': float(hist.at[target, 'AAA'])}, hist) is None


def test_infer_date_skips_symbols_that_split_in_the_window(hist):
    """A pre-split quote must not drag the estimate — the symbol is excluded."""
    table = {'AAA': pd.Series([20.0], index=[hist.index[-20]])}
    target = hist.index[-30]
    prices = {'AAA': float(hist.at[target, 'AAA']) * 20,   # pre-split units
              'BBB': float(hist.at[target, 'BBB']),
              'CCC': float(hist.at[target, 'CCC']),
              'AAA2': 0}
    prices.pop('AAA2')
    # with AAA excluded, BBB+CCC still date it — but that is only 2 symbols,
    # so the guard correctly declines rather than guessing
    assert infer_date_from_prices(prices, hist, table=table) is None


def test_infer_date_on_empty_history():
    assert infer_date_from_prices({'AAA': 1.0}, pd.DataFrame()) is None


# ── seeding ──────────────────────────────────────────────────────────────────

def test_needs_seed_on_a_cold_cache():
    assert splits.needs_seed({'covered_from': None, 'symbols': []},
                             ['AAA', 'BBB'], '2023-01-01') == ['AAA', 'BBB']


def test_needs_seed_for_a_newly_held_symbol():
    meta = {'covered_from': '2023-01-01', 'symbols': ['AAA']}
    assert splits.needs_seed(meta, ['AAA', 'BBB'], '2023-01-01') == ['BBB']


def test_needs_seed_when_coverage_starts_too_late():
    meta = {'covered_from': '2025-01-01', 'symbols': ['AAA']}
    assert splits.needs_seed(meta, ['AAA'], '2023-01-01') == ['AAA']


def test_no_seed_when_already_covered():
    meta = {'covered_from': '2023-01-01', 'symbols': ['AAA', 'BBB']}
    assert splits.needs_seed(meta, ['AAA', 'BBB'], '2023-01-01') == []


def test_meta_round_trip(tmp_path):
    path = str(tmp_path / 'splits.csv')
    splits.save_meta({'covered_from': '2023-01-01', 'symbols': ['BBB', 'AAA']}, path)
    m = splits.load_meta(path)
    assert m['covered_from'] == '2023-01-01' and m['symbols'] == ['AAA', 'BBB']


def test_meta_missing_is_uncovered(tmp_path):
    assert splits.load_meta(str(tmp_path / 'nope.csv'))['covered_from'] is None


# ── position adjustment ──────────────────────────────────────────────────────

def _positions(qty, price, mv):
    return {'370': pd.DataFrame({'Quantity': [qty], 'Current_Price': [price],
                                  'Market_Value': [mv]},
                                 index=pd.Index(['KORU'], name='Symbol'))}


@pytest.fixture
def hist_koru():
    idx = pd.bdate_range('2026-06-01', '2026-08-13')
    # split-adjusted series: the export's $1,103.14 is $55.157 adjusted
    return pd.DataFrame({'KORU': [55.157] * len(idx)}, index=idx)


def test_adjust_scales_a_stale_export(hist_koru):
    acc = _positions(4.0, 1103.14, 4412.56)
    out, factors, msgs = splits.adjust_positions(
        acc, {'370': pd.Timestamp('2026-06-18')}, TABLE, hist_koru)
    assert out['370'].at['KORU', 'Quantity'] == pytest.approx(80.0)
    assert factors['KORU'] == pytest.approx(20.0)
    assert any('20:1' in m and '4 -> 80' in m for m in msgs)


def test_adjust_leaves_a_post_split_export_alone(hist_koru):
    """The re-downloaded export already reports 80 shares — adjusting again
    would report 1,600."""
    acc = _positions(80.0, 55.157, 4412.56)
    out, factors, msgs = splits.adjust_positions(
        acc, {'370': pd.Timestamp('2026-08-01')}, TABLE, hist_koru)
    assert out['370'].at['KORU', 'Quantity'] == pytest.approx(80.0)
    assert factors == {}


def test_adjust_refuses_when_the_export_disagrees(hist_koru):
    """Split reported since the export date, but the export's own arithmetic
    says it is already restated. Never silently 20x a correct position."""
    acc = _positions(80.0, 55.157, 4412.56)
    out, factors, msgs = splits.adjust_positions(
        acc, {'370': pd.Timestamp('2026-06-18')}, TABLE, hist_koru)
    assert out['370'].at['KORU', 'Quantity'] == pytest.approx(80.0)
    assert factors == {}
    assert any('already reflects it' in m for m in msgs)


def test_adjust_is_idempotent(hist_koru):
    acc = _positions(4.0, 1103.14, 4412.56)
    once, _, _ = splits.adjust_positions(acc, {'370': pd.Timestamp('2026-06-18')},
                                          TABLE, hist_koru)
    twice, factors, _ = splits.adjust_positions(once, {'370': pd.Timestamp('2026-06-18')},
                                                 TABLE, hist_koru)
    assert twice['370'].at['KORU', 'Quantity'] == pytest.approx(80.0)
    assert factors == {}


def test_adjust_preserves_position_dollars(hist_koru):
    acc = _positions(4.0, 1103.14, 4412.56)
    out, _, _ = splits.adjust_positions(acc, {'370': pd.Timestamp('2026-06-18')},
                                         TABLE, hist_koru)
    row = out['370'].loc['KORU']
    assert row['Quantity'] * row['Current_Price'] == pytest.approx(4412.56, rel=1e-6)


def test_adjust_skips_when_the_export_date_is_unknown(hist_koru):
    acc = _positions(4.0, 1103.14, 4412.56)
    out, factors, _ = splits.adjust_positions(acc, {'370': None}, TABLE, hist_koru)
    assert out['370'].at['KORU', 'Quantity'] == pytest.approx(4.0)
    assert factors == {}


# ── snapshot normalization (the phantom-trade guard) ─────────────────────────

def _snap(qty, price):
    return pd.DataFrame({'Quantity': [qty], 'Price': [price]},
                        index=pd.Index(['KORU'], name='Symbol'))


def test_normalize_snapshots_removes_the_split_from_a_diff():
    """The whole point: two snapshots straddling a split must not look like a
    huge unexplained BUY to infer_missing_trades."""
    from analytics import normalize_snapshots
    snaps = {('370', pd.Timestamp('2026-06-18')): _snap(4.0, 1103.14),
             ('370', pd.Timestamp('2026-08-01')): _snap(80.0, 55.157)}
    out = normalize_snapshots(snaps, TABLE)
    before = out[('370', pd.Timestamp('2026-06-18'))].at['KORU', 'Quantity']
    after = out[('370', pd.Timestamp('2026-08-01'))].at['KORU', 'Quantity']
    assert before == pytest.approx(80.0)
    assert after == pytest.approx(80.0)
    assert after - before == pytest.approx(0.0)   # no delta -> no phantom trade


def test_normalize_snapshots_preserves_dollars():
    from analytics import normalize_snapshots
    snaps = {('370', pd.Timestamp('2026-06-18')): _snap(4.0, 1103.14)}
    row = normalize_snapshots(snaps, TABLE)[('370', pd.Timestamp('2026-06-18'))].loc['KORU']
    assert row['Quantity'] * row['Price'] == pytest.approx(4 * 1103.14)


def test_normalize_snapshots_is_a_noop_without_a_table():
    from analytics import normalize_snapshots
    snaps = {('370', pd.Timestamp('2026-06-18')): _snap(4.0, 1103.14)}
    assert normalize_snapshots(snaps, None) is snaps


# ── transaction normalization ────────────────────────────────────────────────

def test_transactions_are_restated_into_todays_shares(tmp_path):
    from parsers.transactions import load_transactions
    (tmp_path / 'Accounts_History.csv').write_text(
        'Run Date,Account Number,Action,Symbol,Quantity,Price ($)\n'
        '03/10/2026,236369828,YOU BOUGHT KORU,KORU,6,383.39\n')
    plain = load_transactions(str(tmp_path))
    adj = load_transactions(str(tmp_path), split_table=TABLE)
    assert plain.iloc[0]['Quantity'] == 6 and plain.iloc[0]['Price'] == pytest.approx(383.39)
    assert adj.iloc[0]['Quantity'] == pytest.approx(120.0)
    assert adj.iloc[0]['Price'] == pytest.approx(19.1695)
    # dollars unchanged, so capital_deployed totals do not move
    assert (adj.iloc[0]['Quantity'] * adj.iloc[0]['Price']
            == pytest.approx(plain.iloc[0]['Quantity'] * plain.iloc[0]['Price']))


def test_post_split_transactions_are_untouched(tmp_path):
    from parsers.transactions import load_transactions
    (tmp_path / 'Accounts_History.csv').write_text(
        'Run Date,Account Number,Action,Symbol,Quantity,Price ($)\n'
        '08/01/2026,236369828,YOU BOUGHT KORU,KORU,100,21.50\n')
    adj = load_transactions(str(tmp_path), split_table=TABLE)
    assert adj.iloc[0]['Quantity'] == pytest.approx(100.0)
    assert adj.iloc[0]['Price'] == pytest.approx(21.50)


# ── alert guard ──────────────────────────────────────────────────────────────

def test_big_move_alert_is_not_fooled_by_a_split(tmp_path):
    """A 20:1 makes the live price 1/20th of the stored one. Without the guard
    that reads as -95% and drowns the real alerts."""
    from alerts import detect_big_moves
    snap = tmp_path / 'last_run_snapshot.csv'
    snap.write_text('Symbol,Current_Price,Market_Value\nKORU,1103.14,4412.56\n')
    os.utime(snap, (pd.Timestamp('2026-07-01').timestamp(),) * 2)
    combined = pd.DataFrame({'Current_Price': [55.157], 'Market_Value': [4412.56]},
                            index=pd.Index(['KORU'], name='Symbol'))
    assert detect_big_moves(combined, str(snap)) != []              # unguarded: false alarm
    assert detect_big_moves(combined, str(snap), split_table=TABLE) == []


def test_a_real_move_still_alerts_through_the_guard(tmp_path):
    from alerts import detect_big_moves
    snap = tmp_path / 'last_run_snapshot.csv'
    snap.write_text('Symbol,Current_Price,Market_Value\nKORU,1103.14,4412.56\n')
    os.utime(snap, (pd.Timestamp('2026-07-01').timestamp(),) * 2)
    # post-split price 10% below the split-adjusted previous close
    combined = pd.DataFrame({'Current_Price': [55.157 * 0.90], 'Market_Value': [3971.30]},
                            index=pd.Index(['KORU'], name='Symbol'))
    msgs = detect_big_moves(combined, str(snap), split_table=TABLE)
    assert len(msgs) == 1 and '-10.0%' in msgs[0]

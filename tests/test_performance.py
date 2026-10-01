"""performance.py (is a position earning its keep) + the gain-realization
planner and holding-term classification in tax.py.

These exist because "am I up on this position" and "has this been worth
holding" are different questions. SBUX was up 71% on cost while going nowhere
for five years — the first question hid what the second one exposes.
"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import performance  # noqa: E402
import tax  # noqa: E402

IDX = pd.bdate_range('2019-01-01', '2026-09-28')


def _ramp(start, end):
    """Price series going linearly from start to end over the whole index."""
    return pd.Series(np.linspace(start, end, len(IDX)), index=IDX)


@pytest.fixture
def hist():
    # SPY doubles; XLY (the discretionary sector) rises 25%; the holdings sit
    # at various places relative to both.
    return pd.DataFrame({
        'SPY': _ramp(100, 200),      # +100% over the window
        'XLY': _ramp(100, 125),
        'XLK': _ramp(100, 300),
        'FLAT': _ramp(100, 100),     # went nowhere
        'BEATS': _ramp(100, 400),
        'SECTOR_OK': _ramp(100, 160),   # beats XLY, trails SPY
        'SHORT': pd.Series([10.0, 11.0], index=IDX[-2:]),   # no history
    })


# ── window return ────────────────────────────────────────────────────────────

def test_window_return_and_missing_history(hist):
    # SPY ramps 100 -> 200 over ~7.75 years, so the trailing 5 years is a
    # fraction of that, not the whole doubling
    five = performance.window_return(hist['SPY'], 5)
    assert five is not None and 30 < five < 100
    assert performance.window_return(hist['SPY'], 1) < five      # shorter window
    # a two-point series can't answer a 5-year question
    assert performance.window_return(hist['SHORT'], 5) is None
    assert performance.window_return(pd.Series(dtype=float), 1) is None


def test_window_return_handles_nonpositive_past():
    # the 1-year lookback lands in the zero-priced stretch
    s = pd.Series([0.0] * 600 + [10.0] * 200,
                  index=pd.bdate_range('2020-01-01', periods=800))
    assert performance.window_return(s, 1) is None       # no divide-by-zero


# ── per-position performance ─────────────────────────────────────────────────

def test_excess_against_both_spy_and_sector(hist):
    sectors = {'FLAT': 'Consumer Discretionary', 'BEATS': 'Consumer Discretionary',
               'SECTOR_OK': 'Consumer Discretionary'}
    perf = performance.position_performance(hist, sectors)
    assert perf.loc['FLAT', 'Sector_ETF'] == 'XLY'
    # flat vs a doubling market and a +25% sector: behind both
    assert perf.loc['FLAT', 'SPY_Excess_5y'] < 0
    assert perf.loc['FLAT', 'Sector_Excess_5y'] < 0
    # SECTOR_OK trails SPY but beats its sector — the case that stops a weak
    # sector being mistaken for a weak holding
    assert perf.loc['SECTOR_OK', 'SPY_Excess_5y'] < 0
    assert perf.loc['SECTOR_OK', 'Sector_Excess_5y'] > 0
    assert perf.loc['BEATS', 'SPY_Excess_5y'] > 0


def test_unknown_sector_leaves_sector_excess_none(hist):
    perf = performance.position_performance(hist, {'FLAT': 'Nonexistent Sector'})
    assert perf.loc['FLAT', 'Sector_ETF'] == ''
    assert pd.isna(perf.loc['FLAT', 'Sector_Excess_5y'])
    assert perf.loc['FLAT', 'SPY_Excess_5y'] is not None


def test_benchmark_returns_only_reports_present_symbols(hist):
    b = performance.benchmark_returns(hist)
    assert set(b) == {'SPY', 'XLY', 'XLK'}      # the rest aren't in the frame
    assert b['SPY'][5] is not None


# ── laggards ─────────────────────────────────────────────────────────────────

def test_laggards_ordering_and_filters(hist):
    sectors = {'FLAT': 'Consumer Discretionary', 'BEATS': 'Consumer Discretionary',
               'SECTOR_OK': 'Consumer Discretionary'}
    perf = performance.position_performance(hist, sectors)
    mvs = {'FLAT': 10_000, 'BEATS': 10_000, 'SECTOR_OK': 10_000}

    lag = performance.laggards(perf, mvs)
    assert [d['symbol'] for d in lag] == ['FLAT', 'SECTOR_OK']   # worst first
    assert 'BEATS' not in [d['symbol'] for d in lag]             # ahead of SPY
    assert lag[0]['behind_both'] is True
    assert next(d for d in lag if d['symbol'] == 'SECTOR_OK')['behind_both'] is False

    # both_only is the strict reading: the sector can't be the excuse
    strict = performance.laggards(perf, mvs, both_only=True)
    assert [d['symbol'] for d in strict] == ['FLAT']

    # tiny positions are noise
    assert performance.laggards(perf, {'FLAT': 100}, min_value=500) == []


def test_laggards_empty_frame():
    assert performance.laggards(pd.DataFrame(), {}) == []


# ── holding term ─────────────────────────────────────────────────────────────

def test_holding_term():
    today = pd.Timestamp('2026-09-28')
    # no traced transaction => bought before the 2023 cutoff => all long-term
    assert tax.holding_term('default_cutoff', None, today) == 'long'
    assert tax.holding_term('transaction_history',
                            pd.Timestamp('2024-01-01'), today) == 'long'
    # a purchase inside the last year leaves a short-term slice
    assert tax.holding_term('transaction_history',
                            pd.Timestamp('2026-06-01'), today) == 'mixed'
    assert tax.holding_term('transaction_history', None, today) == 'unknown'
    assert tax.holding_term(None, pd.NaT, today) == 'unknown'
    # exactly 365 days is not yet long-term
    assert tax.holding_term('transaction_history',
                            today - pd.Timedelta(days=365), today) == 'mixed'


# ── gain harvest plan ────────────────────────────────────────────────────────

def _pos(rows):
    return pd.DataFrame(
        {'Market_Value': [r[0] for r in rows.values()],
         'Cost_Basis': [r[0] - r[1] for r in rows.values()],
         'Unrealized': [r[1] for r in rows.values()]},
        index=pd.Index(list(rows), name='Symbol'))


def test_plan_stops_at_budget_and_nets_losses():
    pos = _pos({'A': (10_000, 4_000), 'LOSS': (1_000, -500),
                'B': (20_000, 8_000), 'C': (5_000, 3_000)})
    terms = dict.fromkeys(pos.index, 'long')
    plan = tax.gain_harvest_plan(pos, ['A', 'LOSS', 'B', 'C'], 10_000, terms)
    # 4000 - 500 + 8000 = 11500 would exceed 10000, so B is dropped, C fits
    assert [s['symbol'] for s in plan['sell']] == ['A', 'LOSS', 'C']
    assert plan['gain_realized'] == 6_500          # 4000 - 500 + 3000
    assert plan['capital_freed'] == 16_000
    assert plan['headroom'] == 3_500
    assert [e['symbol'] for e in plan['excluded']] == ['B']
    assert plan['excluded'][0]['reason'] == 'would exceed the budget'


def test_plan_excludes_non_long_term():
    pos = _pos({'OLD': (10_000, 2_000), 'NEW': (10_000, 5_000)})
    plan = tax.gain_harvest_plan(pos, ['NEW', 'OLD'], 50_000,
                                {'OLD': 'long', 'NEW': 'mixed'})
    assert [s['symbol'] for s in plan['sell']] == ['OLD']
    assert plan['excluded'][0] == {'symbol': 'NEW', 'market_value': 10_000,
                                   'gain': 5_000, 'term': 'mixed'}


def test_plan_flags_a_dominating_position():
    pos = _pos({'BIG': (50_000, 40_000), 'SMALL': (2_000, 500),
                'TINY': (1_000, 300)})
    terms = dict.fromkeys(pos.index, 'long')
    plan = tax.gain_harvest_plan(pos, ['BIG', 'SMALL', 'TINY'], 50_000, terms)
    dom = plan['dominated_by']
    assert dom['symbol'] == 'BIG' and dom['share_pct'] > 90
    assert dom['without_it'] == {'gain_realized': 800, 'capital_freed': 3_000,
                                 'positions': 2}


def test_plan_without_domination_has_no_flag():
    pos = _pos({'A': (10_000, 1_000), 'B': (10_000, 1_100), 'C': (9_000, 900)})
    plan = tax.gain_harvest_plan(pos, ['A', 'B', 'C'], 50_000,
                                dict.fromkeys(pos.index, 'long'))
    assert 'dominated_by' not in plan          # evenly spread


def test_plan_needs_three_sales_before_flagging_domination():
    """With two sales one always holds >50% of the gain, which says nothing."""
    pos = _pos({'A': (10_000, 1_000), 'B': (10_000, 9_000)})
    plan = tax.gain_harvest_plan(pos, ['A', 'B'], 50_000,
                                dict.fromkeys(pos.index, 'long'))
    assert 'dominated_by' not in plan


def test_plan_skips_unknown_symbols_and_nan_basis():
    pos = _pos({'A': (10_000, 1_000)})
    pos.loc['NOBASIS'] = [5_000, float('nan'), float('nan')]
    plan = tax.gain_harvest_plan(pos, ['GHOST', 'NOBASIS', 'A'], 50_000,
                                {'A': 'long', 'NOBASIS': 'long'})
    assert [s['symbol'] for s in plan['sell']] == ['A']


def test_plan_empty_order():
    plan = tax.gain_harvest_plan(_pos({'A': (1, 1)}), [], 50_000, {})
    assert plan['sell'] == [] and plan['gain_realized'] == 0


# ── California rate schedule ─────────────────────────────────────────────────

def test_ca_tax_brackets_and_deduction():
    # the standard deduction plus the exemption credit absorb the bottom
    # entirely: a small gain in a no-income year costs nothing in CA
    assert tax.ca_tax(0) == 0.0
    assert tax.ca_tax(5_000) == 0.0
    assert tax.ca_tax(10_000) == 0.0
    # hand-checked against FTB Schedule X TY2025: 1% to 11,079, 2% to 26,264,
    # 4% to 41,452, 6% above, on (50,000 - 5,706) = 44,294 taxable, less $153
    expected = (11_079 * .01 + (26_264 - 11_079) * .02
                + (41_452 - 26_264) * .04 + (44_294 - 41_452) * .06) - 153
    assert tax.ca_tax(50_000) == pytest.approx(expected, abs=0.01)
    # monotonic, and never negative from the credit
    assert tax.ca_tax(1_000_000) > tax.ca_tax(500_000) > tax.ca_tax(100_000)


def test_ca_mfj_doubles_every_edge():
    # R&TC 17045 makes joint tax exactly twice the tax on half the income —
    # exact at all eight edges, not an approximation
    assert tax.ca_tax(80_000, 'mfj') == pytest.approx(tax.ca_tax(40_000) * 2, abs=0.01)
    assert tax.ca_tax(80_000, 'mfj') < tax.ca_tax(80_000, 'single')
    assert tax.ca_marginal_rate(80_000, 'mfj') < tax.ca_marginal_rate(80_000)
    # MFS uses the single schedule
    assert tax.ca_tax(80_000, 'mfs') == tax.ca_tax(80_000, 'single')


def test_ca_refuses_head_of_household_rather_than_guessing():
    """Schedule Z is not a multiple of Schedule X — silently returning single
    brackets is how a HoH return gets a wrong number with no warning."""
    with pytest.raises(ValueError, match='Schedule Z'):
        tax.ca_tax(80_000, 'hoh')
    with pytest.raises(ValueError, match='unknown filing status'):
        tax.ca_tax(80_000, 'widowed')


def test_ca_marginal_rate_steps():
    assert tax.ca_marginal_rate(0) == 0.01          # next dollar, not zero
    assert tax.ca_marginal_rate(50_000) == 0.06
    assert tax.ca_marginal_rate(80_000) == 0.093
    assert tax.ca_marginal_rate(2_000_000) == pytest.approx(0.133)   # +1% BHST
    # The BHST threshold is flat for every status — R&TC 17043 switches off
    # both R&TC 17041 indexing and R&TC 17045 joint doubling. At $1.1M joint,
    # taxable income clears $1M and sits in the 11.3% bracket, so the surtax
    # applies: 12.3%. If the threshold doubled with the schedule it would be
    # 11.3%, which is the error this guards against.
    assert tax.ca_marginal_rate(1_100_000, 'mfj') == pytest.approx(0.123)


def test_state_tax_on_gain_stacks_on_other_income():
    """The whole point: the same sale is cheap alone and expensive on a salary."""
    alone = tax.state_tax_on_gain(50_000, 0)
    stacked = tax.state_tax_on_gain(50_000, 75_000)
    assert alone['effective_pct'] < 3        # ~2%
    assert stacked['effective_pct'] > 9      # ~9.3%, over 4x the bill
    assert stacked['state_tax_on_gain'] > alone['state_tax_on_gain'] * 3
    # it is a difference of two bills, not a flat rate on the gain
    assert stacked['tax_with_gain'] - stacked['tax_without_gain'] == \
        stacked['state_tax_on_gain']
    assert tax.state_tax_on_gain(0, 0)['effective_pct'] == 0.0   # no div by zero


def test_federal_0pct_capacity_includes_the_deduction():
    # the 0% ceiling is measured on TAXABLE income, so the standard deduction
    # sits underneath it — capacity is larger than the bracket top itself
    cap = tax.federal_0pct_capacity(0)
    assert cap == pytest.approx(tax.FED_LTCG_0_TOP['single']
                                + tax.FED_STANDARD_DEDUCTION['single'])
    assert cap == pytest.approx(65_550)    # TY2026 single
    assert cap > 50_000            # the user's rule of thumb is conservative
    # ordinary income eats it dollar for dollar, and it floors at zero
    assert tax.federal_0pct_capacity(20_000) == pytest.approx(cap - 20_000)
    assert tax.federal_0pct_capacity(500_000) == 0.0
    assert tax.federal_0pct_capacity(0, 'mfj') == pytest.approx(cap * 2)
    # HoH is NOT double and NOT single — it has its own figures federally
    assert tax.federal_0pct_capacity(0, 'hoh') == pytest.approx(66_200 + 24_150)
    # §63(f) aged/blind raises it; the constant is a floor, not the number
    assert tax.federal_0pct_capacity(0, deduction=16_100 + 2_050) == \
        pytest.approx(67_600)


# ── federal rate schedule on a gain ──────────────────────────────────────────

def test_federal_boundaries_are_marginal_not_cliffs():
    """One dollar over the 0% ceiling costs fifteen cents, not 15% of the lot.
    Getting this wrong turns a smooth schedule into a phantom cliff."""
    cap = tax.federal_0pct_capacity(0)
    assert tax.federal_tax_on_gain(cap)['federal_tax_on_gain'] == 0
    just_over = tax.federal_tax_on_gain(cap + 1_000)
    assert just_over['federal_tax_on_gain'] == pytest.approx(150, abs=1)
    assert just_over['taxed_at_0'] == pytest.approx(49_450)
    assert just_over['taxed_at_15'] == pytest.approx(1_000)


def test_federal_ordinary_income_fills_the_zero_band_first():
    # the gain stacks on top: other income consumes the 0% room
    with_income = tax.federal_tax_on_gain(50_000, 50_000)
    alone = tax.federal_tax_on_gain(50_000, 0)
    assert alone['federal_tax_on_gain'] == 0
    assert with_income['taxed_at_15'] > 0
    # $50k other income leaves 65,550-50,000 = 15,550 of 0% room
    assert with_income['taxed_at_0'] == pytest.approx(15_550)
    assert with_income['taxed_at_15'] == pytest.approx(34_450)


def test_niit_runs_on_magi_and_is_never_indexed():
    """NIIT is the biggest thing the tool used to miss: it bites at $200k of
    MAGI, far below the 20% bracket's $545,500 of taxable income."""
    assert tax.niit(100_000, 0) == 0.0                 # under the threshold
    # $250k gain, no other income: $50k of MAGI excess, all of it gain
    assert tax.niit(250_000, 0) == pytest.approx(0.038 * 50_000)
    # it is the LESSER of gain and the excess — a small gain on a big salary
    # is capped by the gain, not by the excess
    assert tax.niit(10_000, 500_000) == pytest.approx(0.038 * 10_000)
    assert tax.niit(250_000, 0, 'mfj') == pytest.approx(0.038 * 0)   # 250k thr


def test_niit_lands_before_the_20_percent_bracket():
    # at $238,817 of gain and no other income the 20% bracket is nowhere near,
    # but NIIT is already running — this is exactly the gap that was missed
    f = tax.federal_tax_on_gain(238_817, 0)
    assert f['taxed_at_20'] == 0
    assert f['niit'] > 1_400
    assert f['niit'] == pytest.approx(0.038 * 38_817, abs=1)


def test_combined_tax_pairs_both_jurisdictions():
    c = tax.combined_tax_on_gain(49_648, 0)
    assert c['federal']['federal_tax_on_gain'] == 0      # inside the 0% band
    assert c['state']['state_tax_on_gain'] > 0           # CA still charges
    assert c['total_tax'] == c['state']['state_tax_on_gain']
    assert c['net_proceeds_pct'] + c['effective_pct'] == pytest.approx(100)
    assert c['filing'] == 'single'

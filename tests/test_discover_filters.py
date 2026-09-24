"""discover_filters: the UI/AI/engine contract — pure pandas, no I/O."""
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from discover_filters import (  # noqa: E402
    apply_filters, params_from_args, tool_input_schema, validate_params,
)


@pytest.fixture
def uni() -> pd.DataFrame:
    rows = [
        # Symbol, Quote_Type, Asset_Class, Sector, Industry, Category, Summary
        dict(Symbol='NVDA', Quote_Type='EQUITY', Asset_Class='Equity',
             Sector='Information Technology', Industry='Semiconductors',
             Category='', Summary='Artificial intelligence accelerators',
             In_SP500=True, In_NDX=True, MarketCap=3e12, Trailing_PE=60.0,
             Dividend_Yield=0.0003, Expense_Ratio=None, Ann_Vol=0.50,
             Sharpe_1yr=2.0, Beta=1.8, Gain_1yr=80.0, Max_Drawdown=-0.30,
             High_52w_Ratio=0.98, Uptrend=True, Corr_Portfolio=0.80,
             Avg_Dollar_Vol=3e10, Name='NVIDIA'),
        dict(Symbol='KO', Quote_Type='EQUITY', Asset_Class='Equity',
             Sector='Consumer Staples', Industry='Beverages', Category='',
             Summary='Beverage company', In_SP500=True, In_NDX=False,
             MarketCap=2.8e11, Trailing_PE=25.0, Dividend_Yield=0.030,
             Expense_Ratio=None, Ann_Vol=0.15, Sharpe_1yr=0.8, Beta=0.5,
             Gain_1yr=10.0, Max_Drawdown=-0.10, High_52w_Ratio=0.90,
             Uptrend=False, Corr_Portfolio=0.40, Avg_Dollar_Vol=8e8,
             Name='Coca-Cola'),
        dict(Symbol='TLT', Quote_Type='ETF', Asset_Class='Bond',
             Sector='Fixed Income', Industry='', Category='Long Treasury',
             Summary='20+ year treasury bonds', In_SP500=False, In_NDX=False,
             MarketCap=None, Trailing_PE=None, Dividend_Yield=0.037,
             Expense_Ratio=0.0015, Ann_Vol=0.15, Sharpe_1yr=-0.2, Beta=-0.1,
             Gain_1yr=-2.0, Max_Drawdown=-0.12, High_52w_Ratio=0.85,
             Uptrend=False, Corr_Portfolio=-0.30, Avg_Dollar_Vol=2e9,
             Name='iShares 20+ Year Treasury'),
        dict(Symbol='GLD', Quote_Type='ETF', Asset_Class='Commodity',
             Sector='Commodity', Industry='', Category='Gold',
             Summary='Physical gold trust', In_SP500=False, In_NDX=False,
             MarketCap=None, Trailing_PE=None, Dividend_Yield=0.0,
             Expense_Ratio=0.004, Ann_Vol=0.14, Sharpe_1yr=1.1, Beta=0.1,
             Gain_1yr=25.0, Max_Drawdown=-0.08, High_52w_Ratio=0.99,
             Uptrend=True, Corr_Portfolio=0.05, Avg_Dollar_Vol=1.5e9,
             Name='SPDR Gold Shares'),
        dict(Symbol='BRK-B', Quote_Type='EQUITY', Asset_Class='Equity',
             Sector='Financials', Industry='Insurance', Category='',
             Summary='Conglomerate', In_SP500=True, In_NDX=False,
             MarketCap=9e11, Trailing_PE=22.0, Dividend_Yield=0.0,
             Expense_Ratio=None, Ann_Vol=0.18, Sharpe_1yr=1.0, Beta=0.9,
             Gain_1yr=15.0, Max_Drawdown=-0.09, High_52w_Ratio=0.95,
             Uptrend=True, Corr_Portfolio=0.70, Avg_Dollar_Vol=5e8,
             Name='Berkshire Hathaway'),
        dict(Symbol='SPARSE', Quote_Type='EQUITY', Asset_Class='Equity',
             Sector='Industrials', Industry='', Category='', Summary='',
             In_SP500=False, In_NDX=False, MarketCap=1e9, Trailing_PE=None,
             Dividend_Yield=None, Expense_Ratio=None, Ann_Vol=None,
             Sharpe_1yr=None, Beta=None, Gain_1yr=None, Max_Drawdown=None,
             High_52w_Ratio=None, Uptrend=False, Corr_Portfolio=None,
             Avg_Dollar_Vol=None, Name='Sparse Data Co'),
        dict(Symbol='WCHD', Quote_Type='EQUITY', Asset_Class='Equity',
             Sector='Health Care', Industry='Biotech', Category='',
             Summary='Watched biotech', In_SP500=False, In_NDX=False,
             MarketCap=5e9, Trailing_PE=30.0, Dividend_Yield=0.0,
             Expense_Ratio=None, Ann_Vol=0.40, Sharpe_1yr=0.5, Beta=1.2,
             Gain_1yr=5.0, Max_Drawdown=-0.20, High_52w_Ratio=0.80,
             Uptrend=False, Corr_Portfolio=0.30, Avg_Dollar_Vol=1e8,
             Name='Watched Co'),
    ]
    return pd.DataFrame(rows)


def syms(df):
    return list(df['Symbol'])


def test_no_params_returns_all_sorted_by_sharpe(uni):
    out = apply_filters(uni, {})
    assert syms(out)[0] == 'NVDA'          # highest Sharpe first
    assert 'SPARSE' in syms(out)           # NaN rows kept when no bound active
    assert list(out['Symbol'])[-1] == 'SPARSE'  # NaN sorts last


def test_range_bounds_and_nan_exclusion(uni):
    out = apply_filters(uni, {'ann_vol_max': 0.2})
    assert set(syms(out)) == {'KO', 'TLT', 'GLD', 'BRK-B'}
    assert 'SPARSE' not in syms(out)       # NaN excluded while bound active
    out = apply_filters(uni, {'ann_vol_min': 0.3, 'ann_vol_max': 0.6})
    assert set(syms(out)) == {'NVDA', 'WCHD'}


def test_max_drawdown_sign_semantics(uni):
    # min=-0.25: drawdowns no worse than -25% → NVDA (-0.30) drops out
    out = apply_filters(uni, {'max_drawdown_min': -0.25})
    assert 'NVDA' not in syms(out)
    assert 'KO' in syms(out)


def test_pe_bound_naturally_drops_etfs(uni):
    out = apply_filters(uni, {'pe_trailing_max': 40})
    assert set(syms(out)) == {'KO', 'BRK-B', 'WCHD'}


def test_categoricals(uni):
    assert set(syms(apply_filters(uni, {'quote_types': ['ETF']}))) == {'TLT', 'GLD'}
    assert set(syms(apply_filters(uni, {'asset_classes': ['Bond']}))) == {'TLT'}
    assert set(syms(apply_filters(uni, {'sectors': ['Consumer Staples']}))) == {'KO'}
    out = apply_filters(uni, {'exclude_sectors': ['Information Technology',
                                                  'Financials']})
    assert 'NVDA' not in syms(out) and 'BRK-B' not in syms(out)


def test_text_any_or_and_case(uni):
    out = apply_filters(uni, {'text_any': ['ARTIFICIAL INTELLIGENCE']})
    assert syms(out) == ['NVDA']
    out = apply_filters(uni, {'text_any': ['treasury', 'gold']})
    assert set(syms(out)) == {'TLT', 'GLD'}


def test_tristate_index_membership(uni):
    assert set(syms(apply_filters(uni, {'in_sp500': True}))) == {'NVDA', 'KO', 'BRK-B'}
    out = apply_filters(uni, {'in_sp500': False})
    assert set(syms(out)) == {'TLT', 'GLD', 'SPARSE', 'WCHD'}
    assert len(apply_filters(uni, {})) == 7      # absent = don't care


def test_uptrend_only(uni):
    assert set(syms(apply_filters(uni, {'uptrend_only': True}))) == \
        {'NVDA', 'GLD', 'BRK-B'}


def test_exclude_held_with_slash_dash_normalization(uni):
    held = frozenset({'BRK/B', 'NVDA'})    # broker spelling for BRK
    out = apply_filters(uni, {}, held=held)      # exclude_held defaults True
    assert 'BRK-B' not in syms(out) and 'NVDA' not in syms(out)
    out = apply_filters(uni, {'exclude_held': False}, held=held)
    assert bool(out.set_index('Symbol').loc['BRK-B', 'Held']) is True
    assert bool(out.set_index('Symbol').loc['KO', 'Held']) is False


def test_exclude_watchlist_and_badge(uni):
    watching = frozenset({'wchd'})
    out = apply_filters(uni, {}, watching=watching)
    assert bool(out.set_index('Symbol').loc['WCHD', 'In_Watchlist']) is True
    out = apply_filters(uni, {'exclude_watchlist': True}, watching=watching)
    assert 'WCHD' not in syms(out)


def test_sort_and_limit(uni):
    out = apply_filters(uni, {'sort_by': 'ann_vol', 'sort_desc': False, 'limit': 2})
    assert syms(out) == ['GLD', 'KO']
    out = apply_filters(uni, {'sort_by': 'symbol', 'sort_desc': False})
    assert syms(out)[0] == 'BRK-B'


def test_validate_params():
    clean, warns = validate_params({
        'ann_vol_max': '0.2', 'limit': 999, 'sort_by': 'nope',
        'text_any': 'ai', 'bogus_key': 1, 'in_sp500': 'yes',
        'beta_min': 'not-a-number', 'sectors': [' Tech ', ''],
    })
    assert clean['ann_vol_max'] == 0.2
    assert clean['limit'] == 200
    assert 'sort_by' not in clean
    assert clean['text_any'] == ['ai']
    assert clean['in_sp500'] is True
    assert clean['sectors'] == ['Tech']
    assert 'bogus_key' not in clean and 'beta_min' not in clean
    assert len(warns) == 3


def test_params_from_args_roundtrip():
    from werkzeug.datastructures import MultiDict
    args = MultiDict([('_form', '1'), ('text_any', 'ai, robotics'),
                      ('sectors', 'Information Technology'),
                      ('sectors', 'Health Care'), ('ann_vol_max', '0.3'),
                      ('in_sp500', 'yes'), ('in_ndx', ''), ('limit', '25'),
                      ('uptrend_only', 'on'), ('sort_by', 'gain_1yr')])
    raw = params_from_args(args)
    clean, warns = validate_params(raw)
    assert clean['text_any'] == ['ai', 'robotics']
    assert clean['sectors'] == ['Information Technology', 'Health Care']
    assert clean['ann_vol_max'] == 0.3
    assert clean['in_sp500'] is True and 'in_ndx' not in clean
    assert clean['uptrend_only'] is True
    # _form present + checkbox absent → explicit False (overrides default True)
    assert clean['exclude_held'] is False
    assert clean['limit'] == 25 and clean['sort_by'] == 'gain_1yr'
    assert warns == []


def test_params_from_args_without_form_marker_leaves_defaults():
    from werkzeug.datastructures import MultiDict
    raw = params_from_args(MultiDict([('ann_vol_max', '0.3')]))
    assert 'exclude_held' not in raw       # fresh visit → DEFAULTS apply later


def test_tool_input_schema_shape():
    schema = tool_input_schema()
    assert schema['required'] == ['rationale']
    assert schema['additionalProperties'] is False
    props = schema['properties']
    assert 'ann_vol_max' in props and 'corr_portfolio_max' in props
    assert props['quote_types']['items']['enum'] == ['EQUITY', 'ETF']
    assert 'sharpe_1yr' in props['sort_by']['enum']

"""tax.py + analytics.broker_cost_basis.

Regression cover for the review recommending tax-loss harvesting on positions
held inside IRAs, and for a cost-basis loader that only recognised Fidelity's
column name so ~25% of the book fell back to a 2023 placeholder.
"""
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tax  # noqa: E402
from analytics import broker_cost_basis  # noqa: E402

ENV = {'ACC_TAXABLE': '137,472,4919',
       'ACC_TAX_ADVANTAGED': '370,9828,898'}


def _pos(rows):
    """rows: {symbol: (qty, market_value, cost_basis)}"""
    idx = pd.Index(list(rows), name='Symbol')
    return pd.DataFrame(
        {'Quantity': [r[0] for r in rows.values()],
         'Market_Value': [r[1] for r in rows.values()],
         'Current_Price': [(r[1] / r[0] if r[0] else 0) for r in rows.values()],
         'Cost_Basis': [r[2] for r in rows.values()]}, index=idx)


@pytest.fixture
def accounts():
    return {
        '137': _pos({'AAPL': (100, 30000, 4500), 'SEVN': (100, 821, 1296),
                     'cash': (None, 5000, None)}),           # taxable
        '4919': _pos({'AVGO': (20, 8378, 1604),
                      'META': (5, 2510, 1649)}),             # taxable
        '9828': _pos({'KORU': (500, 19736, 40000),
                      'META': (10, 4032, 9000)}),            # IRA
        '898': _pos({'QQQ': (15, 11109, 9000)}),             # rollover
    }


# ── account status ───────────────────────────────────────────────────────────

def test_account_status_and_unknown():
    st = tax.account_status(ENV)
    assert st['137'] == tax.TAXABLE and st['4919'] == tax.TAXABLE
    assert st['9828'] == tax.TAX_ADVANTAGED and st['898'] == tax.TAX_ADVANTAGED
    # an account in neither list must not silently default to taxable
    assert tax.status_of('99999', ENV) == tax.UNKNOWN
    assert tax.taxable_accounts(ENV) == frozenset({'137', '472', '4919'})


def test_status_tolerates_quotes_and_spaces():
    st = tax.account_status({'ACC_TAXABLE': "'137' , 472 "})
    assert st['137'] == tax.TAXABLE and st['472'] == tax.TAXABLE


def test_account_summary(accounts):
    rows = tax.account_summary(accounts, ENV)
    assert [r['account'] for r in rows][0] == '137'          # sorted by value
    by = {r['account']: r for r in rows}
    assert by['137']['market_value'] == 35821.0              # includes cash
    assert by['137']['positions'] == 2                       # excludes cash
    assert by['9828']['status'] == tax.TAX_ADVANTAGED


# ── the actual fix ───────────────────────────────────────────────────────────

def test_taxable_positions_excludes_retirement_accounts(accounts):
    pos = tax.taxable_positions(accounts, ENV)
    # KORU is IRA-only: it must not appear at all
    assert 'KORU' not in pos.index
    assert 'QQQ' not in pos.index                            # rollover-only
    assert set(pos.index) == {'AAPL', 'SEVN', 'AVGO', 'META'}
    assert 'cash' not in pos.index


def test_taxable_positions_only_counts_the_taxable_slice(accounts):
    """META is held in both a taxable and an IRA account — only the taxable
    lots may inform a tax decision."""
    pos = tax.taxable_positions(accounts, ENV)
    assert pos.loc['META', 'Market_Value'] == 2510           # not 2510 + 4032
    assert pos.loc['META', 'Cost_Basis'] == 1649
    assert pos.loc['META', 'Unrealized'] == 861              # a GAIN, not a loss


def test_harvest_candidates_respects_threshold_and_status(accounts):
    cands = tax.harvest_candidates(accounts, ENV, min_loss=250)
    assert [c['symbol'] for c in cands] == ['SEVN']
    assert cands[0]['unrealized'] == pytest.approx(-475.0, abs=1)
    assert cands[0]['return_pct'] == pytest.approx(-36.65, abs=0.1)
    # a big IRA loss is still never a candidate
    assert 'KORU' not in [c['symbol'] for c in cands]
    # threshold filters out the trivial ones
    assert tax.harvest_candidates(accounts, ENV, min_loss=1000) == []


def test_no_taxable_accounts_yields_empty_not_crash(accounts):
    pos = tax.taxable_positions(accounts, {'ACC_TAXABLE': ''})
    assert pos.empty and list(pos.columns)[:3] == ['Quantity', 'Market_Value',
                                                   'Cost_Basis']
    assert tax.harvest_candidates(accounts, {'ACC_TAXABLE': ''}) == []


def test_missing_cost_basis_column_is_tolerated():
    acct = {'137': pd.DataFrame(
        {'Quantity': [10], 'Market_Value': [100], 'Current_Price': [10]},
        index=pd.Index(['AAA'], name='Symbol'))}
    pos = tax.taxable_positions(acct, ENV)
    assert pd.isna(pos.loc['AAA', 'Cost_Basis'])
    assert tax.harvest_candidates(acct, ENV) == []            # never guesses


def test_brk_slash_b_normalized():
    acct = {'137': _pos({'BRK/B': (10, 4000, 3000)})}
    pos = tax.taxable_positions(acct, ENV)
    assert 'BRK-B' in pos.index and 'BRK/B' not in pos.index


# ── broker cost basis ────────────────────────────────────────────────────────

def test_broker_cost_basis_pools_across_accounts(accounts):
    cb = broker_cost_basis(accounts)
    # META: (1649 + 9000) / (5 + 10) = 709.93/share — one weighted figure
    assert cb['META'] == pytest.approx((1649 + 9000) / 15)
    assert cb['AAPL'] == pytest.approx(45.0)
    assert 'cash' not in cb


def test_broker_cost_basis_edge_cases():
    assert broker_cost_basis({}) == {}
    no_col = {'1': pd.DataFrame({'Quantity': [1], 'Market_Value': [2],
                                 'Current_Price': [2]},
                                index=pd.Index(['A'], name='Symbol'))}
    assert broker_cost_basis(no_col) == {}
    # NaN basis and zero quantity are skipped rather than producing inf/NaN
    messy = {'1': _pos({'A': (0, 10, 5), 'B': (10, 10, float('nan')),
                        'C': (4, 40, 20)})}
    cb = broker_cost_basis(messy)
    assert cb == {'C': pytest.approx(5.0)}


def test_broker_cost_basis_keeps_both_brk_spellings():
    cb = broker_cost_basis({'1': _pos({'BRK/B': (10, 4000, 3000)})})
    assert cb['BRK/B'] == pytest.approx(300.0)
    assert cb['BRK-B'] == pytest.approx(300.0)   # combined uses the dash form


def test_partial_cost_basis_is_treated_as_unknown():
    """One account reports basis, another doesn't. Summing only the known lot
    would understate cost and show a phantom gain, hiding a real loss."""
    accts = {'137': _pos({'ZZZ': (10, 1000, 4000)}),          # real loss
             '472': _pos({'ZZZ': (10, 1000, float('nan'))})}
    pos = tax.taxable_positions(accts, ENV)
    assert pd.isna(pos.loc['ZZZ', 'Cost_Basis'])
    assert pd.isna(pos.loc['ZZZ', 'Unrealized'])
    assert tax.harvest_candidates(accts, ENV) == []            # refuses to guess

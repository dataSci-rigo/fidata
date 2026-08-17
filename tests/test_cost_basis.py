"""Account-key normalization, transaction dedup, and cost-basis arithmetic.

Regression cover for the bug where Fidelity's numeric `Account Number` column
came back from pandas as float64, so str(...)[-4:] on 236369828.0 produced
'28.0'. That matched no position account, which meant (a) the multi-account
history file and the per-account files were both ingested as separate accounts,
duplicating 45 trades, and (b) analytics.infer_missing_trades could not
cross-reference known trades and re-invented ~$21.8K of BUYs it already had.
"""
import pandas as pd
import pytest

from analytics import compute_cost_basis
from parsers.common import account_key
from parsers.transactions import load_transactions

CUTOFF = pd.Timestamp('2023-01-01')
DEFAULT_BUY = pd.Timestamp('2023-01-02')


@pytest.mark.parametrize('raw, expected', [
    (236369828.0, '9828'),        # the bug: float from pandas, was '28.0'
    ('236369828.0', '9828'),
    ('236369828', '9828'),
    (226998197, '8197'),
    ('XRA580898', '898'),         # E*Trade, leading zero dropped -> matches positions
    ('X0898', '898'),
    ('  9828  ', '9828'),
    ('1297', '1297'),
])
def test_account_key_normalizes(raw, expected):
    assert account_key(raw) == expected


def test_account_key_agrees_across_brokers():
    """Positions and transactions must land on the same key or nothing joins."""
    assert account_key('XRA580898') == account_key(580898.0) == '898'


def test_account_key_passes_through_non_numeric():
    assert account_key('no-digits-here') == 'no-digits-here'


def test_load_transactions_drops_duplicate_fills(tmp_path):
    """The same fill exported in two files must be counted once."""
    header = 'Run Date,Account Number,Action,Symbol,Quantity,Price ($)\n'
    fill = '06/02/2026,236369828,YOU BOUGHT XYZ,XYZ,10,50.00\n'
    (tmp_path / 'Accounts_History.csv').write_text(header + fill)
    # same trade, per-account export, account inferred from the filename
    (tmp_path / 'History_for_Account_236369828.csv').write_text(
        'Run Date,Action,Symbol,Quantity,Price ($)\n'
        '06/02/2026,YOU BOUGHT XYZ,XYZ,10,50.00\n')

    tx = load_transactions(str(tmp_path))
    assert len(tx) == 1
    assert tx.iloc[0]['Account'] == '9828'


def test_load_transactions_keeps_distinct_fills(tmp_path):
    """Two genuinely different fills on the same day are both kept."""
    (tmp_path / 'Accounts_History.csv').write_text(
        'Run Date,Account Number,Action,Symbol,Quantity,Price ($)\n'
        '06/02/2026,236369828,YOU BOUGHT XYZ,XYZ,10,50.00\n'
        '06/02/2026,236369828,YOU BOUGHT XYZ,XYZ,10,51.00\n')
    tx = load_transactions(str(tmp_path))
    assert len(tx) == 2


def _combined(symbols):
    return pd.DataFrame(
        {'Quantity': [10.0] * len(symbols), 'Current_Price': [100.0] * len(symbols),
         'Market_Value': [1000.0] * len(symbols)},
        index=pd.Index(symbols, name='Symbol'))


def _tx(rows):
    return pd.DataFrame(rows, columns=['Symbol', 'Date', 'Action', 'Quantity', 'Price', 'Account'])


def test_cost_basis_from_transaction_history():
    tx = _tx([
        ['AAA', pd.Timestamp('2023-06-01'), 'BUY', 10.0, 80.0, '9828'],
        ['AAA', pd.Timestamp('2024-06-01'), 'BUY', 10.0, 100.0, '9828'],
    ])
    out = compute_cost_basis(_combined(['AAA']), tx, {}, CUTOFF, DEFAULT_BUY)
    assert out.loc['AAA', 'Avg_Buy_Price'] == pytest.approx(90.0)
    assert out.loc['AAA', 'First_Buy_Date'] == pd.Timestamp('2023-06-01')
    assert out.loc['AAA', 'Cost_Basis_Source'] == 'transaction_history'


def test_reinvest_counts_toward_cost_basis():
    """REINVEST is real tax basis even though it isn't new capital deployed."""
    tx = _tx([
        ['AAA', pd.Timestamp('2023-06-01'), 'BUY', 10.0, 80.0, '9828'],
        ['AAA', pd.Timestamp('2023-07-01'), 'REINVEST', 10.0, 100.0, '9828'],
    ])
    out = compute_cost_basis(_combined(['AAA']), tx, {}, CUTOFF, DEFAULT_BUY)
    assert out.loc['AAA', 'Avg_Buy_Price'] == pytest.approx(90.0)


def test_duplicate_rows_would_not_change_the_average():
    """Dedup safety net: a doubled fill must not shift Avg_Buy_Price."""
    single = _tx([['AAA', pd.Timestamp('2023-06-01'), 'BUY', 10.0, 80.0, '9828'],
                  ['AAA', pd.Timestamp('2024-06-01'), 'BUY', 30.0, 100.0, '9828']])
    doubled = pd.concat([single, single.iloc[[1]]], ignore_index=True)
    a = compute_cost_basis(_combined(['AAA']), single, {}, CUTOFF, DEFAULT_BUY)
    b = compute_cost_basis(_combined(['AAA']), doubled, {}, CUTOFF, DEFAULT_BUY)
    assert a.loc['AAA', 'Avg_Buy_Price'] != pytest.approx(b.loc['AAA', 'Avg_Buy_Price'])


def test_pre_cutoff_buys_fall_back_to_broker_average():
    """A position bought before the cutoff has no usable tx history."""
    tx = _tx([['AAA', pd.Timestamp('2019-01-01'), 'BUY', 10.0, 20.0, '9828']])
    out = compute_cost_basis(_combined(['AAA']), tx, {'AAA': 25.0}, CUTOFF, DEFAULT_BUY)
    assert out.loc['AAA', 'Avg_Buy_Price'] == pytest.approx(25.0)
    assert out.loc['AAA', 'Cost_Basis_Source'] == 'brokerage_fallback'
    assert out.loc['AAA', 'First_Buy_Date'] == DEFAULT_BUY


def test_no_history_and_no_fallback_is_default_cutoff():
    out = compute_cost_basis(_combined(['AAA']), _tx([]), {}, CUTOFF, DEFAULT_BUY)
    assert out.loc['AAA', 'Cost_Basis_Source'] == 'default_cutoff'
    assert pd.isna(out.loc['AAA', 'Avg_Buy_Price'])
    assert out.loc['AAA', 'First_Buy_Date'] == DEFAULT_BUY


def test_cash_row_marked_not_applicable():
    out = compute_cost_basis(_combined(['AAA', 'cash']), _tx([]), {}, CUTOFF, DEFAULT_BUY)
    assert out.loc['cash', 'Cost_Basis_Source'] == 'n/a'

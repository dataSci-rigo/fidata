import os

from parsers import detect_format
from parsers.schwab import parse_all_accounts, parse_single_account


def test_detect_schwab_all(fixtures_dir):
    fp = os.path.join(fixtures_dir, 'schwab_all_accounts.csv')
    assert detect_format(fp) == 'schwab_all'


def test_detect_schwab_single(fixtures_dir):
    fp = os.path.join(fixtures_dir, 'schwab_single_account.csv')
    assert detect_format(fp) == 'schwab_single'


def test_parse_all_accounts(fixtures_dir):
    fp = os.path.join(fixtures_dir, 'schwab_all_accounts.csv')
    accounts = parse_all_accounts(fp)

    assert set(accounts.keys()) == {'1111', '2222'}

    a = accounts['1111']
    # 'Cash and Money Market' is a NON_EQUITY_SYMBOLS section total, dropped
    # entirely (not aliased to 'cash' the way 'Cash & Cash Investments' is).
    assert list(a.index) == ['AAA', 'BBB']
    assert set(a.columns) == {'Quantity', 'Market_Value', 'Current_Price'}
    assert a.loc['AAA', 'Quantity'] == 10
    assert a.loc['AAA', 'Market_Value'] == 1100.00
    assert a.loc['AAA', 'Current_Price'] == 110.0

    b = accounts['2222']
    assert list(b.index) == ['CCC', 'cash']
    assert b.loc['cash', 'Market_Value'] == 700.00


def test_parse_single_account(fixtures_dir):
    fp = os.path.join(fixtures_dir, 'schwab_single_account.csv')
    acct, df = parse_single_account(fp)

    assert acct == '3333'
    assert list(df.index) == ['DDD', 'EEE', 'cash']
    assert df.loc['DDD', 'Quantity'] == 4
    assert df.loc['DDD', 'Market_Value'] == 4400.00
    assert df.loc['DDD', 'Current_Price'] == 1100.0
    assert df.loc['cash', 'Market_Value'] == 300.00

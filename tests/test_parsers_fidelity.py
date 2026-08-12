import os

from parsers import detect_format
from parsers.fidelity import parse_holdings_xlsx, parse_positions_csv


def test_detect_fidelity_csv(fixtures_dir):
    fp = os.path.join(fixtures_dir, 'fidelity_positions.csv')
    assert detect_format(fp) == 'fidelity_csv'


def test_detect_fidelity_xlsx(fixtures_dir):
    fp = os.path.join(fixtures_dir, 'fidelity_holdings.xlsx')
    assert detect_format(fp) == 'fidelity_xlsx'


def test_parse_positions_csv(fixtures_dir):
    fp = os.path.join(fixtures_dir, 'fidelity_positions.csv')
    accounts = parse_positions_csv(fp)

    assert set(accounts.keys()) == {'5555'}  # last 4 of 444455555
    df = accounts['5555']

    assert set(df.index) == {'cash', 'FFF', 'GGG'}
    assert set(df.columns) == {'Quantity', 'Market_Value', 'Current_Price'}
    # SPAXX** cash row is aliased to 'cash'
    assert df.loc['cash', 'Market_Value'] == 500.00
    assert df.loc['FFF', 'Quantity'] == 10
    assert df.loc['FFF', 'Current_Price'] == 50.00
    assert df.loc['GGG', 'Market_Value'] == 500.00


def test_parse_holdings_xlsx(fixtures_dir):
    fp = os.path.join(fixtures_dir, 'fidelity_holdings.xlsx')
    acct, df = parse_holdings_xlsx(fp)

    assert acct == '7888'  # last 4 of XRA577888
    assert list(df.index) == ['NNN', 'OOO']
    assert df.loc['NNN', 'Quantity'] == 10
    assert df.loc['NNN', 'Current_Price'] == 25.0
    assert df.loc['NNN', 'Market_Value'] == 250.0
    assert df.loc['OOO', 'Market_Value'] == 250.0

import os

from parsers import detect_format
from parsers.etrade import parse_account_summary


def test_detect_etrade(fixtures_dir):
    fp = os.path.join(fixtures_dir, 'etrade_account_summary.csv')
    assert detect_format(fp) == 'etrade'


def test_parse_account_summary(fixtures_dir, tmp_path):
    fp = os.path.join(fixtures_dir, 'etrade_account_summary.csv')
    acct, df = parse_account_summary(fp, data_dir=str(tmp_path))

    assert acct == '6666'
    assert set(df.columns) == {'Quantity', 'Current_Price', 'Market_Value'}
    assert 'HHH' in df.index
    assert 'III' in df.index
    assert df.loc['HHH', 'Quantity'] == 10
    assert df.loc['HHH', 'Current_Price'] == 100.00
    assert df.loc['HHH', 'Market_Value'] == 1000.00
    # TOTAL row is filtered out, never treated as a position
    assert 'TOTAL' not in df.index

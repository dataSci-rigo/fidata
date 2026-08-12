import os

from parsers.transactions import (
    parse_history_csv,
    parse_history_xlsx,
    parse_realized_gain_csv,
    _parse_action,
)


def test_parse_action_splits_reinvest_from_buy():
    assert _parse_action('YOU BOUGHT TEST') == 'BUY'
    assert _parse_action('REINVESTMENT TEST') == 'REINVEST'
    assert _parse_action('YOU SOLD TEST') == 'SELL'
    assert _parse_action('something else') is None


def test_parse_history_csv(fixtures_dir):
    fp = os.path.join(fixtures_dir, 'fidelity_history.csv')
    rows = parse_history_csv(fp)

    actions = {r['Symbol']: r['Action'] for r in rows}
    assert actions['JJJ'] in ('BUY', 'SELL')  # two JJJ rows, just sanity-check presence
    symbols_actions = [(r['Symbol'], r['Action']) for r in rows]
    assert ('JJJ', 'BUY') in symbols_actions
    assert ('JJJ', 'SELL') in symbols_actions
    assert ('KKK', 'REINVEST') in symbols_actions  # not folded into BUY

    jjj_buy = next(r for r in rows if r['Symbol'] == 'JJJ' and r['Action'] == 'BUY')
    assert jjj_buy['Quantity'] == 10
    assert jjj_buy['Price'] == 50.00
    assert jjj_buy['Account'] == '0000'  # last 4 of 999900000


def test_parse_history_xlsx(fixtures_dir):
    fp = os.path.join(fixtures_dir, 'etrade_history.xlsx')
    rows = parse_history_xlsx(fp)

    assert len(rows) == 2
    buy = next(r for r in rows if r['Action'] == 'BUY')
    assert buy['Symbol'] == 'PPP'
    assert buy['Quantity'] == 10
    assert buy['Price'] == 30.0
    assert buy['Account'] == '6999'  # last 4 of XRA666999

    sell = next(r for r in rows if r['Action'] == 'SELL')
    assert sell['Quantity'] == 4
    assert sell['Price'] == 35.0


def test_parse_realized_gain_csv(fixtures_dir):
    fp = os.path.join(fixtures_dir, 'schwab_realized_gain.csv')
    rows = parse_realized_gain_csv(fp)

    assert len(rows) == 2
    lll = next(r for r in rows if r['Symbol'] == 'LLL')
    assert lll['Quantity'] == 10
    assert lll['Cost_Per_Share'] == 50.00
    assert lll['Proceeds_Per_Share'] == 60.00
    assert lll['Gain_Loss'] == 100.00

    mmm = next(r for r in rows if r['Symbol'] == 'MMM')
    assert mmm['Gain_Loss'] == -25.00

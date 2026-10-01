"""weekly_input.build — the facts bundle both review paths read from.
Pure: synthetic artifacts in tmp dirs, no network, no module path constants."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import weekly_input  # noqa: E402


def _write(dirpath, name, payload):
    os.makedirs(dirpath, exist_ok=True)
    with open(os.path.join(dirpath, name), 'w') as f:
        json.dump(payload, f)


@pytest.fixture
def dirs(tmp_path):
    app, data = str(tmp_path / 'app_data'), str(tmp_path / 'data')
    _write(app, 'combined.json', [
        {'Symbol': 'NVDA', 'Market_Value': 60000, 'Current_Price': 120,
         'Avg_Buy_Price': 60, 'Gain_3m': 25.0, 'Gain_1yr': 80.0,
         'Ann_Vol': 0.5, 'Beta': 1.8, 'Sector': 'Information Technology',
         'Cost_Basis_Source': 'transactions'},
        {'Symbol': 'KO', 'Market_Value': 30000, 'Current_Price': 50,
         'Avg_Buy_Price': 62.5, 'Gain_3m': -8.0, 'Gain_1yr': 4.0,
         'Ann_Vol': 0.15, 'Beta': 0.5, 'Sector': 'Consumer Staples',
         'Cost_Basis_Source': 'transactions'},
        {'Symbol': 'OLD', 'Market_Value': 10000, 'Current_Price': 5,
         'Avg_Buy_Price': 20, 'Gain_3m': -3.0, 'Ann_Vol': 0.3,
         'Cost_Basis_Source': 'default_cutoff'},
        {'Symbol': 'cash', 'Market_Value': 5000},
    ])
    _write(app, 'sectors.json', {'by_gics': [
        {'Sector': 'Information Technology', 'Total_Market_Value': '$60,000'}]})
    _write(app, 'earnings.json', [
        {'Symbol': 'NVDA', 'Next_Earnings': '2026-10-01T00:00:00', 'EPS_Est': 1.234},
        {'Symbol': 'NONE', 'Next_Earnings': None}])
    _write(data, 'mpt_summary.json', {
        'current': {'return': 0.336, 'vol': 0.157, 'sharpe': 1.9},
        'port_beta': 1.19, 'hhi': 444.4, 'effective_n': 22.53,
        'rf_annual': 0.0373, 'n_symbols': 100,
        'high_correlation_pairs': [{'symbol_a': 'A', 'symbol_b': 'B',
                                    'correlation': 0.9}]})
    _write(data, 'portfolio_extras.json', {
        'capital': {'totals': {'roic_pct': 12.3456, 'total_pl': 1234.5},
                    'annual_activity': [{'Year': 2026, 'Bought': 1000}]},
        'cash_flows': {'totals': {'dividends': 579.49, 'fees': -104.0}},
        'closed_positions': [{'Symbol': 'X', 'Total_GL': 100}]})
    _write(data, 'news_feed.json', {
        'position_stories': [{'id': 'p1', 'symbol': 'NVDA', 'title': 'chip news',
                              'publisher': 'Reuters', 'published_at': '2026-09-23',
                              'summary': 'dropped', 'first_seen': '2026-09-23'}],
        'market_stories': [{'id': 'm1', 'symbol': 'SPY', 'title': 'fed news',
                            'publisher': 'AP', 'published_at': '2026-09-23',
                            'why': 'rates'}]})
    return app, data, str(tmp_path / 'no_accounts'), str(tmp_path / 'no_history.csv')


def test_bundle_shape_and_numbers(dirs):
    app, data, accts, hist = dirs
    out = weekly_input.build(app, data, accts, hist)

    assert out['generated_at']
    assert out['mpt'] == {'return_pct': 33.6, 'vol_pct': 15.7, 'sharpe': 1.9,
                          'beta': 1.19, 'hhi': 444.0, 'effective_n': 22.5,
                          'rf_pct': 3.73, 'n_symbols': 100,
                          'high_correlation_pairs': [{'symbol_a': 'A',
                                                      'symbol_b': 'B',
                                                      'correlation': 0.9}]}
    # cash excluded from market value but reported separately
    assert out['totals']['market_value'] == 100000
    assert out['totals']['cash'] == 5000
    assert out['totals']['positions'] == 3
    assert out['totals']['roic_pct'] == 12.35
    assert out['cash_flows'] == {'dividends': 579.0, 'fees': -104.0}

    nvda = out['holdings'][0]
    assert nvda['symbol'] == 'NVDA'            # sorted by market value
    assert nvda['weight_pct'] == 60.0
    assert nvda['return_pct'] == 100.0         # 120/60 - 1
    assert nvda['ann_vol_pct'] == 50.0
    assert '3 positions total' in out['holdings_note']

    assert [h['symbol'] for h in out['movers_3m']['best']][0] == 'NVDA'
    assert out['movers_3m']['worst'][0]['symbol'] == 'KO'   # worst first

    # default_cutoff rows are excluded: their cost basis isn't trustworthy
    assert [h['symbol'] for h in out['unrealized_losses']] == ['KO']

    assert out['news']['positions'][0]['title'] == 'chip news'
    assert 'summary' not in out['news']['positions'][0]     # trimmed for size
    assert out['news']['market'][0]['why'] == 'rates'
    assert out['upcoming_earnings'] == [
        {'symbol': 'NVDA', 'date': '2026-10-01', 'eps_est': 1.23}]
    assert out['closed_positions'] == [{'Symbol': 'X', 'Total_GL': 100}]
    assert out['annual_activity'] == [{'Year': 2026, 'Bought': 1000}]


def test_every_artifact_missing_still_builds(tmp_path):
    # every filesystem root overridden, accounts_dir included — otherwise the
    # tax sections read the real exports
    out = weekly_input.build(str(tmp_path / 'nope'), str(tmp_path / 'nope2'),
                             str(tmp_path / 'nope3'),
                             str(tmp_path / 'no_history.csv'))
    assert set(out) == {'generated_at'}        # sections omitted, not null


def test_partial_artifacts(tmp_path):
    app, data = str(tmp_path / 'a'), str(tmp_path / 'd')
    _write(app, 'combined.json', [{'Symbol': 'AAA', 'Market_Value': 100}])
    out = weekly_input.build(app, data, str(tmp_path / "no_accounts"),
                             str(tmp_path / "no_history.csv"))
    assert out['totals']['market_value'] == 100
    assert out['holdings'][0]['return_pct'] is None    # no prices -> None
    assert 'mpt' not in out and 'news' not in out


def test_nan_and_bad_values_become_none(tmp_path):
    app, data = str(tmp_path / 'a'), str(tmp_path / 'd')
    _write(app, 'combined.json', [
        {'Symbol': 'AAA', 'Market_Value': 100, 'Beta': 'n/a',
         'Ann_Vol': None, 'Current_Price': 10, 'Avg_Buy_Price': 0}])
    out = weekly_input.build(app, data, str(tmp_path / "no_accounts"),
                             str(tmp_path / "no_history.csv"))
    h = out['holdings'][0]
    assert h['beta'] is None and h['return_pct'] is None
    assert h['ann_vol_pct'] == 0.0


def test_bundle_is_json_serializable_and_compact(dirs):
    app, data, accts, hist = dirs
    text = json.dumps(weekly_input.build(app, data, accts, hist), default=str)
    assert len(text) < 200_000            # must fit comfortably in a prompt
    json.loads(text)


# ── SessionStart nudge helper ────────────────────────────────────────────────

def test_nudge_messages(tmp_path):
    import json as _json
    from datetime import date, timedelta

    import weekly_review_nudge as nudge

    today = date(2026, 9, 24)
    marker = tmp_path / 'artifact_published.json'

    # never published -> nudge (so first-time setup doesn't stay silent)
    assert 'never been published' in nudge.message(str(marker), today=today)

    # published today -> silent
    marker.write_text(_json.dumps({'published_at': str(today)}))
    assert nudge.message(str(marker), today=today) is None

    # 6 days -> still silent, 7 -> nudge (boundary)
    marker.write_text(_json.dumps(
        {'published_at': str(today - timedelta(days=6))}))
    assert nudge.message(str(marker), today=today) is None
    marker.write_text(_json.dumps(
        {'published_at': str(today - timedelta(days=7))}))
    assert '7 days' in nudge.message(str(marker), today=today)

    # corrupt / malformed date must not raise in a session-start hook
    marker.write_text('{oops')
    assert nudge.message(str(marker), today=today)
    marker.write_text(_json.dumps({'published_at': 'not-a-date'}))
    assert nudge.message(str(marker), today=today)

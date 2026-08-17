"""Golden-schema tests for analytics.export_app_data.

Every silent-data bug this project has hit was the same shape: a column that
stopped being written to app_data/*.json and nothing noticed for weeks
(Sector/Cap_Tier/Vol_Tier computed on a copy and never merged back;
Beta/Alpha_pct computed inside run_pipeline and dropped before export). These
tests pin the output schema so the next one fails loudly instead.

Two layers:
  * the synthetic tests always run, and catch export_app_data itself dropping
    or renaming a column;
  * test_real_combined_json_has_full_schema reads the *actual* pipeline output
    if it exists, which is what catches the pipeline forgetting to compute a
    column in the first place. It skips on a fresh clone.
"""
import json
import os

import pandas as pd
import pytest

from analytics import export_app_data

# The full combined.json column set. Adding a column to the pipeline means
# adding it here on purpose — that is the point.
EXPECTED_COMBINED_COLUMNS = {
    'Symbol', 'Quantity', 'Current_Price', 'Market_Value',
    'First_Buy_Date', 'Avg_Buy_Price', 'Days_Held', 'Cost_Basis_Source',
    'Trailing_PE', 'Forward_PE',
    'Target_Mean', 'Target_Median', 'Target_High', 'Target_Low', 'Num_Analysts',
    'Ann_Vol', 'Sharpe_1yr', 'Gain_1yr', 'Sharpe_6m', 'Gain_6m',
    'Sharpe_3m', 'Gain_3m',
    'Quote_Type', 'Sector', 'MarketCap', 'Cap_Tier', 'Vol_Tier',
    'Strong_Buy', 'Buy', 'Hold', 'Sell', 'Strong_Sell', 'Consensus',
    'Beta', 'Alpha_pct', 'Split_Factor',
}

# Written on every run. upgrades.json is deliberately absent: nothing writes
# upgrades.csv, so that file has never existed (see enrich.earnings_and_
# recommendations — the upgrades fetch was removed as dead weight).
EXPECTED_FILES = {
    'accounts.json', 'combined.json', 'sectors.json', 'flags.json',
    'targets.json', 'recommendations.json',
}


def _synthetic_combined() -> pd.DataFrame:
    """Two equities + a cash row, carrying every expected column."""
    rows = [
        {'Symbol': 'AAA', 'Quantity': 10.0, 'Current_Price': 100.0, 'Market_Value': 1000.0,
         'First_Buy_Date': pd.Timestamp('2023-06-01'), 'Avg_Buy_Price': 80.0, 'Days_Held': 400,
         'Cost_Basis_Source': 'transaction_history', 'Trailing_PE': 20.0, 'Forward_PE': 18.0,
         'Target_Mean': 120.0, 'Target_Median': 118.0, 'Target_High': 140.0, 'Target_Low': 90.0,
         'Num_Analysts': 12.0, 'Ann_Vol': 0.25, 'Sharpe_1yr': 1.2, 'Gain_1yr': 25.0,
         'Sharpe_6m': 1.1, 'Gain_6m': 12.0, 'Sharpe_3m': 0.9, 'Gain_3m': 6.0,
         'Quote_Type': 'EQUITY', 'Sector': 'Information Technology', 'MarketCap': 5.0e11,
         'Cap_Tier': 'Large Cap', 'Vol_Tier': 'Mid Vol', 'Strong_Buy': 5.0, 'Buy': 4.0,
         'Hold': 2.0, 'Sell': 0.0, 'Strong_Sell': 0.0, 'Consensus': 'Strong Buy',
         'Beta': 1.1, 'Alpha_pct': 3.4, 'Split_Factor': 1.0},
        {'Symbol': 'BBB', 'Quantity': 5.0, 'Current_Price': 50.0, 'Market_Value': 250.0,
         'First_Buy_Date': pd.Timestamp('2024-01-02'), 'Avg_Buy_Price': 60.0, 'Days_Held': 200,
         'Cost_Basis_Source': 'brokerage_fallback', 'Trailing_PE': 35.0, 'Forward_PE': 30.0,
         'Target_Mean': 45.0, 'Target_Median': 44.0, 'Target_High': 55.0, 'Target_Low': 35.0,
         'Num_Analysts': 8.0, 'Ann_Vol': 0.42, 'Sharpe_1yr': -0.3, 'Gain_1yr': -8.0,
         'Sharpe_6m': -0.2, 'Gain_6m': -4.0, 'Sharpe_3m': -0.1, 'Gain_3m': -2.0,
         'Quote_Type': 'EQUITY', 'Sector': 'Health Care', 'MarketCap': 3.0e9,
         'Cap_Tier': 'Mid Cap', 'Vol_Tier': 'High Vol', 'Strong_Buy': 1.0, 'Buy': 1.0,
         'Hold': 6.0, 'Sell': 1.0, 'Strong_Sell': 0.0, 'Consensus': 'Hold',
         'Beta': 1.6, 'Alpha_pct': -2.0, 'Split_Factor': 20.0},
        {'Symbol': 'cash', 'Quantity': None, 'Current_Price': 1.0, 'Market_Value': 500.0,
         'First_Buy_Date': pd.Timestamp('2023-01-02'), 'Avg_Buy_Price': None, 'Days_Held': 500,
         'Cost_Basis_Source': 'n/a', 'Trailing_PE': None, 'Forward_PE': None,
         'Target_Mean': None, 'Target_Median': None, 'Target_High': None, 'Target_Low': None,
         'Num_Analysts': None, 'Ann_Vol': None, 'Sharpe_1yr': None, 'Gain_1yr': None,
         'Sharpe_6m': None, 'Gain_6m': None, 'Sharpe_3m': None, 'Gain_3m': None,
         'Quote_Type': None, 'Sector': None, 'MarketCap': None, 'Cap_Tier': None,
         'Vol_Tier': None, 'Strong_Buy': None, 'Buy': None, 'Hold': None, 'Sell': None,
         'Strong_Sell': None, 'Consensus': None, 'Beta': None, 'Alpha_pct': None,
         'Split_Factor': 1.0},
    ]
    return pd.DataFrame(rows).set_index('Symbol')


@pytest.fixture
def exported(tmp_path):
    combined = _synthetic_combined()
    accounts = {'1111': combined.loc[['AAA'], ['Quantity', 'Market_Value', 'Current_Price']]}
    out = tmp_path / 'app_data'
    export_app_data(str(out), accounts, combined)
    return out


def test_all_expected_files_written(exported):
    written = {p.name for p in exported.iterdir()}
    assert EXPECTED_FILES <= written, f'missing: {EXPECTED_FILES - written}'


def test_combined_json_keeps_every_column(exported):
    rows = json.loads((exported / 'combined.json').read_text())
    assert len(rows) == 3
    for row in rows:
        assert set(row.keys()) == EXPECTED_COMBINED_COLUMNS


def test_sector_breakdowns_are_populated(exported):
    """by_cap/by_vol were silently empty for months — pin all three."""
    sectors = json.loads((exported / 'sectors.json').read_text())
    assert set(sectors) == {'by_gics', 'by_cap', 'by_vol'}
    for key, rows in sectors.items():
        assert rows, f'{key} is empty'


def test_cash_row_excluded_from_breakdowns(exported):
    sectors = json.loads((exported / 'sectors.json').read_text())
    for rows in sectors.values():
        assert all(r.get('Sector') != 'cash' for r in rows)
    flags = json.loads((exported / 'flags.json').read_text())
    for rows in flags.values():
        assert all(r['Symbol'] != 'cash' for r in rows)


def test_recommendations_written_with_consensus(exported):
    recs = json.loads((exported / 'recommendations.json').read_text())
    assert {r['Symbol'] for r in recs} == {'AAA', 'BBB'}
    assert {r['Consensus'] for r in recs} == {'Strong Buy', 'Hold'}


def test_real_combined_json_has_full_schema():
    """The guard that actually retires the bug class.

    Runs against the live pipeline output, so it fails if run_pipeline ever
    again computes a column and forgets to merge it into `combined` before
    export — which is how Sector/Cap_Tier/Vol_Tier and then Beta/Alpha_pct
    each went missing without a single error.
    """
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(repo, 'app_data', 'combined.json')
    if not os.path.exists(path):
        pytest.skip('no pipeline output yet — run run_pipeline.py')

    with open(path) as f:
        rows = json.load(f)
    assert rows, 'combined.json is empty'

    missing = EXPECTED_COMBINED_COLUMNS - set(rows[0])
    assert not missing, f'combined.json lost columns: {sorted(missing)}'

    equities = [r for r in rows if r['Symbol'] != 'cash']
    for col in ('Beta', 'Alpha_pct', 'Sector', 'Cap_Tier', 'Vol_Tier'):
        blank = [r['Symbol'] for r in equities if r.get(col) in (None, '')]
        assert not blank, f'{col} is null for {len(blank)} equities, e.g. {blank[:5]}'

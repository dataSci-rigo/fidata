import json
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alerts import detect_breakouts


@pytest.fixture
def hist_screener(fixtures_dir):
    df = pd.read_csv(os.path.join(fixtures_dir, 'hist_screener.csv'),
                     index_col=0, parse_dates=True)
    df.index.name = 'Date'
    return df


SYMBOLS = ['BRKUP', 'NEARHI', 'DOWNTR', 'SPARSE']


def test_fires_both_tiers(hist_screener, tmp_path):
    state = str(tmp_path / 'alerted.json')
    msgs = detect_breakouts(hist_screener, SYMBOLS, state)
    assert len(msgs) == 2
    assert any(m.startswith('🚀 BRKUP') for m in msgs)     # true breakout
    assert any(m.startswith('📈 NEARHI') for m in msgs)    # near-high band
    today = str(pd.Timestamp.now().normalize().date())
    with open(state) as f:
        alerted = json.load(f)
    # breakout stamps both tiers so BRKUP won't re-ping as "near-high"
    assert alerted['BRKUP'] == {'breakout': today, 'near': today}
    assert alerted['NEARHI'] == {'near': today}


def test_dedup_second_call(hist_screener, tmp_path):
    state = str(tmp_path / 'alerted.json')
    detect_breakouts(hist_screener, SYMBOLS, state)
    assert detect_breakouts(hist_screener, SYMBOLS, state) == []


def test_cooldown_expiry(hist_screener, tmp_path):
    state = tmp_path / 'alerted.json'
    old = str((pd.Timestamp.now() - pd.Timedelta(days=40)).date())
    state.write_text(json.dumps({'BRKUP': {'breakout': old, 'near': old},
                                 'NEARHI': {'near': old}}))
    msgs = detect_breakouts(hist_screener, SYMBOLS, str(state))
    assert len(msgs) == 2


def test_state_untouched_when_nothing_fires(hist_screener, tmp_path):
    state = tmp_path / 'alerted.json'
    # Only symbols that never pass the scan filters
    msgs = detect_breakouts(hist_screener, ['DOWNTR', 'SPARSE'], str(state))
    assert msgs == []
    assert not state.exists()

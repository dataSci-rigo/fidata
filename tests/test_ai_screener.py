"""ai_screener: context assembly from tmp artifacts + tool-use plumbing with
a fake client — no network, no real API key needed."""
import json
import os
import sys
from types import SimpleNamespace

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ai_screener  # noqa: E402


def test_build_context_with_artifacts(tmp_path):
    app_data = tmp_path / 'app_data'
    data = tmp_path / 'data'
    app_data.mkdir(), data.mkdir()
    (data / 'mpt_summary.json').write_text(json.dumps({
        'current': {'return': 0.21, 'vol': 0.18, 'sharpe': 0.95},
        'port_beta': 1.12, 'effective_n': 14.2, 'hhi': 704, 'rf_annual': 0.041}))
    (app_data / 'combined.json').write_text(json.dumps([
        {'Symbol': 'NVDA', 'Market_Value': 60000},
        {'Symbol': 'SPY', 'Market_Value': 40000},
        {'Symbol': 'cash', 'Market_Value': 5000}]))
    (app_data / 'sectors.json').write_text(json.dumps({
        'by_gics': [{'Sector': 'Information Technology',
                     'Total_Market_Value': '$60,000'}]}))
    uni = pd.DataFrame({'Sector': ['Information Technology', 'Fixed Income'],
                        'Asset_Class': ['Equity', 'Bond'],
                        'Ann_Vol': [0.5, 0.15], 'MarketCap': [3e12, None],
                        'Dividend_Yield': [0.0003, 0.037],
                        'Corr_Portfolio': [0.8, -0.3]})
    ctx = ai_screener.build_context(str(app_data), str(data), uni)
    assert 'vol 18.0%' in ctx
    assert 'NVDA 60%' in ctx                    # 60000/100000 (cash excluded)
    assert 'Fixed Income' in ctx and 'Bond (1)' in ctx
    assert 'corr_portfolio range' in ctx


def test_build_context_all_missing(tmp_path):
    ctx = ai_screener.build_context(str(tmp_path / 'a'), str(tmp_path / 'b'), None)
    assert 'unknown' in ctx and ctx.startswith('PORTFOLIO CONTEXT:')


def test_query_to_filters_happy_path(monkeypatch):
    captured = {}

    class FakeMessages:
        def create(self, **kw):
            captured.update(kw)
            return SimpleNamespace(content=[
                SimpleNamespace(type='text', text='thinking...'),
                SimpleNamespace(type='tool_use', input={
                    'rationale': 'Portfolio vol is 18%, so cap at 15%.',
                    'ann_vol_max': 0.15, 'corr_portfolio_max': 0.3,
                    'made_up_key': 'x'}),
            ])

    class FakeClient:
        def __init__(self):
            self.messages = FakeMessages()

    monkeypatch.setattr(ai_screener, 'anthropic',
                        SimpleNamespace(Anthropic=FakeClient), raising=False)
    monkeypatch.setitem(sys.modules, 'anthropic',
                        SimpleNamespace(Anthropic=FakeClient))
    out = ai_screener.query_to_filters('reduce my vol', 'CTX')
    assert out['params'] == {'ann_vol_max': 0.15, 'corr_portfolio_max': 0.3}
    assert out['rationale'].startswith('Portfolio vol')
    assert any('made_up_key' in w for w in out['warnings'])
    assert captured['tool_choice'] == {'type': 'tool',
                                       'name': 'set_screener_filters'}
    assert 'USER QUERY: reduce my vol' in captured['messages'][0]['content']


def test_query_to_filters_client_failure(monkeypatch):
    class Boom:
        def __init__(self):
            raise RuntimeError('no key')

    monkeypatch.setitem(sys.modules, 'anthropic', SimpleNamespace(Anthropic=Boom))
    with pytest.raises(ai_screener.AiScreenerError):
        ai_screener.query_to_filters('q', 'ctx')


def test_query_to_filters_no_tool_block(monkeypatch):
    class FakeClient:
        def __init__(self):
            self.messages = SimpleNamespace(
                create=lambda **kw: SimpleNamespace(content=[]))

    monkeypatch.setitem(sys.modules, 'anthropic',
                        SimpleNamespace(Anthropic=FakeClient))
    with pytest.raises(ai_screener.AiScreenerError):
        ai_screener.query_to_filters('q', 'ctx')

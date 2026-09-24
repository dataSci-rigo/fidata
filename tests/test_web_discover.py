"""/discover routes: rendering, filtering, watchlist writes, AI endpoint, and
refresh launch — all against tmp dirs, with the network-facing pieces
monkeypatched. No real yfinance/anthropic calls anywhere."""
import json
import os
import sys
from types import SimpleNamespace

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

flask = pytest.importorskip('flask', reason='pip install -r requirements-local.txt')

import ai_screener  # noqa: E402
import local_server  # noqa: E402
import universe_data  # noqa: E402

HDRS = {'X-FiData': '1'}


@pytest.fixture(autouse=True)
def _local_enabled(monkeypatch):
    monkeypatch.setenv('FIDATA_LOCAL', '1')


def make_app(tmp_path, with_universe=True):
    app_data = tmp_path / 'app_data'
    data = tmp_path / 'data'
    app_data.mkdir(exist_ok=True), data.mkdir(exist_ok=True)
    (app_data / 'combined.json').write_text(json.dumps([
        {'Symbol': 'NVDA', 'Market_Value': 50000},
        {'Symbol': 'cash', 'Market_Value': 1000}]))
    (tmp_path / 'watchlist.txt').write_text('# header comment\nWCHD\n')
    if with_universe:
        udir = data / 'universe'
        udir.mkdir()
        pd.DataFrame([
            dict(Symbol='NVDA', Name='NVIDIA', Quote_Type='EQUITY',
                 Asset_Class='Equity', Sector='Information Technology',
                 Industry='Semis', Category='', Summary='AI chips',
                 In_SP500=True, In_NDX=True, MarketCap=3e12, Ann_Vol=0.5,
                 Sharpe_1yr=2.0, Gain_1yr=80.0, Gain_3m=20.0, Beta=1.8,
                 Corr_Portfolio=0.8, Dividend_Yield=0.0003,
                 Expense_Ratio=None, High_52w_Ratio=0.98, Uptrend=True),
            dict(Symbol='TLT', Name='iShares 20+ Treasury', Quote_Type='ETF',
                 Asset_Class='Bond', Sector='Fixed Income', Industry='',
                 Category='Long Government', Summary='treasuries',
                 In_SP500=False, In_NDX=False, MarketCap=None, Ann_Vol=0.15,
                 Sharpe_1yr=-0.2, Gain_1yr=-2.0, Gain_3m=1.0, Beta=-0.1,
                 Corr_Portfolio=-0.3, Dividend_Yield=0.037,
                 Expense_Ratio=0.0015, High_52w_Ratio=0.85, Uptrend=False),
            dict(Symbol='WCHD', Name='Watched Co', Quote_Type='EQUITY',
                 Asset_Class='Equity', Sector='Health Care', Industry='Bio',
                 Category='', Summary='', In_SP500=False, In_NDX=False,
                 MarketCap=5e9, Ann_Vol=0.4, Sharpe_1yr=0.5, Gain_1yr=5.0,
                 Gain_3m=2.0, Beta=1.2, Corr_Portfolio=0.3,
                 Dividend_Yield=0.0, Expense_Ratio=None,
                 High_52w_Ratio=0.8, Uptrend=False),
        ]).to_csv(udir / universe_data.UNIVERSE_FILE, index=False)
        (udir / universe_data.TREASURY_FILE).write_text(json.dumps(
            {'13-week': 4.31, '10-year': 4.05, 'fetched_at': 'x'}))
    app = local_server.create_app(app_data_dir=str(app_data),
                                  data_dir=str(data), root_dir=str(tmp_path))
    app.config.update(TESTING=True)
    return app


def test_discover_renders_rows_and_badges(tmp_path):
    c = make_app(tmp_path).test_client()
    body = c.get('/discover').data.decode()
    assert 'TLT' in body and 'watching' in body
    assert 'NVDA' not in body            # held → excluded by default
    assert '4.31' in body                # treasury card
    body = c.get('/discover?_form=1').data.decode()
    assert 'NVDA' in body and '>held<' in body.replace('\n', '')


def test_discover_query_filtering(tmp_path):
    c = make_app(tmp_path).test_client()
    body = c.get('/discover?_form=1&asset_classes=Bond').data.decode()
    assert 'TLT' in body and 'WCHD' not in body
    body = c.get('/discover?_form=1&text_any=AI+chips').data.decode()
    assert 'NVDA' in body and 'TLT' not in body


def test_discover_empty_state(tmp_path):
    c = make_app(tmp_path, with_universe=False).test_client()
    resp = c.get('/discover')
    assert resp.status_code == 200
    assert b'refresh_universe.py' in resp.data


def test_watchlist_post_paths(tmp_path):
    c = make_app(tmp_path).test_client()
    assert c.post('/discover/watchlist', json={'symbol': 'TLT'}).status_code == 403
    r = c.post('/discover/watchlist', json={'symbol': 'TLT'}, headers=HDRS)
    assert r.get_json()['status'] == 'added'
    r = c.post('/discover/watchlist', json={'symbol': 'TLT'}, headers=HDRS)
    assert r.get_json()['status'] == 'exists'
    r = c.post('/discover/watchlist', json={'symbol': 'NVDA'}, headers=HDRS)
    assert r.get_json()['status'] == 'held'
    assert c.post('/discover/watchlist', json={'symbol': 'no way'},
                  headers=HDRS).status_code == 400
    text = (tmp_path / 'watchlist.txt').read_text()
    assert text.startswith('# header comment\n') and 'TLT' in text


def test_ai_endpoint_success_and_failure(tmp_path, monkeypatch):
    c = make_app(tmp_path).test_client()
    monkeypatch.setattr(ai_screener, 'query_to_filters',
                        lambda q, ctx: {'params': {'ann_vol_max': 0.2},
                                        'rationale': 'r', 'warnings': []})
    r = c.post('/discover/ai', json={'query': 'reduce vol'}, headers=HDRS)
    assert r.status_code == 200 and r.get_json()['params'] == {'ann_vol_max': 0.2}
    assert c.post('/discover/ai', json={'query': ''}, headers=HDRS).status_code == 400
    assert c.post('/discover/ai', json={'query': 'x'}).status_code == 403

    def boom(q, ctx):
        raise ai_screener.AiScreenerError('no key')
    monkeypatch.setattr(ai_screener, 'query_to_filters', boom)
    r = c.post('/discover/ai', json={'query': 'x'}, headers=HDRS)
    assert r.status_code == 503 and 'no key' in r.get_json()['error']


def test_refresh_launch_and_conflict(tmp_path, monkeypatch):
    c = make_app(tmp_path).test_client()
    launched = {}

    class FakeProc:
        def __init__(self, cmd, **kw):
            launched['cmd'] = cmd
            launched['kw'] = kw
            self._alive = True

        def poll(self):
            return None if self._alive else 0

    monkeypatch.setattr(local_server.subprocess, 'Popen', FakeProc)
    r = c.post('/discover/refresh', json={'mode': 'full'}, headers=HDRS)
    assert r.status_code == 202
    assert '--full' in launched['cmd']
    assert any('refresh_universe.py' in p for p in launched['cmd'])
    assert launched['kw']['start_new_session'] is True
    # second launch while the fake proc is "running" → 409
    assert c.post('/discover/refresh', json={'mode': 'quick'},
                  headers=HDRS).status_code == 409
    st = c.get('/discover/refresh/status').get_json()
    assert st['running'] is True
    assert c.post('/discover/refresh', json={}).status_code == 403


def test_refresh_status_reads_file(tmp_path):
    app = make_app(tmp_path)
    udir = tmp_path / 'data' / 'universe'
    (udir / universe_data.STATUS_FILE).write_text(json.dumps(
        {'phase': 'done', 'done': 5, 'total': 5}))
    st = app.test_client().get('/discover/refresh/status').get_json()
    assert st['phase'] == 'done' and st['running'] is False

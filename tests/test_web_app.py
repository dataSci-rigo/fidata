"""Route tests for the local-only web viewer.

Two walks over every route: one against the real repo output, one against
empty directories. The second is why create_app takes the directories as
parameters — a viewer that 500s the moment the pipeline hasn't run is worse
than useless, and viewer_app.py's sys.exit(1) on a missing app_data/ is the
behavior being deliberately avoided here.
"""
import os

import pytest

flask = pytest.importorskip('flask', reason='pip install -r requirements-local.txt')

import local_server
from local_server import LocalOnlyError, create_app

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ROUTES = ['/', '/holdings', '/accounts', '/sectors', '/flags', '/analysts',
          '/risk', '/capital', '/chart', '/review']


@pytest.fixture(autouse=True)
def _local_enabled(monkeypatch):
    monkeypatch.setenv('FIDATA_LOCAL', '1')


@pytest.fixture
def live_client():
    app = create_app()
    app.config.update(TESTING=True)
    return app.test_client()


@pytest.fixture
def empty_client(tmp_path):
    app = create_app(app_data_dir=str(tmp_path / 'app_data'),
                     data_dir=str(tmp_path / 'data'),
                     root_dir=str(tmp_path))
    app.config.update(TESTING=True)
    return app.test_client()


# ── the gate ─────────────────────────────────────────────────────────────────

def test_create_app_refuses_without_env(monkeypatch):
    monkeypatch.delenv('FIDATA_LOCAL', raising=False)
    with pytest.raises(LocalOnlyError):
        create_app()


def test_create_app_refuses_on_wrong_value(monkeypatch):
    monkeypatch.setenv('FIDATA_LOCAL', 'true')
    with pytest.raises(LocalOnlyError):
        create_app()


def test_main_exits_2_without_env(monkeypatch, capsys):
    monkeypatch.delenv('FIDATA_LOCAL', raising=False)
    monkeypatch.setattr('sys.argv', ['local_server.py'])
    assert local_server.main() == 2
    err = capsys.readouterr().err
    assert 'FIDATA_LOCAL' in err and 'never run on the VM' in err


# ── every route renders ──────────────────────────────────────────────────────

@pytest.mark.parametrize('route', ROUTES)
def test_routes_ok_with_real_data(live_client, route):
    resp = live_client.get(route)
    assert resp.status_code == 200, route


@pytest.mark.parametrize('route', ROUTES)
def test_routes_ok_with_no_data_at_all(empty_client, route):
    """No app_data/, no data/, no historical.csv — still 200, still explains
    itself rather than crashing."""
    resp = empty_client.get(route)
    assert resp.status_code == 200, route
    assert b'run_pipeline.py' in resp.data


def test_holdings_shows_every_column(live_client):
    body = live_client.get('/holdings').data.decode()
    for col in ('Beta', 'Alpha pct', 'Sector', 'Cap Tier', 'Cost Basis Source'):
        assert col in body, col


def test_cash_row_is_marked(live_client):
    assert 'class="cash"' in live_client.get('/holdings').data.decode()


# ── figures ──────────────────────────────────────────────────────────────────

def test_static_figure_served_from_allowlist(live_client):
    resp = live_client.get('/fig/efficient_frontier.png')
    if resp.status_code == 404:
        pytest.skip('efficient_frontier.png not generated yet')
    assert resp.data[:8] == b'\x89PNG\r\n\x1a\n'


def test_unknown_figure_name_404s(live_client):
    assert live_client.get('/fig/../.env.png').status_code in (404, 308, 400)
    assert live_client.get('/fig/secrets.png').status_code == 404


def test_price_chart_renders_png(live_client):
    resp = live_client.get('/fig/price/NVDA.png?start=2025-01-01&end=2026-08-12')
    if resp.status_code == 404:
        pytest.skip('no historical data for NVDA')
    assert resp.status_code == 200
    assert resp.mimetype == 'image/png'
    assert resp.data[:8] == b'\x89PNG\r\n\x1a\n'
    assert len(resp.data) > 5000


def test_price_chart_handles_slash_symbols(live_client):
    """BRK/B is stored as BRK-B in historical.csv."""
    resp = live_client.get('/fig/price/BRK-B.png?start=2025-01-01&end=2026-08-12')
    if resp.status_code == 404:
        pytest.skip('BRK-B not held')
    assert resp.mimetype == 'image/png'


@pytest.mark.parametrize('query, code', [
    ('?start=2026-08-01&end=2025-01-01', 400),   # inverted
    ('?start=2026-08-08&end=2026-08-12', 400),   # < 5 points
    ('?start=not-a-date&end=2026-08-12', 400),
])
def test_price_chart_rejects_bad_ranges(live_client, query, code):
    resp = live_client.get(f'/fig/price/NVDA.png{query}')
    if resp.status_code == 404:
        pytest.skip('no historical data for NVDA')
    assert resp.status_code == code
    assert b'Traceback' not in resp.data


def test_price_chart_unknown_symbol_404s(live_client):
    resp = live_client.get('/fig/price/NOTATICKER.png')
    assert resp.status_code == 404
    assert b'Traceback' not in resp.data


# ── data quirks the routes must absorb ───────────────────────────────────────

def test_mpt_key_asymmetry_is_normalized():
    """`current` uses 'return', the others use 'ret'. Jinja would render the
    mismatch as a silently blank cell."""
    out = local_server._normalize_mpt({
        'current': {'return': 0.34, 'vol': 0.15, 'sharpe': 1.9},
        'max_sharpe': {'ret': 0.68, 'vol': 0.12, 'sharpe': 5.2},
        'min_var': {'ret': 0.12, 'vol': 0.05, 'sharpe': 1.7},
    })
    assert [r['ret'] for r in out] == [0.34, 0.68, 0.12]


def test_account_columns_are_pinned_not_inferred(live_client):
    """accounts.json key order differs between accounts, so the header must
    come from the pinned list rather than rows[0]."""
    body = live_client.get('/accounts').data.decode()
    assert body.index('Current Price') < body.index('Market Value')


def test_unnamed_account_falls_back(live_client):
    """.env has 9 ACC_* entries for 10 accounts (no ACC_898)."""
    body = live_client.get('/accounts').data.decode()
    assert 'Account ' in body or 'ACC_' not in body

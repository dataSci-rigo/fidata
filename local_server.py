#!/usr/bin/env python3
"""fiData/local_server.py — local-only web viewer.

A browser front-end over whatever the last run_pipeline.py left on disk. Its
GET surface never fetches, never triggers the pipeline, and never sends
Telegram — so opening it cannot perturb the scheduled jobs' state
(last_run_snapshot.csv, alerted_earnings.json) the way a "Refresh" button
would.

The /discover page adds three explicit-user-action POST endpoints (the only
writes in the app): /discover/watchlist appends to watchlist.txt,
/discover/refresh launches refresh_universe.py as a detached subprocess, and
/discover/ai calls the Anthropic API. They touch only data/universe/* and
watchlist.txt — never pipeline state — and require the X-FiData: 1 header
(set by this page's own fetch calls; a cross-site form can't send it).

LOCAL ONLY. Gated on FIDATA_LOCAL=1, which belongs in this machine's .env and
must never reach ~/code20/.env.master — env_sync.py push_env propagates master
keys to the VM, which would defeat the gate. Nothing in run_pipeline.py, the
review scripts, or systemd/ references this file, so on the VM it is inert.

    FIDATA_LOCAL=1 python local_server.py [--port 8787]
"""
import argparse
import glob
import io
import json
import os
import subprocess
import sys

from dotenv import dotenv_values

_DATA_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _DATA_DIR)

# Load .env before any local import, for the same reason run_pipeline.py does
# (see its comment): modules that read os.environ at import time must see it.
for _k, _v in dotenv_values(os.path.join(_DATA_DIR, '.env')).items():
    os.environ.setdefault(_k, _v)

# Agg before plotting is imported — headless render path, no display.
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

import pandas as pd
from flask import Flask, Response, abort, render_template, request, send_file

from app_data_io import (APP_DATA, DATA_STATE, cell, col_kind, data_status, fmt,
                         humanize, load)
from discover_filters import (DEFAULTS, RANGE_FIELDS, SORT_COLUMNS,
                              apply_filters, params_from_args, validate_params)
from market_data import append_watchlist, load_watchlist
from plotting import DEFAULT_RF, build_price_drawdown_figure
import ai_screener
import universe_data

HIST_FILE = os.path.join(_DATA_DIR, 'historical.csv')

# Explicit allowlist. The repo root holds .env, historical.csv and every broker
# export, so it must never be a Flask static folder.
FIGURES = {
    'efficient_frontier': os.path.join(_DATA_DIR, 'efficient_frontier.png'),
    'correlation_heatmap': os.path.join(_DATA_DIR, 'correlation_heatmap.png'),
}

STALE_HOURS = 24  # pipeline runs 07/13/19, so >24h is genuinely stale

# combined.json's cash row is ~30 nulls; it sorts and charts badly.
CASH = 'cash'

_hist_cache: dict = {'mtime': None, 'df': None}


class LocalOnlyError(RuntimeError):
    pass


# ── data helpers ──────────────────────────────────────────────────────────────

def _rows(name: str, app_data_dir: str) -> list:
    data = load(name, app_data_dir)
    return data if isinstance(data, list) else []


def _obj(name: str, app_data_dir: str) -> dict:
    data = load(name, app_data_dir)
    return data if isinstance(data, dict) else {}


def _state(name: str, data_dir: str):
    path = os.path.join(data_dir, name)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def _columns(rows: list, first: tuple = ()) -> list:
    """Column order for a table. accounts.json's key order is not stable across
    accounts, so callers pin the leading columns rather than trusting rows[0].
    """
    if not rows:
        return []
    seen = list(first)
    for r in rows:
        for k in r:
            if k not in seen:
                seen.append(k)
    return [c for c in seen if any(c in r for r in rows)]


def _account_names() -> dict:
    """ACC_<suffix> friendly names from .env. Only 9 of 10 accounts have one,
    so callers need the 'Account <id>' fallback."""
    return {k[4:]: v for k, v in os.environ.items() if k.startswith('ACC_') and v}


def _hist() -> pd.DataFrame:
    """historical.csv, reloaded only when the file changes."""
    if not os.path.exists(HIST_FILE):
        return pd.DataFrame()
    mtime = os.path.getmtime(HIST_FILE)
    if _hist_cache['mtime'] != mtime:
        df = pd.read_csv(HIST_FILE, index_col=0, parse_dates=True)
        if getattr(df.index, 'tz', None) is not None:
            df.index = df.index.tz_localize(None)
        _hist_cache.update(mtime=mtime, df=df)
    return _hist_cache['df']


def _normalize_mpt(summary: dict) -> list[dict]:
    """mpt_summary.json's `current` uses 'return' while max_sharpe/min_var use
    'ret'. Jinja renders an undefined key as an empty cell, so flatten here."""
    out = []
    for key, label in (('current', 'Current'), ('max_sharpe', 'Max Sharpe'),
                        ('min_var', 'Min Variance')):
        block = summary.get(key) or {}
        out.append({
            'label': label,
            'ret': block.get('ret', block.get('return')),
            'vol': block.get('vol'),
            'sharpe': block.get('sharpe'),
        })
    return out


# ── app factory ───────────────────────────────────────────────────────────────

def create_app(app_data_dir: str = APP_DATA, data_dir: str = DATA_STATE,
                root_dir: str = _DATA_DIR) -> Flask:
    """Build the Flask app.

    The directories are parameters, not module constants, so the tests can
    point the whole app at empty tmp dirs and prove every page still renders
    its empty state instead of 500ing.

    Raises LocalOnlyError unless FIDATA_LOCAL=1 — checked here as well as in
    __main__ so importing the module can't bypass the gate.
    """
    if os.environ.get('FIDATA_LOCAL') != '1':
        raise LocalOnlyError('FIDATA_LOCAL is not set to 1')

    app = Flask(__name__, static_folder=None)
    app.jinja_env.globals.update(fmt=fmt, cell=cell, col_kind=col_kind,
                                 humanize=humanize)

    def base_ctx(active: str) -> dict:
        status = data_status(app_data_dir, data_dir, root_dir)
        present = [s for s in status if s['exists']]
        oldest = max((s['age_hours'] for s in present), default=None)
        return {
            'active': active,
            'has_data': os.path.exists(os.path.join(app_data_dir, 'combined.json')),
            'status': status,
            'oldest_hours': oldest,
            'stale': oldest is not None and oldest > STALE_HOURS,
            'has_review': bool(glob.glob(os.path.join(data_dir, 'weekly_review_*.json'))),
        }

    def tables(active: str, title: str, specs: list, note: str = '') -> str:
        """Render the shared tabbed-table page. `specs` is
        [(tab_label, columns, rows)] — one template for six routes."""
        return render_template('page_tables.html', title=title, note=note,
                               specs=[s for s in specs], **base_ctx(active))

    # ── overview ─────────────────────────────────────────────────────────────
    @app.route('/')
    def overview():
        extras = _state('portfolio_extras.json', data_dir) or {}
        summary = _state('mpt_summary.json', data_dir) or {}
        alerts = _state('last_alerts.json', data_dir) or {}
        totals = (extras.get('capital') or {}).get('totals') or {}
        combined = _rows('combined.json', app_data_dir)
        return render_template(
            'overview.html', title='Overview', totals=totals, summary=summary,
            mpt=_normalize_mpt(summary),
            n_positions=len([r for r in combined if r.get('Symbol') != CASH]),
            alerts=alerts.get('messages') or [], alerts_run_at=alerts.get('run_at'),
            **base_ctx('overview'))

    # ── holdings ─────────────────────────────────────────────────────────────
    @app.route('/holdings')
    def holdings():
        rows = _rows('combined.json', app_data_dir)
        # cash last: its nulls poison every column sort
        rows = sorted(rows, key=lambda r: (r.get('Symbol') == CASH,))
        cols = _columns(rows, first=('Symbol', 'Quantity', 'Current_Price',
                                      'Market_Value', 'Gain_3m', 'Beta', 'Sector'))
        return render_template('holdings.html', title='Holdings', rows=rows,
                                cols=cols, cash=CASH, **base_ctx('holdings'))

    # ── accounts ─────────────────────────────────────────────────────────────
    @app.route('/accounts')
    def accounts():
        data = _obj('accounts.json', app_data_dir)
        names = _account_names()
        specs = []
        for acct in sorted(data, key=lambda a: -sum(
                float(r.get('Market_Value') or 0) for r in data[a])):
            rows = data[acct]
            total = sum(float(r.get('Market_Value') or 0) for r in rows)
            label = f"{names.get(acct, f'Account {acct}')} · {fmt(total, '$big')}"
            # pinned order: accounts.json key order is not stable across accounts
            specs.append((label, ['Symbol', 'Quantity', 'Current_Price',
                                   'Market_Value'], rows))
        return tables('accounts', 'Accounts', specs)

    # ── sectors ──────────────────────────────────────────────────────────────
    @app.route('/sectors')
    def sectors():
        data = _obj('sectors.json', app_data_dir)
        labels = {'by_gics': 'By GICS Sector', 'by_cap': 'By Market Cap',
                  'by_vol': 'By Volatility'}
        specs = [(labels.get(k, k), _columns(data[k]), data[k])
                 for k in ('by_gics', 'by_cap', 'by_vol') if data.get(k)]
        return tables('sectors', 'Sector Breakdown', specs)

    # ── flags ────────────────────────────────────────────────────────────────
    @app.route('/flags')
    def flags():
        data = _obj('flags.json', app_data_dir)
        labels = {
            'high_trailing_pe': 'Highest Trailing P/E',
            'high_forward_pe': 'Highest Forward P/E',
            'low_forward_pe': 'Lowest Forward P/E',
            'best_3m': 'Best 3-Month', 'worst_3m': 'Worst 3-Month',
        }
        specs = [(labels.get(k, k), _columns(data[k], first=('Symbol',)), data[k])
                 for k in labels if data.get(k)]
        return tables('flags', 'Flags', specs)

    # ── analysts ─────────────────────────────────────────────────────────────
    @app.route('/analysts')
    def analysts():
        tgt = _obj('targets.json', app_data_dir)
        specs = []
        for key, label in (('overvalued', 'Target Below Price'),
                            ('most_upside', 'Most Upside'),
                            ('tightest', 'Tightest Consensus')):
            if tgt.get(key):
                specs.append((label, _columns(tgt[key], first=('Symbol',)), tgt[key]))
        recs = _rows('recommendations.json', app_data_dir)
        if recs:
            recs = sorted(recs, key=lambda r: -(float(r.get('Strong_Buy') or 0)
                                                 + float(r.get('Buy') or 0)))
            specs.append(('Recommendations', _columns(recs, first=('Symbol',)), recs))
        earn = _rows('earnings.json', app_data_dir)
        if earn:
            specs.append(('Upcoming Earnings',
                          _columns(earn, first=('Symbol', 'Next_Earnings')), earn))
        return tables('analysts', 'Analysts & Earnings', specs)

    # ── risk ─────────────────────────────────────────────────────────────────
    @app.route('/risk')
    def risk():
        summary = _state('mpt_summary.json', data_dir) or {}
        extras = _state('portfolio_extras.json', data_dir) or {}
        figs = []
        for key, path in FIGURES.items():
            figs.append({'key': key, 'label': humanize(key).title(),
                         'exists': os.path.exists(path),
                         'mtime': os.path.getmtime(path) if os.path.exists(path) else None})
        return render_template(
            'risk.html', title='Risk & MPT', summary=summary,
            mpt=_normalize_mpt(summary), figs=figs,
            pairs=summary.get('high_correlation_pairs') or [],
            contributors=extras.get('risk_contributors') or [],
            weights=extras.get('max_sharpe_weights') or [],
            **base_ctx('risk'))

    # ── capital ──────────────────────────────────────────────────────────────
    @app.route('/capital')
    def capital():
        extras = _state('portfolio_extras.json', data_dir) or {}
        cap = extras.get('capital') or {}
        specs = []
        if cap.get('annual_activity'):
            specs.append(('Annual Activity', _columns(cap['annual_activity'],
                                                       first=('Year',)),
                          cap['annual_activity']))
        if cap.get('holdings_cost_basis'):
            specs.append(('Holdings Cost Basis',
                          _columns(cap['holdings_cost_basis'], first=('Symbol',)),
                          cap['holdings_cost_basis']))
        if extras.get('closed_positions'):
            specs.append((f"Closed Positions ({len(extras['closed_positions'])})",
                          _columns(extras['closed_positions'], first=('Symbol',)),
                          extras['closed_positions']))
        if cap.get('dividends_reinvested'):
            specs.append(('Dividends Reinvested',
                          _columns(cap['dividends_reinvested']),
                          cap['dividends_reinvested']))
        return tables('capital', 'Capital & Closed Positions', specs)

    # ── weekly review ────────────────────────────────────────────────────────
    @app.route('/review')
    @app.route('/review/<date>')
    def review(date=None):
        paths = sorted(glob.glob(os.path.join(data_dir, 'weekly_review_*.json')),
                       reverse=True)
        dates = [os.path.basename(p)[len('weekly_review_'):-len('.json')] for p in paths]
        chosen = date or (dates[0] if dates else None)
        sections = {}
        if chosen and chosen in dates:
            with open(os.path.join(data_dir, f'weekly_review_{chosen}.json')) as f:
                sections = json.load(f)
        elif chosen:
            abort(404)
        return render_template('review.html', title='Weekly Review', dates=dates,
                                chosen=chosen, sections=sections, **base_ctx('review'))

    # ── news ─────────────────────────────────────────────────────────────────
    @app.route('/news')
    def news_page():
        feed = _state('news_feed.json', data_dir) or {}
        return render_template('news.html', title='News', feed=feed,
                                **base_ctx('news'))

    # ── charts ───────────────────────────────────────────────────────────────
    @app.route('/chart')
    def chart():
        hist = _hist()
        rows = _rows('combined.json', app_data_dir)
        owned = _chart_symbols(hist, rows)
        symbol = request.args.get('symbol') or (owned[0] if owned else '')
        end = request.args.get('end') or (str(hist.index.max().date())
                                          if not hist.empty else '')
        start = request.args.get('start')
        if not start and end:
            start = str(pd.Timestamp(end).date() - pd.Timedelta(days=365))
        return render_template('chart.html', title='Chart', symbols=owned,
                                symbol=symbol, start=start or '', end=end or '',
                                **base_ctx('chart'))

    @app.route('/fig/<name>.png')
    def figure(name):
        path = FIGURES.get(name)  # allowlist, never a path built from `name`
        if not path or not os.path.exists(path):
            abort(404)
        return send_file(path, mimetype='image/png')

    @app.route('/fig/price/<symbol>.png')
    def price_figure(symbol):
        hist = _hist()
        if hist.empty:
            abort(404, 'no historical.csv — run run_pipeline.py')
        rows = _rows('combined.json', app_data_dir)
        col = _hist_column(hist, symbol)
        if col is None:
            abort(404, f'no historical data for {symbol}')

        try:
            t0 = pd.Timestamp(request.args['start']) if request.args.get('start') \
                else hist.index.min()
            t1 = pd.Timestamp(request.args['end']) if request.args.get('end') \
                else hist.index.max()
        except ValueError:
            abort(400, 'dates must be YYYY-MM-DD')
        if t0 >= t1:
            abort(400, 'start must be before end')

        prices = hist[col].dropna()
        prices = prices[(prices.index >= t0) & (prices.index <= t1)]
        if len(prices) < 5:
            abort(400, f'only {len(prices)} points in range (need >= 5)')

        qty = next((float(r.get('Quantity') or 0) for r in rows
                    if r.get('Symbol') == symbol), 0.0)
        summary = _state('mpt_summary.json', data_dir) or {}
        fig, _stats = build_price_drawdown_figure(
            prices, symbol, qty=qty, rf_annual=summary.get('rf_annual', DEFAULT_RF))
        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=110, facecolor=fig.get_facecolor())
        plt.close(fig)  # threaded dev server: leaked figures accumulate
        buf.seek(0)
        return send_file(buf, mimetype='image/png')

    # ── discover (candidate screener) ────────────────────────────────────────
    universe_dir = os.path.join(data_dir, 'universe')
    watchlist_path = os.path.join(root_dir, 'watchlist.txt')
    status_path = os.path.join(universe_dir, universe_data.STATUS_FILE)
    _uni_cache: dict = {'mtime': None, 'df': None}
    _refresh_proc: dict = {'proc': None}

    DISCOVER_COLS = ['Symbol', 'Name', 'Asset_Class', 'Sector', 'MarketCap',
                     'Ann_Vol', 'Sharpe_1yr', 'Gain_1yr', 'Gain_3m', 'Beta',
                     'Corr_Portfolio', 'Dividend_Yield', 'Expense_Ratio',
                     'High_52w_Ratio']

    def _universe_df():
        path = os.path.join(universe_dir, universe_data.UNIVERSE_FILE)
        if not os.path.exists(path):
            return None
        mtime = os.path.getmtime(path)
        if _uni_cache['mtime'] != mtime:
            _uni_cache.update(mtime=mtime, df=pd.read_csv(path))
        return _uni_cache['df']

    def _held_symbols() -> frozenset:
        rows = _rows('combined.json', app_data_dir)
        return frozenset(r['Symbol'] for r in rows
                         if r.get('Symbol') and r['Symbol'] != CASH)

    def _require_page_post():
        # CSRF guard for the app's only write endpoints: a cross-site form or
        # fetch cannot attach this custom header; this page's own JS does.
        if request.headers.get('X-FiData') != '1':
            abort(403, 'missing X-FiData header')

    def _refresh_running() -> bool:
        proc = _refresh_proc['proc']
        if proc is not None and proc.poll() is None:
            return True
        st = universe_data.read_status(status_path) or {}
        if st.get('phase') in (None, 'done', 'error'):
            return False
        pid = st.get('pid')
        if not pid:
            return False
        try:
            os.kill(int(pid), 0)
            return True
        except (OSError, ValueError):
            return False

    @app.route('/discover')
    def discover():
        df = _universe_df()
        params, warnings = validate_params(params_from_args(request.args))
        form_state = {**DEFAULTS, **params}
        watching = frozenset(load_watchlist(watchlist_path))
        rows, total, sector_opts, asset_opts = [], 0, [], []
        if df is not None:
            total = len(df)
            res = apply_filters(df, params, held=_held_symbols(),
                                watching=watching)
            res = res.astype(object).where(pd.notna(res), None)
            rows = res.to_dict('records')
            sector_opts = sorted(x for x in df['Sector'].dropna().unique() if x)
            asset_opts = sorted(x for x in df['Asset_Class'].dropna().unique() if x)
        treasury = universe_data.read_status(
            os.path.join(universe_dir, universe_data.TREASURY_FILE)) or {}
        uni_path = os.path.join(universe_dir, universe_data.UNIVERSE_FILE)
        asof_hours = ((pd.Timestamp.now().timestamp() - os.path.getmtime(uni_path))
                      / 3600.0) if os.path.exists(uni_path) else None
        return render_template(
            'discover.html', title='Discover', has_universe=df is not None,
            rows=rows, cols=DISCOVER_COLS, matched=len(rows), total=total,
            form=form_state, warnings=warnings,
            sector_opts=sector_opts, asset_opts=asset_opts,
            range_fields=[(k, humanize(v[0])) for k, v in RANGE_FIELDS.items()],
            sort_options=sorted(SORT_COLUMNS),
            treasury=treasury, asof_hours=asof_hours,
            ai_available=bool(os.environ.get('ANTHROPIC_API_KEY')),
            refresh_running=_refresh_running(),
            **base_ctx('discover'))

    @app.route('/discover/ai', methods=['POST'])
    def discover_ai():
        _require_page_post()
        query = str((request.get_json(silent=True) or {}).get('query') or '').strip()
        if not query:
            abort(400, 'empty query')
        ctx = ai_screener.build_context(app_data_dir, data_dir, _universe_df())
        try:
            return ai_screener.query_to_filters(query, ctx)
        except ai_screener.AiScreenerError as e:
            return {'error': str(e)}, 503

    @app.route('/discover/watchlist', methods=['POST'])
    def discover_watchlist():
        _require_page_post()
        sym = str((request.get_json(silent=True) or {}).get('symbol') or '')
        norm = sym.strip().upper().replace('/', '-')
        held = {s.upper().replace('/', '-') for s in _held_symbols()}
        if norm and norm in held:
            return {'status': 'held'}
        result = append_watchlist(sym, watchlist_path)
        if result == 'invalid':
            abort(400, f'invalid symbol {sym!r}')
        return {'status': result}

    @app.route('/discover/refresh', methods=['POST'])
    def discover_refresh():
        _require_page_post()
        if _refresh_running():
            return {'error': 'a refresh is already running'}, 409
        mode = (request.get_json(silent=True) or {}).get('mode', 'quick')
        os.makedirs(universe_dir, exist_ok=True)
        cmd = [sys.executable, os.path.join(root_dir, 'refresh_universe.py'),
               '--status-file', status_path, '--universe-dir', universe_dir]
        if mode == 'full':
            cmd.append('--full')
        log = open(os.path.join(universe_dir, 'refresh.log'), 'w')
        _refresh_proc['proc'] = subprocess.Popen(
            cmd, cwd=root_dir, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True)
        return {'status': 'started', 'mode': mode}, 202

    @app.route('/discover/refresh/status')
    def discover_refresh_status():
        st = universe_data.read_status(status_path) or {}
        return {**st, 'running': _refresh_running()}

    @app.errorhandler(404)
    @app.errorhandler(400)
    @app.errorhandler(403)
    def _plain_error(e):
        return Response(f'{e.code}: {e.description}\n', status=e.code,
                        mimetype='text/plain')

    return app


def _hist_column(hist: pd.DataFrame, symbol: str):
    """Portfolio symbol -> historical.csv column (BRK/B is stored as BRK-B)."""
    if symbol in hist.columns:
        return symbol
    ysym = symbol.replace('/', '-')
    return ysym if ysym in hist.columns else None


def _chart_symbols(hist: pd.DataFrame, rows: list) -> list:
    if hist.empty:
        return []
    owned = [r['Symbol'] for r in rows
             if r.get('Symbol') and r['Symbol'] != CASH
             and _hist_column(hist, r['Symbol'])]
    return sorted(owned) or sorted(hist.columns)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--port', type=int, default=8787)
    ap.add_argument('--debug', action='store_true')
    args = ap.parse_args()
    try:
        app = create_app()
    except LocalOnlyError:
        print('FIDATA_LOCAL is not set to 1 — refusing to start.\n'
              'This viewer is local-only and must never run on the VM.\n'
              f'Add FIDATA_LOCAL=1 to {os.path.join(_DATA_DIR, ".env")} '
              '(local machine only).', file=sys.stderr)
        return 2
    print(f'fiData local viewer → http://127.0.0.1:{args.port}')
    # host is hardcoded: no flag to accidentally expose this on the network.
    app.run(host='127.0.0.1', port=args.port, debug=args.debug)
    return 0


if __name__ == '__main__':
    sys.exit(main())

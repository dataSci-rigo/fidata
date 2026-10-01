#!/usr/bin/env python3
"""fiData/weekly_input.py — build the compact facts bundle the weekly review
is written from.

No AI, no network: it just distils the pipeline's artifacts into one JSON
small enough to paste into a chat. Two consumers share it, which is the point
— whatever writes the review sees exactly the same numbers:

  * run_weekly_review.py (the automatic Sunday job, via OpenRouter)
  * a Claude session on the laptop — /weekly-review, or downloaded from the
    panel at /fidata/weekly_input.json and pasted in by hand

Every input file is optional; a missing artifact just omits its section
rather than failing, so this still produces something useful on a machine
where the pipeline has never fully run.
"""
import argparse
import json
import os

DATA_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DATA = os.path.join(DATA_DIR, 'app_data')
DATA_STATE = os.path.join(DATA_DIR, 'data')
OUT_NAME = 'weekly_input.json'

TOP_HOLDINGS = 25
TOP_MOVERS = 8
TOP_LOSERS = 12
TOP_NEWS = 8


def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _num(val):
    try:
        f = float(val)
        return None if f != f else f          # NaN -> None
    except (TypeError, ValueError):
        return None


def _round(val, digits=2):
    f = _num(val)
    return None if f is None else round(f, digits)


def _tax_sections(accounts_dir: str, prices=None) -> dict:
    """Account tax status + what is actually harvestable.

    Without this the bundle described a merged portfolio with no notion of tax
    treatment, and the review recommended harvesting losses inside IRAs. Uses
    the broker's own cost basis, not the reconstruction from transaction
    history — the broker accounts for splits and wash sales, and the
    reconstruction demonstrably does not (AVGO read as a 22% loss against the
    broker's 269% gain).
    """
    try:
        import tax
        from parsers import load_positions
        from run_pipeline import EXCLUDE_FILES
        accounts = load_positions(accounts_dir, exclude=EXCLUDE_FILES)
    except Exception as e:
        print(f'weekly_input: tax sections unavailable: {e}')
        return {}

    summary = tax.account_summary(accounts)
    pos = tax.taxable_positions(accounts, prices=prices)
    by_status: dict[str, float] = {}
    for row in summary:
        by_status[row['status']] = by_status.get(row['status'], 0.0) + row['market_value']

    out: dict = {
        'accounts': summary,
        'tax_totals': {k: round(v, 0) for k, v in by_status.items()},
        'harvest_candidates': tax.harvest_candidates(accounts, prices=prices),
        'harvest_note': ('Only taxable accounts appear here. A realized loss in an '
                         'IRA/Roth/rollover has no tax effect, and those accounts hold '
                         f"${by_status.get('tax_advantaged', 0):,.0f} of the book."),
    }
    out['_taxable_frame'] = pos          # internal, popped before serialising
    if not pos.empty:
        def _row(s, r):
            return {'symbol': str(s),
                    'market_value': _round(r['Market_Value'], 0),
                    'cost_basis': _round(r['Cost_Basis'], 0),
                    'unrealized': _round(r['Unrealized'], 0),
                    'return_pct': _round(r['Return_Pct'])}

        top = pos.reindex(pos['Market_Value'].sort_values(ascending=False).index)
        shown = top.head(20)
        # Losers are small by construction, so a top-20-by-value cut drops
        # exactly the rows a loss question is about — SEVN sat at position 30
        # and read as "no taxable losses at all". Always append them.
        losers = pos[pos['Unrealized'].notna() & (pos['Unrealized'] < 0)]
        extra = losers[~losers.index.isin(shown.index)]
        out['taxable_positions'] = (
            [_row(s, r) for s, r in shown.iterrows()]
            + [_row(s, r) for s, r in extra.sort_values('Unrealized').iterrows()])
        out['taxable_positions_note'] = (
            f'{len(pos)} taxable positions in total; listed here are the 20 '
            f'largest by value plus every position at a loss. '
            f'{int(pos["Cost_Basis"].isna().sum())} have no broker cost basis '
            f'and are excluded from all tax math.')
    return out


GAIN_BUDGET_DEFAULT = 50_000.0     # FIDATA_GAIN_BUDGET overrides


def _performance_sections(hist_file: str, rows: list, taxable_pos,
                          combined_by_sym: dict, budget: float) -> dict:
    """Multi-year performance vs SPY and sector, plus the gain-realization
    plan that a low-income year makes worth having."""
    try:
        import pandas as pd
        import performance
        import tax
        from parsers.transactions import load_transactions
        hist = pd.read_csv(hist_file, index_col=0, parse_dates=True).sort_index()
    except Exception as e:
        print(f'weekly_input: performance sections unavailable: {e}')
        return {}

    sectors = {r['Symbol']: (r.get('Sector') or '') for r in rows}
    mvs = {r['Symbol']: _num(r.get('Market_Value')) or 0 for r in rows}
    perf = performance.position_performance(hist, sectors)
    if perf.empty:
        return {}

    out: dict = {
        'benchmarks': {k: v for k, v in performance.benchmark_returns(hist).items()
                       if k in (performance.MARKET,) or v.get(5) is not None},
        'laggards': performance.laggards(perf, mvs)[:15],
        'laggards_note': ('Price return over five years against SPY and against the '
                          "position's own sector ETF. behind_both = trailing the market "
                          'AND its sector, so the sector is not the excuse. This is '
                          'independent of cost basis: it asks what the capital did while '
                          'it sat there.'),
    }

    # Gain realization: order the sell list by how badly a position has lagged,
    # so the plan clears dead money rather than chasing the biggest gain.
    if taxable_pos is not None and not taxable_pos.empty:
        try:
            tx = load_transactions(os.path.join(DATA_DIR, 'buysell'))
            last_buy = (tx[tx['Action'].isin(['BUY', 'REINVEST'])]
                        .groupby('Symbol')['Date'].max().to_dict()) if not tx.empty else {}
        except Exception:
            last_buy = {}
        terms = {sym: tax.holding_term(
                    (combined_by_sym.get(sym) or {}).get('Cost_Basis_Source'),
                    last_buy.get(sym))
                 for sym in taxable_pos.index}
        lag_order = [d['symbol'] for d in performance.laggards(perf, mvs, min_value=0)]
        order = [s for s in lag_order if s in taxable_pos.index]
        plan = tax.gain_harvest_plan(taxable_pos, order, budget, terms)
        plan['note'] = (f'Long-term positions only, worst five-year performers first, '
                        f'stopping at a ${budget:,.0f} realized-gain budget. The budget '
                        f'is the 0% long-term bracket as the user stated it — it applies '
                        f'to taxable income INCLUDING these gains, and state tax (CA has '
                        f'no preferential rate) is not modeled here.')
        # Federal headroom alone reads as "free". California taxes the same
        # gain as ordinary income, so price it here rather than leave the
        # review to hand-wave a state figure it has no schedule for.
        other_income = float(os.getenv('FIDATA_OTHER_INCOME') or 0.0)
        filing = (os.getenv('FIDATA_FILING_STATUS') or 'single').strip().lower()
        plan['tax_cost'] = tax.combined_tax_on_gain(
            plan['gain_realized'], other_income, filing)
        plan['tax_cost']['assumes'] = (
            f'{filing}, ${other_income:,.0f} of other income this year — set '
            f'FIDATA_OTHER_INCOME / FIDATA_FILING_STATUS when that changes. '
            f'Models federal LTCG + NIIT + California only: not ACA premium '
            f'credit clawback, kiddie tax, EITC disqualification, or any loss '
            f'carryforward, each of which can exceed this bill.')
        plan['state_tax'] = plan['tax_cost']['state']   # back-compat
        out['gain_harvest'] = plan
        out['holding_terms'] = {k: v for k, v in terms.items() if v != 'long'}
    return out


def build(app_data_dir: str = APP_DATA, data_dir: str = DATA_STATE,
          accounts_dir: str | None = None, hist_file: str | None = None) -> dict:
    """The bundle. Keys are omitted (not null) when their source is missing."""
    import pandas as pd

    combined = _load(os.path.join(app_data_dir, 'combined.json')) or []
    sectors = _load(os.path.join(app_data_dir, 'sectors.json')) or {}
    earnings = _load(os.path.join(app_data_dir, 'earnings.json')) or []
    mpt = _load(os.path.join(data_dir, 'mpt_summary.json')) or {}
    extras = _load(os.path.join(data_dir, 'portfolio_extras.json')) or {}
    news = _load(os.path.join(data_dir, 'news_feed.json')) or {}

    out: dict = {'generated_at': str(pd.Timestamp.now(tz='America/Los_Angeles'))}
    # When the POSITIONS were last priced, which is not when this bundle was
    # built — the first published page dated itself to build time and so
    # implied the holdings were hours old when they were weeks old.
    combined_path = os.path.join(app_data_dir, 'combined.json')
    if os.path.exists(combined_path):
        out['data_as_of'] = str(pd.Timestamp(os.path.getmtime(combined_path), unit='s',
                                             tz='UTC').tz_convert('America/Los_Angeles'))

    rows = [r for r in combined if r.get('Symbol') and r['Symbol'] != 'cash']
    cash = next((r for r in combined if r.get('Symbol') == 'cash'), None)
    total_mv = sum(_num(r.get('Market_Value')) or 0 for r in rows)

    if mpt:
        cur = mpt.get('current') or {}
        out['mpt'] = {
            'return_pct': _round((cur.get('return') or 0) * 100),
            'vol_pct': _round((cur.get('vol') or 0) * 100),
            'sharpe': _round(cur.get('sharpe')),
            'beta': _round(mpt.get('port_beta')),
            'hhi': _round(mpt.get('hhi'), 0),
            'effective_n': _round(mpt.get('effective_n'), 1),
            'rf_pct': _round((mpt.get('rf_annual') or 0) * 100),
            'n_symbols': mpt.get('n_symbols'),
            'high_correlation_pairs': (mpt.get('high_correlation_pairs') or [])[:10],
        }

    capital = (extras.get('capital') or {}).get('totals') or {}
    if capital or total_mv:
        out['totals'] = {
            'market_value': _round(total_mv, 0),
            'cash': _round((cash or {}).get('Market_Value'), 0),
            'positions': len(rows),
            # dollars to the whole unit, percentages keep their decimals —
            # an ROIC of "12" instead of 12.35 throws away the useful part
            **{k: _round(v, 2 if k.endswith('_pct') else 0)
               for k, v in capital.items()},
        }
    cash_flows = (extras.get('cash_flows') or {}).get('totals')
    if cash_flows:
        out['cash_flows'] = {k: _round(v, 0) for k, v in cash_flows.items()}

    if sectors.get('by_gics'):
        out['sectors'] = sectors['by_gics']

    if rows:
        def holding(r):
            mv = _num(r.get('Market_Value')) or 0
            price, avg = _num(r.get('Current_Price')), _num(r.get('Avg_Buy_Price'))
            return {
                'symbol': r['Symbol'],
                'market_value': _round(mv, 0),
                'weight_pct': _round(mv / total_mv * 100 if total_mv else None),
                'return_pct': _round((price / avg - 1) * 100
                                     if price and avg else None),
                'gain_3m': _round(r.get('Gain_3m')),
                'gain_1yr': _round(r.get('Gain_1yr')),
                'ann_vol_pct': _round((_num(r.get('Ann_Vol')) or 0) * 100),
                'beta': _round(r.get('Beta')),
                'sector': r.get('Sector'),
                'cost_basis_source': r.get('Cost_Basis_Source'),
            }

        by_mv = sorted(rows, key=lambda r: -(_num(r.get('Market_Value')) or 0))
        out['holdings'] = [holding(r) for r in by_mv[:TOP_HOLDINGS]]
        out['holdings_note'] = (f'{len(rows)} positions total; the '
                                f'{min(TOP_HOLDINGS, len(rows))} largest are listed')

        with_3m = [r for r in rows if _num(r.get('Gain_3m')) is not None]
        by_3m = sorted(with_3m, key=lambda r: -_num(r.get('Gain_3m')))
        out['movers_3m'] = {
            'best': [holding(r) for r in by_3m[:TOP_MOVERS]],
            'worst': [holding(r) for r in by_3m[-TOP_MOVERS:][::-1]],
        }

        # Tax-loss candidates: unrealized losers, excluding rows whose cost
        # basis is the 2023 default (those returns are not trustworthy).
        losers = [holding(r) for r in rows
                  if r.get('Cost_Basis_Source') != 'default_cutoff']
        losers = [h for h in losers
                  if h['return_pct'] is not None and h['return_pct'] < 0]
        out['unrealized_losses'] = sorted(
            losers, key=lambda h: h['return_pct'])[:TOP_LOSERS]

    if extras.get('closed_positions'):
        out['closed_positions'] = extras['closed_positions'][:15]
    if extras.get('capital', {}).get('annual_activity'):
        out['annual_activity'] = extras['capital']['annual_activity']

    def story(s, extra=()):
        keep = ('symbol', 'title', 'publisher', 'published_at') + tuple(extra)
        return {k: s.get(k) for k in keep if s.get(k)}

    if news.get('position_stories') or news.get('market_stories'):
        out['news'] = {
            'positions': [story(s) for s in (news.get('position_stories') or [])][:TOP_NEWS],
            'market': [story(s, ('why',)) for s in (news.get('market_stories') or [])][:TOP_NEWS],
        }

    # Live prices so the taxable view isn't valued at the export's date.
    live = None
    if rows:
        live = pd.Series({r['Symbol']: _num(r.get('Current_Price')) for r in rows
                          if r.get('Symbol')}).dropna()
    out.update(_tax_sections(accounts_dir or os.path.join(DATA_DIR, 'accounts'), live))
    taxable_frame = out.pop('_taxable_frame', None)
    budget = float(os.getenv('FIDATA_GAIN_BUDGET') or GAIN_BUDGET_DEFAULT)
    out.update(_performance_sections(
        hist_file or os.path.join(DATA_DIR, 'historical.csv'), rows,
        taxable_frame, {r['Symbol']: r for r in rows}, budget))

    if earnings:
        out['upcoming_earnings'] = [
            {'symbol': e.get('Symbol'),
             'date': str(e.get('Next_Earnings'))[:10],
             'eps_est': _round(e.get('EPS_Est'))}
            for e in earnings[:20] if e.get('Next_Earnings')]

    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--out', default=os.path.join(DATA_STATE, OUT_NAME),
                    help=f'output path (default: data/{OUT_NAME})')
    ap.add_argument('--stdout', action='store_true',
                    help='print the bundle instead of writing it')
    ap.add_argument('--app-data-dir', default=APP_DATA, help=argparse.SUPPRESS)
    ap.add_argument('--data-dir', default=DATA_STATE, help=argparse.SUPPRESS)
    ap.add_argument('--accounts-dir', default=None, help=argparse.SUPPRESS)
    ap.add_argument('--hist-file', default=None, help=argparse.SUPPRESS)
    args = ap.parse_args()

    bundle = build(args.app_data_dir, args.data_dir, args.accounts_dir,
                   args.hist_file)
    text = json.dumps(bundle, indent=2, default=str)
    if args.stdout:
        print(text)
        return 0
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, 'w') as f:
        f.write(text + '\n')
    print(f'{args.out}: {len(text):,} chars, sections: '
          f"{', '.join(k for k in bundle if k != 'generated_at')}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

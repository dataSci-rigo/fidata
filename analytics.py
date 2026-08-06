"""Portfolio analytics: merging accounts, cost basis, capital tracking,
snapshot-diff trade inference, MPT metrics, and the app_data JSON export.

This module is the headless-safe replacement for notebook cells 1, 2, 3, 5,
7, 12, 15, 16 — no plotting or interactive input happens here, so
`run_pipeline.py` can call the numeric functions without a display backend
or a human at the keyboard.
"""
import json
import os
from datetime import date as _date
from itertools import groupby as _groupby

import numpy as np
import pandas as pd

from parsers import clean_num, is_option, load_snapshot

CUTOFF = pd.Timestamp('2023-01-01')
DEFAULT_BUY = pd.bdate_range('2023-01-01', periods=1)[0]  # first trading day on/after Jan 1


# ── Cell 1: merge_accounts ─────────────────────────────────────────────────────

def merge_accounts(accounts: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Merge all per-account position DataFrames into one, grouped by Symbol."""
    all_rows = pd.concat(accounts.values())
    all_rows.index.name = 'Symbol'

    cash_rows = all_rows[all_rows.index == 'cash']
    equity_rows = all_rows[all_rows.index != 'cash']

    price_check = equity_rows.groupby('Symbol')['Current_Price'].agg(['min', 'max'])
    price_mismatches = price_check[abs(price_check['max'] - price_check['min']) > 0.01]
    if not price_mismatches.empty:
        print('⚠ Price mismatches across accounts (same symbol, different price):')
        print(price_mismatches.to_string())
        print()

    combined = (
        equity_rows
        .groupby('Symbol')
        .agg(
            Quantity=('Quantity', 'sum'),
            Current_Price=('Current_Price', 'mean'),
            Market_Value=('Market_Value', 'sum'),
        )
        .sort_index()
    )

    total_cash = cash_rows['Market_Value'].sum()
    cash_combined = pd.DataFrame(
        {'Quantity': [float('nan')], 'Current_Price': [1.0], 'Market_Value': [total_cash]},
        index=pd.Index(['cash'], name='Symbol'))

    combined = pd.concat([combined, cash_combined])
    # Bug fix: original code called .rename(...) without reassigning, so the
    # BRK/B -> BRK-B rename never actually took effect.
    combined = combined.rename(index={'BRK/B': 'BRK-B'})
    return combined


# ── Cell 2: cost basis ─────────────────────────────────────────────────────────

def load_fallback_cost_basis(accounts_dir: str) -> dict[str, float]:
    """Symbol -> avg cost per share, read from the brokerage's own 'Average Cost Basis' column."""
    import io
    fid_cost: dict[str, float] = {}
    for fn in sorted(os.listdir(accounts_dir)):
        if not fn.endswith('.csv'):
            continue
        fp = os.path.join(accounts_dir, fn)
        try:
            with open(fp, 'r', encoding='utf-8-sig') as f:
                lines = f.readlines()
            clean = [l.rstrip(',\n') + '\n' for l in lines]
            df_cb = pd.read_csv(io.StringIO(''.join(clean)))
            if 'Average Cost Basis' not in df_cb.columns or 'Symbol' not in df_cb.columns:
                continue
            df_cb = df_cb[['Symbol', 'Average Cost Basis']].dropna()
            for _, r in df_cb.iterrows():
                sym = str(r['Symbol']).strip()
                val = clean_num(r['Average Cost Basis'])
                if sym and not pd.isna(val):
                    fid_cost[sym] = val
        except Exception:
            continue
    return fid_cost


def compute_cost_basis(combined: pd.DataFrame, tx_df: pd.DataFrame,
                        fid_cost: dict[str, float],
                        cutoff: pd.Timestamp = CUTOFF,
                        default_buy: pd.Timestamp = DEFAULT_BUY) -> pd.DataFrame:
    """Add First_Buy_Date, Days_Held, Avg_Buy_Price, Cost_Basis_Source to `combined`.

    Cost_Basis_Source values:
      'transaction_history'  — real BUY/REINVEST transactions on/after `cutoff`
      'brokerage_fallback'   — no post-cutoff buys; used the broker's own
                                 'Average Cost Basis' export column instead
      'default_cutoff'       — neither available; First_Buy_Date fudged to
                                 `default_buy` and Avg_Buy_Price is unknown
    """
    combined = combined.copy()
    today_ts = pd.Timestamp(_date.today())

    buys = (tx_df[(tx_df['Action'].isin(['BUY', 'REINVEST'])) & (tx_df['Date'] >= cutoff)]
            if not tx_df.empty else pd.DataFrame())
    first_buy = (buys.groupby('Symbol')['Date'].min()
                 if not buys.empty else pd.Series(dtype='datetime64[ns]'))
    avg_cost = (buys.groupby('Symbol')
                .apply(lambda g: (g['Price'] * g['Quantity']).sum() / g['Quantity'].sum())
                if not buys.empty else pd.Series(dtype=float))

    combined['First_Buy_Date'] = pd.to_datetime(combined.index.map(first_buy))
    combined['Avg_Buy_Price'] = combined.index.map(avg_cost)

    source = pd.Series('transaction_history', index=combined.index)
    source[combined['First_Buy_Date'].isna()] = 'default_cutoff'

    missing_mask = combined['Avg_Buy_Price'].isna() & (combined.index != 'cash')
    fallback = combined.loc[missing_mask].index.map(lambda s: fid_cost.get(s, float('nan')))
    combined.loc[missing_mask, 'Avg_Buy_Price'] = fallback
    used_fallback = missing_mask & combined['Avg_Buy_Price'].notna()
    source[used_fallback] = 'brokerage_fallback'

    combined['First_Buy_Date'] = combined['First_Buy_Date'].fillna(default_buy)
    combined['Days_Held'] = (today_ts - combined['First_Buy_Date']).dt.days.astype('Int64')
    combined['Cost_Basis_Source'] = source
    combined.loc[combined.index == 'cash', 'Cost_Basis_Source'] = 'n/a'

    return combined


def capital_deployed(tx_df: pd.DataFrame) -> pd.DataFrame:
    """Annual Bought/Sold/Net_Deployed, counting only real BUY/SELL cash flow
    (REINVEST is excluded — dividend reinvestment isn't new capital in)."""
    if tx_df.empty or 'Price' not in tx_df.columns:
        return pd.DataFrame(columns=['Bought', 'Sold', 'Net_Deployed'])

    cf = tx_df[tx_df['Action'].isin(['BUY', 'SELL'])].copy()
    cf = cf[cf['Price'].notna() & cf['Quantity'].notna()]
    cf['Cash_Flow'] = cf.apply(
        lambda r: -(r['Price'] * r['Quantity']) if r['Action'] == 'BUY'
        else (r['Price'] * r['Quantity']), axis=1)
    cf['Year'] = cf['Date'].dt.year

    annual = (cf.groupby('Year')
              .agg(Bought=('Cash_Flow', lambda x: -x[x < 0].sum()),
                   Sold=('Cash_Flow', lambda x: x[x > 0].sum()))
              .assign(Net_Deployed=lambda d: d['Bought'] - d['Sold']))
    return annual.round(2)


def dividends_reinvested(tx_df: pd.DataFrame) -> pd.Series:
    """Annual dollar total of REINVEST transactions, tracked separately from
    Capital_Deployed since it isn't new capital in."""
    if tx_df.empty or 'REINVEST' not in tx_df['Action'].unique().tolist():
        return pd.Series(dtype=float)
    ri = tx_df[tx_df['Action'] == 'REINVEST'].copy()
    ri = ri[ri['Price'].notna() & ri['Quantity'].notna()]
    ri['Year'] = ri['Date'].dt.year
    ri['Amount'] = ri['Price'] * ri['Quantity']
    return ri.groupby('Year')['Amount'].sum().round(2)


# ── Cell 5: snapshot-diff trade inference (headless — bug fix #3) ─────────────

def collect_snapshots(past_dir: str, accounts_dir: str) -> dict:
    """{(acct, date): DataFrame(Quantity, Price)} — keep the snapshot with the
    most symbols when the same (acct, date) shows up in both folders."""
    snap_map: dict = {}
    for folder in [past_dir, accounts_dir]:  # past first so accounts/ overwrites with newer data
        for fn in sorted(os.listdir(folder), key=lambda f: os.path.getmtime(os.path.join(folder, f))):
            if not fn.endswith('.csv'):
                continue
            fp = os.path.join(folder, fn)
            snap_dict, snap_date = load_snapshot(fp)
            if snap_date is None:
                continue
            for acct, df in snap_dict.items():
                key = (acct, snap_date)
                if key not in snap_map or len(df) > len(snap_map[key]):
                    snap_map[key] = df
    return snap_map


def infer_missing_trades(snap_map: dict, tx_df: pd.DataFrame, hist_df: pd.DataFrame,
                          unknown_file: str) -> pd.DataFrame:
    """Diff consecutive snapshots per account, cross-reference tx_df, and
    return a DataFrame of inferred BUY/SELL trades.

    Bug fix #3: no `input()` prompt. Any unexplained delta whose price can't
    be resolved from the snapshot or `hist_df` is unconditionally logged to
    `unknown_file` and skipped — this is what makes the pipeline safe to run
    unattended (cron/systemd), not just interactively in a notebook.
    """
    QTY_TOL = 0.01
    unknown_trades = []
    inferred_rows = []

    all_snaps = sorted(snap_map.items(), key=lambda x: (x[0][0], x[0][1]))

    for acct, grp in _groupby(all_snaps, key=lambda x: x[0][0]):
        timeline = [(date, df) for (_, date), df in grp]
        for i in range(len(timeline) - 1):
            t1_date, df1 = timeline[i]
            t2_date, df2 = timeline[i + 1]

            for sym in set(df1.index) | set(df2.index):
                if is_option(sym):
                    continue
                q1 = float(df1.loc[sym, 'Quantity']) if sym in df1.index else 0.0
                q2 = float(df2.loc[sym, 'Quantity']) if sym in df2.index else 0.0
                delta = q2 - q1
                if abs(delta) < QTY_TOL:
                    continue

                if not tx_df.empty:
                    w = tx_df[(tx_df['Symbol'] == sym) &
                              (tx_df['Account'] == acct) &
                              (tx_df['Date'] > t1_date) &
                              (tx_df['Date'] <= t2_date)]
                    known_delta = (w.loc[w['Action'].isin(['BUY', 'REINVEST']), 'Quantity'].sum() -
                                   w.loc[w['Action'] == 'SELL', 'Quantity'].sum())
                else:
                    known_delta = 0.0

                unexplained = round(delta - known_delta, 6)
                if abs(unexplained) < QTY_TOL:
                    continue

                action = 'BUY' if unexplained > 0 else 'SELL'
                qty = abs(unexplained)

                snap_src = df2 if sym in df2.index else df1
                price = float(snap_src.loc[sym, 'Price']) if 'Price' in snap_src.columns else float('nan')

                if (pd.isna(price) or price <= 0) and sym in hist_df.columns:
                    avail = hist_df[sym].dropna()
                    if not avail.empty:
                        idx = avail.index.get_indexer([t2_date], method='nearest')[0]
                        price = float(avail.iloc[idx])

                if pd.isna(price) or price <= 0:
                    unknown_trades.append(dict(Symbol=sym, Account=acct, Action=action,
                                                Quantity=qty, T1=str(t1_date.date()),
                                                T2=str(t2_date.date())))
                    continue

                inferred_rows.append(dict(Symbol=sym, Date=t2_date, Action=action,
                                           Quantity=qty, Price=round(price, 4), Account=acct))

    if unknown_trades:
        pd.DataFrame(unknown_trades).to_csv(unknown_file, index=False)
        print(f'  {len(unknown_trades)} unknown trade(s) saved -> {unknown_file}')

    if inferred_rows:
        inf_df = pd.DataFrame(inferred_rows)
        inf_df['Date'] = pd.to_datetime(inf_df['Date'])
        return inf_df

    return pd.DataFrame(columns=['Symbol', 'Date', 'Action', 'Quantity', 'Price', 'Account'])


# ── Cell 15/16: MPT ─────────────────────────────────────────────────────────────

def mpt_metrics(combined: pd.DataFrame, hist_df: pd.DataFrame, rf_annual: float,
                 lookback: int = 252) -> dict:
    """Core MPT numbers for the current (market-value-weighted) portfolio.

    Pure numeric function — no plotting — so run_pipeline.py can call it
    headlessly and the notebook's plotting cell can call it too and just add
    matplotlib on top.
    """
    eq_all = combined.index[combined.index != 'cash'].tolist()
    avail = [s for s in eq_all if s in hist_df.columns]

    prices = hist_df[avail].iloc[-(lookback + 1):].ffill()
    prices = prices.loc[:, prices.count() >= 200]
    rets = prices.pct_change().iloc[1:].fillna(0)
    mpt_syms = rets.columns.tolist()

    mu = rets.mean() * 252
    cov = rets.cov() * 252

    mv = combined.loc[mpt_syms, 'Market_Value'].fillna(0)
    w0 = (mv / mv.sum()).values if mv.sum() > 0 else np.zeros(len(mpt_syms))

    def port_stats(w):
        r = float(w @ mu.values)
        vol = float(np.sqrt(np.maximum(w @ cov.values @ w, 0)))
        sr = (r - rf_annual) / vol if vol > 0 else 0.0
        return r, vol, sr

    p_ret, p_vol, p_sr = port_stats(w0)

    result = {
        'symbols': mpt_syms, 'mu': mu, 'cov': cov, 'w0': w0, 'rets': rets,
        'port_stats': port_stats,
        'p_ret': p_ret, 'p_vol': p_vol, 'p_sr': p_sr,
        'rf_annual': rf_annual,
    }

    if 'SPY' in rets.columns:
        spy_r = rets['SPY']
        spy_v = spy_r.var()
        beta_alpha = {}
        for sym in mpt_syms:
            b = rets[sym].cov(spy_r) / spy_v
            a = mu[sym] - b * (spy_r.mean() * 252)
            beta_alpha[sym] = {'Beta': round(b, 3), 'Alpha_pct': round(a * 100, 2)}
        ba_df = pd.DataFrame(beta_alpha).T.astype(float)
        ba_df.index.name = 'Symbol'
        result['beta_alpha'] = ba_df
        result['port_beta'] = float(w0 @ ba_df['Beta'].reindex(mpt_syms).fillna(1).values)

    Sw = cov.values @ w0
    if p_vol > 0:
        rc_vol = w0 * Sw / p_vol
        rc_pct = rc_vol / p_vol * 100
    else:
        rc_pct = np.zeros(len(mpt_syms))
    risk_df = pd.DataFrame({'Weight_pct': (w0 * 100).round(2),
                             'RiskContrib_pct': np.round(rc_pct, 2)}, index=mpt_syms)
    risk_df.index.name = 'Symbol'
    result['risk_contrib'] = risk_df
    result['hhi'] = float(((w0 * 100) ** 2).sum())
    result['effective_n'] = float(1.0 / (w0 ** 2).sum()) if (w0 ** 2).sum() > 0 else 0.0

    return result


def efficient_frontier(metrics: dict, n_mc: int = 3000, seed: int = 42) -> dict:
    """Monte Carlo cloud + max-Sharpe/min-variance optimization. Pure numeric,
    no plotting — the notebook cell adds matplotlib on top of this output."""
    from scipy.optimize import minimize

    mpt_syms = metrics['symbols']
    n = len(mpt_syms)
    port_stats = metrics['port_stats']
    rf_annual = metrics['rf_annual']

    rng = np.random.default_rng(seed)
    mc_r, mc_v, mc_s = [], [], []
    for _ in range(n_mc):
        w = rng.dirichlet(np.ones(n))
        r, v, s = port_stats(w)
        mc_r.append(r)
        mc_v.append(v)
        mc_s.append(s)

    cons = [{'type': 'eq', 'fun': lambda w: w.sum() - 1}]
    bounds = [(0.0, 1.0)] * n
    w_init = np.ones(n) / n

    def neg_sharpe(w):
        r, v, _ = port_stats(w)
        return -(r - rf_annual) / v if v > 0 else 1e6

    def min_vol_fn(w):
        _, v, _ = port_stats(w)
        return v

    opt_ms = minimize(neg_sharpe, w_init, method='SLSQP', bounds=bounds,
                       constraints=cons, options={'maxiter': 2000, 'ftol': 1e-9})
    w_ms = opt_ms.x
    r_ms, v_ms, s_ms = port_stats(w_ms)

    opt_mv = minimize(min_vol_fn, w_init, method='SLSQP', bounds=bounds,
                       constraints=cons, options={'maxiter': 2000, 'ftol': 1e-9})
    w_mv = opt_mv.x
    r_mv, v_mv, s_mv = port_stats(w_mv)

    return {
        'mc_r': np.array(mc_r) * 100, 'mc_v': np.array(mc_v) * 100, 'mc_s': np.array(mc_s),
        'w_max_sharpe': pd.Series(w_ms, index=mpt_syms),
        'max_sharpe': {'ret': r_ms, 'vol': v_ms, 'sharpe': s_ms},
        'w_min_var': pd.Series(w_mv, index=mpt_syms),
        'min_var': {'ret': r_mv, 'vol': v_mv, 'sharpe': s_mv},
    }


# ── Cell 17: correlation matrix ──────────────────────────────────────────────────

def correlation_matrix(combined: pd.DataFrame, rets: pd.DataFrame,
                        top_n: int = 25) -> tuple[pd.DataFrame, list[str]]:
    """Correlation matrix for the top `top_n` MPT-universe positions by market
    value. `rets` is the daily-returns DataFrame from mpt_metrics()['rets'] —
    pass the same one so the correlation windows line up with the MPT run.
    Pure numeric, no plotting — mirrors mpt_metrics/efficient_frontier."""
    mpt_syms = rets.columns.tolist()
    n = min(top_n, len(mpt_syms))
    top_syms = combined.loc[mpt_syms, 'Market_Value'].nlargest(n).index.tolist()
    corr = rets[top_syms].corr().round(2)
    return corr, top_syms


def high_correlation_pairs(corr: pd.DataFrame, top_syms: list[str],
                            threshold: float = 0.85) -> list[tuple[str, str, float]]:
    """Symbol pairs with |correlation| above `threshold`, most-correlated first."""
    pairs = [
        (s1, s2, corr.loc[s1, s2])
        for i, s1 in enumerate(top_syms)
        for s2 in top_syms[i + 1:]
        if abs(corr.loc[s1, s2]) > threshold
    ]
    return sorted(pairs, key=lambda x: abs(x[2]), reverse=True)


# ── Cell 12: app_data JSON export ────────────────────────────────────────────────

def _df_to_records(df: pd.DataFrame) -> list[dict]:
    return json.loads(df.reset_index().to_json(orient='records', date_format='iso'))


def _sector_summary(df: pd.DataFrame, group_col: str, gain_cols: list[str]) -> list[dict]:
    if group_col not in df.columns:
        return []
    g = df.groupby(group_col)
    mv = g['Market_Value'].sum().rename('Total_Market_Value')
    wg = {}
    for gc in gain_cols:
        if gc not in df.columns:
            continue
        sub = df[['Market_Value', gc, group_col]].dropna(subset=[gc])
        wg[gc] = sub.groupby(group_col).apply(
            lambda x: (x[gc] * x['Market_Value']).sum() / x['Market_Value'].sum(),
            include_groups=False).round(2)
    result = pd.concat([mv, pd.DataFrame(wg)], axis=1)
    result['Total_Market_Value'] = result['Total_Market_Value'].round(2)
    return _df_to_records(result.reset_index())


def export_app_data(app_data_dir: str, accounts: dict[str, pd.DataFrame],
                     combined: pd.DataFrame, earn_file: str | None = None,
                     upgrades_file: str | None = None) -> None:
    """Write accounts.json, combined.json, sectors.json, flags.json,
    targets.json, earnings.json, recommendations.json, upgrades.json."""
    os.makedirs(app_data_dir, exist_ok=True)

    acct_out = {acct_id: _df_to_records(df) for acct_id, df in accounts.items()}
    with open(os.path.join(app_data_dir, 'accounts.json'), 'w') as f:
        json.dump(acct_out, f, indent=2)

    with open(os.path.join(app_data_dir, 'combined.json'), 'w') as f:
        json.dump(_df_to_records(combined), f, indent=2)

    eq = combined[combined.index != 'cash'].copy()
    gain_cols = ['Gain_3m', 'Gain_6m', 'Gain_1yr']

    sector_data = {
        'by_gics': _sector_summary(eq, 'Sector', gain_cols),
        'by_cap': _sector_summary(eq, 'Cap_Tier', gain_cols),
        'by_vol': _sector_summary(eq, 'Vol_Tier', gain_cols),
    }
    with open(os.path.join(app_data_dir, 'sectors.json'), 'w') as f:
        json.dump(sector_data, f, indent=2)

    n_flag = 10
    flags = {}
    flag_cols = {
        'high_trailing_pe': ('Trailing_PE', 'largest'),
        'high_forward_pe': ('Forward_PE', 'largest'),
        'worst_3m': ('Gain_3m', 'smallest'),
        'low_forward_pe': ('Forward_PE', 'smallest'),
        'best_3m': ('Gain_3m', 'largest'),
    }
    for key, (col, direction) in flag_cols.items():
        if col not in eq.columns:
            continue
        sub = eq[col].dropna()
        idx = sub.nlargest(n_flag).index if direction == 'largest' else sub.nsmallest(n_flag).index
        keep = [c for c in ['Trailing_PE', 'Forward_PE', 'Current_Price', 'Market_Value',
                             'Gain_3m', 'Gain_6m', 'Gain_1yr', 'Sharpe_3m', 'Target_Mean']
                if c in eq.columns]
        flags[key] = _df_to_records(eq.loc[idx, keep])
    with open(os.path.join(app_data_dir, 'flags.json'), 'w') as f:
        json.dump(flags, f, indent=2)

    tgt_cols = [c for c in ['Current_Price', 'Target_Median', 'Target_High', 'Target_Low',
                             'Target_Mean', 'Target_Upside', 'Target_Spread', 'Num_Analysts']
                if c in eq.columns]
    if 'Target_Median' in eq.columns and 'Current_Price' in eq.columns:
        tgt = eq[tgt_cols].dropna(subset=['Target_Median', 'Current_Price']).copy()
        if 'Target_Upside' not in tgt.columns:
            tgt['Target_Upside'] = ((tgt['Target_Median'] / tgt['Current_Price'] - 1) * 100).round(2)
        if 'Target_Spread' not in tgt.columns and 'Target_High' in tgt.columns:
            tgt['Target_Spread'] = ((tgt['Target_High'] - tgt['Target_Low']) / tgt['Target_Median'] * 100).round(2)
        targets_data = {
            'overvalued': _df_to_records(tgt[tgt['Target_Median'] < tgt['Current_Price']].sort_values('Target_Upside')),
            'most_upside': _df_to_records(tgt.nlargest(10, 'Target_Upside')),
            'tightest': _df_to_records(tgt.nsmallest(10, 'Target_Spread')) if 'Target_Spread' in tgt.columns else [],
        }
    else:
        targets_data = {}
    with open(os.path.join(app_data_dir, 'targets.json'), 'w') as f:
        json.dump(targets_data, f, indent=2)

    if earn_file and os.path.exists(earn_file):
        earn_df = pd.read_csv(earn_file, index_col='Symbol', parse_dates=['Next_Earnings'])
        today = pd.Timestamp(_date.today())
        upcoming = (
            earn_df[earn_df.index.isin(combined.index) &
                    earn_df['Next_Earnings'].notna() &
                    (earn_df['Next_Earnings'] > today)]
            .sort_values('Next_Earnings')
            .reset_index())
        with open(os.path.join(app_data_dir, 'earnings.json'), 'w') as f:
            json.dump(json.loads(upcoming.to_json(orient='records', date_format='iso')), f, indent=2)

    rec_cols = [c for c in ['Strong_Buy', 'Buy', 'Hold', 'Sell', 'Strong_Sell', 'Consensus']
                if c in combined.columns]
    if rec_cols:
        recs_out = combined[rec_cols].dropna(how='all')
        with open(os.path.join(app_data_dir, 'recommendations.json'), 'w') as f:
            json.dump(_df_to_records(recs_out), f, indent=2)

    if upgrades_file and os.path.exists(upgrades_file):
        ud_df = pd.read_csv(upgrades_file)
        with open(os.path.join(app_data_dir, 'upgrades.json'), 'w') as f:
            json.dump(json.loads(ud_df.to_json(orient='records', date_format='iso')), f, indent=2)

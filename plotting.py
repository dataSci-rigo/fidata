"""Matplotlib plot generation for MPT/efficient-frontier and correlation
analysis. Used both headlessly (run_pipeline.py, which sets the Agg backend
itself before importing this module — see the comment at the top of
run_pipeline.py) and from the notebook (which keeps its own Jupyter-provided
interactive backend). This module deliberately does NOT call
`matplotlib.use()` itself — doing so here would clobber whichever backend
the caller already configured, breaking inline plots in the notebook.
"""
import json
import os

import matplotlib.pyplot as plt

# Shared dark palette — mirrors viewer_app.py's theme constants so the Tk
# viewer, the local web viewer and the saved PNGs all look like one product.
BG = '#1e1e2e'
PANEL = '#2a2a3e'
ACCENT = '#7c6af7'
TEXT = '#cdd6f4'
DIMTEXT = '#6c7086'
RED = '#f38ba8'
BLUE = '#89b4fa'

DEFAULT_RF = 0.043


def build_price_drawdown_figure(prices, sym: str, qty: float = 0.0,
                                 rf_annual: float = DEFAULT_RF, fig=None):
    """Price + drawdown chart with an annualized-vol / Sharpe / max-drawdown box.

    `prices` is a date-indexed close series already sliced to the wanted range.

    `fig` is load-bearing for the Tk viewer: its canvas owns a persistent
    Figure, so it passes that in to be cleared and redrawn rather than getting
    a new one each time (a fresh figure would never reach the canvas). The web
    viewer passes nothing, gets its own figure, and must plt.close() it after
    streaming — the Flask dev server is threaded and leaked figures accumulate.

    Returns (fig, stats) where stats has ann_vol/sharpe/max_dd as fractions
    plus the position's market value and expected annual dollar swing.
    """
    import matplotlib.dates as mdates
    import matplotlib.ticker as mticker
    import numpy as np

    daily_ret = prices.pct_change().dropna()
    std = daily_ret.std()
    ann_vol = std * np.sqrt(252) if std > 0 else 0.0
    rf_daily = rf_annual / 252
    sharpe = ((daily_ret.mean() - rf_daily) / std * np.sqrt(252)) if std > 0 else 0.0

    roll_max = prices.cummax()
    dd = (prices - roll_max) / roll_max
    max_dd = dd.min()

    p_last = float(prices.iloc[-1])
    pos_mv = float(qty or 0) * p_last
    dollar_vol = pos_mv * ann_vol

    if fig is None:
        fig = plt.figure(figsize=(11, 6), facecolor=BG)
    else:
        fig.clear()
    gs = fig.add_gridspec(2, 1, height_ratios=[3, 1], hspace=0.06)
    ax1 = fig.add_subplot(gs[0])
    ax2 = fig.add_subplot(gs[1], sharex=ax1)

    for ax in (ax1, ax2):
        ax.set_facecolor(PANEL)
        ax.tick_params(colors=DIMTEXT, labelsize=8)
        for sp in ax.spines.values():
            sp.set_color('#3a3a5e')
        ax.grid(True, color='#3a3a5e', linewidth=0.4, alpha=0.6)

    ax1.plot(prices.index, prices.values, color=BLUE, lw=1.5, zorder=3)
    ax1.fill_between(prices.index, prices.values, prices.min(), color=BLUE, alpha=0.07)
    ax1.set_ylabel('Price ($)', color=TEXT, fontsize=9)
    ax1.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f'${x:,.0f}'))
    ax1.set_title(
        f'{sym}  ·  {prices.index[0].strftime("%b %d %Y")} → {prices.index[-1].strftime("%b %d %Y")}',
        color=TEXT, fontsize=12, fontweight='bold', pad=6)

    qty_line = f'Qty {int(qty):,}   MV ${pos_mv:,.0f}\n' if qty else ''
    dv_part = f'   (${dollar_vol:,.0f}/yr)' if pos_mv > 0 else ''
    ax1.text(0.02, 0.97,
             f'{qty_line}Ann Vol  {ann_vol*100:.1f}%{dv_part}\n'
             f'Sharpe   {sharpe:.2f}\n'
             f'Max DD   {max_dd*100:.1f}%',
             transform=ax1.transAxes, va='top', fontsize=9, color=TEXT,
             family='monospace',
             bbox=dict(boxstyle='round,pad=0.5', facecolor=BG,
                       edgecolor=ACCENT, alpha=0.92))

    dd_pct = dd * 100
    ax2.fill_between(dd_pct.index, dd_pct.values, 0, where=dd_pct.values < 0,
                     color=RED, alpha=0.45)
    ax2.plot(dd_pct.index, dd_pct.values, color=RED, lw=0.9)
    ax2.axhline(0, color=DIMTEXT, lw=0.5)
    ax2.set_ylabel('Drawdown %', color=TEXT, fontsize=9)
    ax2.set_ylim(top=0)
    ax2.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f'{x:.0f}%'))
    ax2.xaxis.set_major_formatter(mdates.DateFormatter('%b %Y'))

    fig.autofmt_xdate(rotation=25, ha='right')
    # sharex gridspec + tight_layout always warns "Axes not compatible"; the
    # result is what we want anyway, and this is called per web request.
    import warnings
    with warnings.catch_warnings():
        warnings.filterwarnings('ignore', message='.*not compatible with tight_layout.*')
        fig.tight_layout(h_pad=0.3)

    return fig, {'ann_vol': float(ann_vol), 'sharpe': float(sharpe),
                 'max_dd': float(max_dd), 'last_price': float(p_last),
                 'position_mv': float(pos_mv), 'dollar_vol': float(dollar_vol),
                 'points': int(len(prices))}


def build_efficient_frontier_figure(metrics: dict, ef: dict):
    """Renders the same scatter/markers as notebook cell 16. Returns the
    figure — caller decides whether to plt.show() (notebook, interactive) or
    just save + plt.close() (headless pipeline). Kept separate from
    save_efficient_frontier_plot() so both paths use the exact same drawing
    code without one forcing plt.close() on the other."""
    p_ret, p_vol, p_sr = metrics['p_ret'], metrics['p_vol'], metrics['p_sr']
    n = len(metrics['symbols'])
    r_ms, v_ms, s_ms = ef['max_sharpe']['ret'], ef['max_sharpe']['vol'], ef['max_sharpe']['sharpe']
    r_mv, v_mv, s_mv = ef['min_var']['ret'], ef['min_var']['vol'], ef['min_var']['sharpe']

    fig, ax = plt.subplots(figsize=(12, 7))
    sc = ax.scatter(ef['mc_v'], ef['mc_r'], c=ef['mc_s'], cmap='viridis', alpha=0.35, s=14, zorder=1)
    fig.colorbar(sc, ax=ax, label='Sharpe Ratio')

    ax.scatter(p_vol * 100, p_ret * 100, marker='*', s=420, color='red',
               zorder=5, label=f'Current  (SR={p_sr:.2f}, Vol={p_vol:.1%})')
    ax.scatter(v_ms * 100, r_ms * 100, marker='D', s=150, color='gold',
               edgecolors='black', zorder=5,
               label=f'Max Sharpe  (SR={s_ms:.2f}, Vol={v_ms:.1%})')
    ax.scatter(v_mv * 100, r_mv * 100, marker='s', s=150, color='cyan',
               edgecolors='black', zorder=5,
               label=f'Min Variance  (SR={s_mv:.2f}, Vol={v_mv:.1%})')

    ax.set_xlabel('Annualized Volatility (%)', fontsize=12)
    ax.set_ylabel('Annualized Expected Return (%)', fontsize=12)
    ax.set_title(f'Efficient Frontier  (1-yr lookback, long-only, {n} assets)', fontsize=13)
    ax.legend(fontsize=10)
    ax.grid(True, alpha=0.25)
    fig.tight_layout()
    return fig


def save_efficient_frontier_plot(metrics: dict, ef: dict, out_path: str) -> None:
    """Headless: build, save, close — no display needed."""
    fig = build_efficient_frontier_figure(metrics, ef)
    fig.savefig(out_path, dpi=150, bbox_inches='tight')
    plt.close(fig)


def build_correlation_heatmap_figure(corr, top_n: int):
    """Renders the same heatmap as notebook cell 17. Returns the figure —
    see build_efficient_frontier_figure()'s docstring for why."""
    import seaborn as sns

    fig, ax = plt.subplots(figsize=(15, 13))
    sns.heatmap(
        corr, annot=True, fmt='.2f', cmap='RdYlGn_r',
        vmin=-1, vmax=1, center=0,
        linewidths=0.3, linecolor='gray',
        annot_kws={'size': 7}, ax=ax,
    )
    ax.set_title(
        f'Return Correlations — Top {top_n} Positions by Market Value\n(1-yr daily returns)',
        fontsize=13,
    )
    fig.tight_layout()
    return fig


def save_correlation_heatmap_plot(corr, top_n: int, out_path: str) -> None:
    """Headless: build, save, close — no display needed."""
    fig = build_correlation_heatmap_figure(corr, top_n)
    fig.savefig(out_path, dpi=150, bbox_inches='tight')
    plt.close(fig)


def save_mpt_summary(metrics: dict, ef: dict, high_corr_pairs: list, out_path: str) -> None:
    """Small JSON summary of the MPT/efficient-frontier numbers, for
    panel/positions_routes.py to render without needing pandas/numpy."""
    summary = {
        'n_symbols': len(metrics['symbols']),
        'current': {'return': metrics['p_ret'], 'vol': metrics['p_vol'], 'sharpe': metrics['p_sr']},
        'max_sharpe': ef['max_sharpe'],
        'min_var': ef['min_var'],
        'hhi': metrics['hhi'],
        'effective_n': metrics['effective_n'],
        'rf_annual': metrics['rf_annual'],
        'high_correlation_pairs': [
            {'symbol_a': s1, 'symbol_b': s2, 'correlation': round(float(c), 2)}
            for s1, s2, c in high_corr_pairs
        ],
    }
    if 'port_beta' in metrics:
        summary['port_beta'] = metrics['port_beta']

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, 'w') as f:
        json.dump(summary, f, indent=2)

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

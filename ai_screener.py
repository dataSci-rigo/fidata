"""Natural-language query -> discovery-screener filter params, via one forced
tool-use call. "reduce my vol" only means something relative to YOUR portfolio,
so the prompt carries a compact context block assembled from the pipeline's
on-disk artifacts (mpt_summary.json, combined.json, sectors.json) plus the
universe's own value ranges — never a recompute, never a network fetch.

Unlike ai_review.py the client is created lazily: a missing ANTHROPIC_API_KEY
must degrade the /discover page's AI box, not crash the whole local server at
import time. The model's output is only ever a *suggestion* — it goes through
discover_filters.validate_params and then the same deterministic
apply_filters as a hand-filled form.
"""
import json
import os

from discover_filters import tool_input_schema, validate_params

_MODEL = os.getenv('FIDATA_COACH_MODEL', 'claude-sonnet-4-6')

_SYSTEM = """You translate a user's investing intent into screener filter
parameters for their personal discovery screener. You are given portfolio
context and the universe's value ranges; the user query follows.

Rules:
- Set ONLY parameters the request implies; leave everything else unset.
- Resolve relative goals against the given numbers: "reduce my volatility"
  means ann_vol_max meaningfully below the portfolio's annualized vol (and
  consider Bond/low-beta asset classes); "uncorrelated to my portfolio" means
  corr_portfolio_max around 0.3 or lower; a theme like "AI exposure" means
  text_any phrases plus the matching sector.
- Use fractions where the schema says fractions (0.20 = 20% vol) and percent
  numbers where it says percent (gain_1yr 10 = +10%).
- Never invent numbers the context does not support; prefer leaving a bound
  unset over guessing.
- rationale: one or two sentences referencing the context numbers you used."""


class AiScreenerError(RuntimeError):
    pass


def _load_json(path: str):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def build_context(app_data_dir: str, data_dir: str, universe_df=None) -> str:
    """Compact plain-text context block. Every artifact is optional — a
    missing file becomes an 'unknown' line, not an error."""
    lines = ['PORTFOLIO CONTEXT:']

    mpt = _load_json(os.path.join(data_dir, 'mpt_summary.json')) or {}
    cur = mpt.get('current') or {}
    if cur:
        lines.append(
            f"- portfolio annualized return {cur.get('return', 0):.1%}, "
            f"vol {cur.get('vol', 0):.1%}, sharpe {cur.get('sharpe', 0):.2f}")
    else:
        lines.append('- portfolio risk/return: unknown (pipeline has not run)')
    if mpt.get('port_beta') is not None:
        lines.append(f"- portfolio beta vs SPY {mpt['port_beta']:.2f}")
    if mpt.get('effective_n') is not None:
        lines.append(f"- effective number of positions {mpt['effective_n']:.1f} "
                     f"(HHI {mpt.get('hhi', 0):.0f})")
    if mpt.get('rf_annual') is not None:
        lines.append(f"- risk-free rate {mpt['rf_annual']:.2%}")

    combined = _load_json(os.path.join(app_data_dir, 'combined.json')) or []
    rows = [r for r in combined if r.get('Symbol') and r['Symbol'] != 'cash']
    if rows:
        total = sum(float(r.get('Market_Value') or 0) for r in rows) or 1.0
        top = sorted(rows, key=lambda r: -(float(r.get('Market_Value') or 0)))[:10]
        lines.append('- top holdings: ' + ', '.join(
            f"{r['Symbol']} {float(r.get('Market_Value') or 0) / total:.0%}"
            for r in top))

    sectors = _load_json(os.path.join(app_data_dir, 'sectors.json')) or {}
    gics = sectors.get('by_gics') or []
    if gics:
        lines.append('- sector weights (market value): ' + ', '.join(
            f"{s.get('Sector')}: {s.get('Total_Market_Value')}" for s in gics[:8]))

    if universe_df is not None and len(universe_df):
        lines.append('')
        lines.append('UNIVERSE CONTEXT:')
        if 'Sector' in universe_df.columns:
            secs = sorted(x for x in universe_df['Sector'].dropna().unique() if x)
            lines.append('- available sectors: ' + ', '.join(secs))
        if 'Asset_Class' in universe_df.columns:
            counts = universe_df['Asset_Class'].value_counts()
            lines.append('- asset classes: ' + ', '.join(
                f'{k} ({v})' for k, v in counts.items()))
        for col, label, fmt in (('Ann_Vol', 'ann_vol', '{:.2f}'),
                                ('MarketCap', 'market_cap', '{:.0f}'),
                                ('Dividend_Yield', 'dividend_yield', '{:.3f}'),
                                ('Corr_Portfolio', 'corr_portfolio', '{:.2f}')):
            if col in universe_df.columns:
                vals = universe_df[col].dropna()
                if len(vals):
                    lines.append(
                        f'- {label} range: min {fmt.format(vals.min())}, '
                        f'median {fmt.format(vals.median())}, '
                        f'max {fmt.format(vals.max())}')
    return '\n'.join(lines)


def query_to_filters(query: str, context: str) -> dict:
    """One forced tool-use call -> {'params': dict, 'rationale': str,
    'warnings': list}. Raises AiScreenerError on any API/setup problem so the
    route can 503 while the page keeps working manually."""
    try:
        import anthropic
        client = anthropic.Anthropic()
    except Exception as e:
        raise AiScreenerError(f'anthropic client unavailable: {e}') from e

    try:
        resp = client.messages.create(
            model=_MODEL, max_tokens=1024, system=_SYSTEM,
            messages=[{'role': 'user',
                       'content': f'{context}\n\nUSER QUERY: {query}'}],
            tools=[{'name': 'set_screener_filters',
                    'description': 'Set the discovery screener filter parameters '
                                   'that express the user\'s intent.',
                    'input_schema': tool_input_schema()}],
            tool_choice={'type': 'tool', 'name': 'set_screener_filters'},
        )
    except Exception as e:
        raise AiScreenerError(f'AI request failed: {e}') from e

    block = next((b for b in resp.content if getattr(b, 'type', '') == 'tool_use'),
                 None)
    if block is None:
        raise AiScreenerError('AI returned no tool_use block')
    raw = dict(block.input or {})
    rationale = str(raw.pop('rationale', '') or '')
    params, warnings = validate_params(raw)
    return {'params': params, 'rationale': rationale, 'warnings': warnings}

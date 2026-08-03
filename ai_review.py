"""Claude API portfolio reviews: a short daily summary and a deeper
structured weekly review (rebalancing / sector drift / tax-loss harvesting /
watch list). Mirrors todo_list/franklin/coach.py's client pattern.

Delivery is handled by the callers (run_pipeline.py-style scripts): daily
summary goes to Telegram only; the weekly review gets a short Telegram
digest plus the full structured text written to disk for panel/fidata_routes.py
to render.
"""
import os

import anthropic
import pandas as pd

_client = anthropic.Anthropic()
_MODEL = os.getenv('FIDATA_COACH_MODEL', 'claude-sonnet-4-6')


class ReviewError(Exception):
    pass


def _ask(system: str, user_message: str, max_tokens: int) -> str:
    try:
        response = _client.messages.create(
            model=_MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{'role': 'user', 'content': user_message}],
        )
        return response.content[0].text
    except Exception as e:
        raise ReviewError(str(e)) from e


def daily_summary(combined: pd.DataFrame, earn_cache: pd.DataFrame,
                   alerts_today: list[str]) -> str:
    """Short Telegram-ready summary: notable moves + upcoming earnings.
    Deliberately terse — this runs once a day and should read in 10 seconds."""
    eq = combined[combined.index != 'cash'].copy()

    lines = ['Today\'s portfolio snapshot:', '']
    if 'Gain_3m' in eq.columns:
        movers = eq[['Current_Price', 'Market_Value', 'Gain_3m']].dropna(subset=['Gain_3m'])
        top = movers.nlargest(5, 'Gain_3m')
        bottom = movers.nsmallest(5, 'Gain_3m')
        lines.append('Top 3-month movers:')
        for sym, row in top.iterrows():
            lines.append(f'  {sym}: {row["Gain_3m"]:+.1f}%')
        lines.append('Worst 3-month movers:')
        for sym, row in bottom.iterrows():
            lines.append(f'  {sym}: {row["Gain_3m"]:+.1f}%')
        lines.append('')

    today = pd.Timestamp.now().normalize()
    upcoming = earn_cache[
        earn_cache.index.isin(eq.index) &
        earn_cache['Next_Earnings'].notna() &
        (earn_cache['Next_Earnings'] >= today) &
        (earn_cache['Next_Earnings'] <= today + pd.Timedelta(days=7))
    ].sort_values('Next_Earnings')
    if not upcoming.empty:
        lines.append('Earnings in the next 7 days:')
        for sym, row in upcoming.iterrows():
            lines.append(f'  {sym}: {pd.Timestamp(row["Next_Earnings"]).date()}')
        lines.append('')

    if alerts_today:
        lines.append('Alerts already sent today:')
        lines.extend(f'  {m}' for m in alerts_today)

    user_message = '\n'.join(lines)
    system = (
        'You are a terse portfolio assistant. Summarize the data below in '
        '3-5 short bullet points for a Telegram message. No preamble, no '
        'markdown headers, no disclaimers about not being financial advice.'
    )
    return _ask(system, user_message, max_tokens=400)


def weekly_deep_review(combined: pd.DataFrame, sector_data: dict,
                        mpt_metrics: dict, sold_df: pd.DataFrame) -> dict[str, str]:
    """Structured weekly review. Returns {section_name: text} so callers
    (Telegram digest, panel dashboard) can render each section separately —
    the full text easily exceeds Telegram's 4096-char message limit."""
    eq = combined[combined.index != 'cash'].copy()

    lines = ['Current holdings (Symbol, Market_Value, Return_%, Cost_Basis_Source):']
    cols = [c for c in ['Market_Value', 'Avg_Buy_Price', 'Current_Price', 'Cost_Basis_Source']
            if c in eq.columns]
    for sym, row in eq[cols].iterrows():
        ret = ''
        if 'Avg_Buy_Price' in row and pd.notna(row.get('Avg_Buy_Price')) and row['Avg_Buy_Price']:
            ret = f", Return: {(row['Current_Price']/row['Avg_Buy_Price']-1)*100:.1f}%"
        lines.append(f'  {sym}: MV=${row.get("Market_Value", 0):,.0f}{ret}, '
                      f'source={row.get("Cost_Basis_Source", "?")}')

    lines.append('')
    lines.append('Sector allocation (by GICS):')
    for row in sector_data.get('by_gics', []):
        lines.append(f'  {row}')

    lines.append('')
    lines.append('MPT metrics:')
    lines.append(f'  Current: return={mpt_metrics.get("p_ret", 0):.2%}, '
                  f'vol={mpt_metrics.get("p_vol", 0):.2%}, sharpe={mpt_metrics.get("p_sr", 0):.2f}')
    lines.append(f'  HHI={mpt_metrics.get("hhi", 0):.0f}, Effective-N={mpt_metrics.get("effective_n", 0):.1f}')

    tlh_candidates = eq[(eq.get('Cost_Basis_Source') != 'default_cutoff')].copy()
    if 'Avg_Buy_Price' in tlh_candidates.columns:
        tlh_candidates['Return_%'] = (tlh_candidates['Current_Price'] / tlh_candidates['Avg_Buy_Price'] - 1) * 100
        losers = tlh_candidates[tlh_candidates['Return_%'] < 0].sort_values('Return_%')
        if not losers.empty:
            lines.append('')
            lines.append('Unrealized losses (candidates for tax-loss harvesting review — '
                          'excludes rows with Cost_Basis_Source=default_cutoff, since those '
                          'cost bases are unreliable):')
            for sym, row in losers.iterrows():
                lines.append(f'  {sym}: {row["Return_%"]:.1f}%')

    if not sold_df.empty:
        lines.append('')
        lines.append(f'Realized gains/losses this year: ${sold_df["Gain_Loss"].sum():,.0f} '
                      f'across {len(sold_df)} closed lots.')

    user_message = '\n'.join(lines)
    system = (
        'You are a portfolio analyst producing a weekly deep review. Write four '
        'clearly headed sections using EXACTLY these headers on their own line: '
        '"Rebalancing:", "Sector Drift:", "Tax-Loss Harvesting:", "Watch List:". '
        'Under each header, write 2-4 short paragraphs or bullet points grounded '
        'in the data given — do not invent numbers not present in the input. '
        'No disclaimers about not being financial advice.'
    )
    text = _ask(system, user_message, max_tokens=2000)

    sections = {'Rebalancing': '', 'Sector Drift': '', 'Tax-Loss Harvesting': '', 'Watch List': ''}
    current = None
    for line in text.splitlines():
        stripped = line.strip().rstrip(':')
        if stripped in sections:
            current = stripped
            continue
        if current:
            sections[current] += line + '\n'
    for k in sections:
        sections[k] = sections[k].strip()
    return sections

"""Which accounts are taxable, what is harvestable, and what a sale costs.

The pipeline merges all ten accounts into one `combined` frame before anything
downstream sees them, so tax treatment was invisible: the weekly review was
recommending tax-loss harvesting on positions sitting in IRAs, where a
realized loss does nothing at all. ~44% of the book is tax-advantaged.

Account status comes from `ACC_TAXABLE` in .env (comma-separated account keys,
matching the `ACC_<key>` name map). Anything not listed is treated as
tax-advantaged ONLY if it is listed in `ACC_TAX_ADVANTAGED`; an account in
neither list is 'unknown' and deliberately excluded from harvest figures — a
silent default is how the original mistake happened.

Account types, read from the broker exports themselves (2026-09):
  137  Schwab   Designated_Bene_Individual   taxable
  472  Schwab   Designated_Bene_Individual   taxable
  4919 E*Trade  money and investments        taxable
  133  Schwab   Contributory IRA             tax-advantaged
  370  Schwab   Rollover IRA                 tax-advantaged
  618  Schwab   Rollover IRA                 tax-advantaged
  8197 Fidelity ROTH IRA                     tax-advantaged
  9828 Fidelity Rollover IRA                 tax-advantaged
  1297 E*Trade  Traditional IRA              tax-advantaged
  898  E*Trade  XRA580898 (Empower rollover) tax-advantaged

The rate schedules at the bottom exist because "the 0% federal bracket makes
this free" is only true federally. California taxes long-term gain as ordinary
income, so a plan that reports federal headroom and nothing else understates
the cost of every sale in it.
"""
import os

import pandas as pd

TAXABLE = 'taxable'
TAX_ADVANTAGED = 'tax_advantaged'
UNKNOWN = 'unknown'


def _keys(env_var: str, env: dict | None = None) -> list[str]:
    raw = (env or os.environ).get(env_var) or ''
    return [k.strip().strip("'\"") for k in str(raw).split(',') if k.strip()]


def account_status(env: dict | None = None) -> dict[str, str]:
    """{account key: taxable | tax_advantaged}. Keys absent from both lists
    resolve to 'unknown' via status_of()."""
    out = {k: TAXABLE for k in _keys('ACC_TAXABLE', env)}
    for k in _keys('ACC_TAX_ADVANTAGED', env):
        out.setdefault(k, TAX_ADVANTAGED)
    return out


def status_of(acct: str, env: dict | None = None) -> str:
    return account_status(env).get(str(acct), UNKNOWN)


def taxable_accounts(env: dict | None = None) -> frozenset[str]:
    return frozenset(k for k, v in account_status(env).items() if v == TAXABLE)


def account_summary(accounts: dict, env: dict | None = None) -> list[dict]:
    """Per-account market value and tax status, for reporting."""
    status = account_status(env)
    out = []
    for acct, df in accounts.items():
        out.append({
            'account': str(acct),
            'status': status.get(str(acct), UNKNOWN),
            'market_value': round(float(
                pd.to_numeric(df['Market_Value'], errors='coerce').sum()), 2),
            'positions': int((df.index != 'cash').sum()),
        })
    return sorted(out, key=lambda r: -r['market_value'])


def taxable_positions(accounts: dict, env: dict | None = None,
                      prices: pd.Series | None = None) -> pd.DataFrame:
    """Per-symbol position held in TAXABLE accounts only, with the broker's own
    cost basis: Quantity, Market_Value, Cost_Basis, Unrealized, Return_Pct.

    This — not `combined` — is what a tax-loss decision may be based on.

    `prices` (symbol -> current price, e.g. combined['Current_Price']) restates
    market value at today's prices. Without it the value comes from the broker
    export, which can be months old — the share counts and cost basis are
    still right, but an unrealized figure against a stale price is not.
    """
    cols = ['Quantity', 'Market_Value', 'Cost_Basis', 'Unrealized', 'Return_Pct']
    taxable = taxable_accounts(env)
    frames = [df for acct, df in accounts.items() if str(acct) in taxable]
    if not frames:
        return pd.DataFrame(columns=cols, index=pd.Index([], name='Symbol'))

    rows = pd.concat(frames)
    rows.index.name = 'Symbol'
    rows = rows[rows.index != 'cash']
    if 'Cost_Basis' not in rows.columns:
        rows = rows.assign(Cost_Basis=float('nan'))

    # Cost basis must be all-or-nothing per symbol. A plain sum turns an
    # all-NaN basis into 0.0 (making an unknown cost look like a 100% gain),
    # and a partial sum understates cost when only some lots report it —
    # either way the position reads as a phantom gain and a real loss hides.
    def _all_or_nan(s):
        return s.sum() if s.notna().all() and len(s) else float('nan')

    out = rows.groupby('Symbol').agg(Quantity=('Quantity', 'sum'),
                                     Market_Value=('Market_Value', 'sum'),
                                     Cost_Basis=('Cost_Basis', _all_or_nan))
    out = out.rename(index={'BRK/B': 'BRK-B'})
    if prices is not None and len(prices):
        live = pd.to_numeric(pd.Series(prices), errors='coerce').reindex(out.index)
        restated = out['Quantity'] * live
        out['Market_Value'] = restated.where(restated.notna(), out['Market_Value'])
    out['Unrealized'] = (out['Market_Value'] - out['Cost_Basis']).round(2)
    out['Return_Pct'] = ((out['Market_Value'] / out['Cost_Basis'] - 1) * 100).round(2)
    return out[cols].sort_values('Unrealized')


def holding_term(cost_basis_source: str | None, last_buy=None,
                 today=None) -> str:
    """'long' | 'mixed' | 'unknown' for a whole position.

    'long' means every share qualifies for long-term treatment, which is what
    a gain-realization plan needs. Two ways to be sure: the pipeline found no
    transaction for it (`default_cutoff`), meaning it was bought before the
    2023 cutoff; or its most recent BUY is more than a year old. A purchase
    inside the last year makes the position 'mixed' — part of the gain would
    be short-term and taxed as ordinary income.
    """
    today = pd.Timestamp(today) if today is not None else pd.Timestamp.now()
    if str(cost_basis_source) == 'default_cutoff':
        return 'long'
    if last_buy is None or pd.isna(last_buy):
        return 'unknown'
    return 'long' if (today - pd.Timestamp(last_buy)).days > 365 else 'mixed'


def gain_harvest_plan(positions: pd.DataFrame, order: list[str],
                      budget: float, terms: dict | None = None) -> dict:
    """Walk `order` (symbols, best-to-sell first) accumulating realized gain
    until `budget` is used up.

    In a low-income year the 0% long-term bracket makes realizing gains cheap,
    so the question flips from "which losses can I harvest" to "which
    positions would I exit anyway, and how much gain does that realize".
    Losses encountered along the way reduce the running total, which is
    correct — they net against gains in the same year.

    Only 'long' positions are included; a 'mixed' one is listed separately so
    a short-term slice can't quietly enter the plan.
    """
    terms = terms or {}
    taken, skipped = [], []
    realized = freed = 0.0
    for sym in order:
        if sym not in positions.index:
            continue
        row = positions.loc[sym]
        gain, mv = row.get('Unrealized'), row.get('Market_Value')
        if pd.isna(gain) or pd.isna(mv):
            continue
        term = terms.get(sym, 'unknown')
        entry = {'symbol': str(sym), 'market_value': round(float(mv), 0),
                 'gain': round(float(gain), 0), 'term': term}
        if term != 'long':
            skipped.append(entry)
            continue
        if realized + float(gain) > budget and taken:
            entry['reason'] = 'would exceed the budget'
            skipped.append(entry)
            continue
        realized += float(gain)
        freed += float(mv)
        taken.append({**entry, 'cumulative_gain': round(realized, 0),
                      'cumulative_freed': round(freed, 0)})
    plan = {'budget': round(float(budget), 0),
            'gain_realized': round(realized, 0),
            'capital_freed': round(freed, 0),
            'headroom': round(float(budget) - realized, 0),
            'sell': taken, 'excluded': skipped}
    # One large holding can swallow the whole budget and crowd out a dozen
    # cheap cleanups. Say so rather than presenting the list as a single
    # take-it-or-leave-it plan.
    gains = [s for s in taken if s['gain'] > 0]
    # Needs at least three sales to mean anything: with two, one of them
    # always holds more than half the gain.
    if len(taken) >= 3 and gains and realized > 0:
        biggest = max(gains, key=lambda s: s['gain'])
        share = biggest['gain'] / realized * 100
        if share >= 50:
            plan['dominated_by'] = {
                'symbol': biggest['symbol'], 'gain': biggest['gain'],
                'share_pct': round(share, 1),
                'without_it': {
                    'gain_realized': round(realized - biggest['gain'], 0),
                    'capital_freed': round(freed - biggest['market_value'], 0),
                    'positions': len(taken) - 1}}
    return plan


def harvest_candidates(accounts: dict, env: dict | None = None,
                       min_loss: float = 250.0,
                       prices: pd.Series | None = None) -> list[dict]:
    """Losses that can actually be harvested: taxable accounts only, broker
    cost basis present, loss past `min_loss` (below which the paperwork costs
    more than the deduction saves)."""
    pos = taxable_positions(accounts, env, prices)
    if pos.empty:
        return []
    losers = pos[pos['Unrealized'].notna() & (pos['Unrealized'] <= -min_loss)]
    return [{'symbol': str(sym),
             'market_value': round(float(r['Market_Value']), 2),
             'cost_basis': round(float(r['Cost_Basis']), 2),
             'unrealized': round(float(r['Unrealized']), 2),
             'return_pct': None if pd.isna(r['Return_Pct']) else float(r['Return_Pct']),
             'quantity': round(float(r['Quantity']), 4)}
            for sym, r in losers.iterrows()]


# ── Rate schedules ──────────────────────────────────────────────────────────
# Every figure below was verified against the primary source named beside it.
# The tax YEAR is part of each constant's meaning: the federal table is TY2026
# and the California table is TY2025, because those are the latest published.
# Mixing years is the most common way to get these wrong by a bracket.

FILING_STATUSES = ('single', 'mfj', 'hoh', 'mfs')


def _norm_filing(filing: str) -> str:
    """Normalise a filing status, or raise. Silently falling back to 'single'
    is how a head-of-household return would get a single filer's brackets."""
    f = str(filing).strip().lower()
    alias = {'joint': 'mfj', 'married': 'mfj', 'married_joint': 'mfj',
             'head_of_household': 'hoh', 'married_separate': 'mfs',
             'separate': 'mfs'}
    f = alias.get(f, f)
    if f not in FILING_STATUSES:
        raise ValueError(f'unknown filing status {filing!r}; '
                         f'expected one of {FILING_STATUSES}')
    return f


# California has NO preferential capital-gains rate. R&TC 17041(a)(1) taxes
# "the entire taxable income" on one graduated schedule, and California never
# incorporates IRC Subchapter A (where the federal 0/15/20% regime in §1(h)
# lives) — it incorporates Subchapters O and P only, via R&TC 18031/18151. So
# the federal 0% bracket never makes a sale free here, only cheap.
#
# FTB Schedule X, single filer, TAX YEAR 2025: (top of bracket, rate).
# TY2026 California figures do not exist yet — the FTB publishes indexed
# amounts around October and full schedules in late December, and the 2026
# Form 540-ES worksheet directs taxpayers to use the 2025 table as the proxy.
# Do not guess a CCPI factor; update when FTB publishes.
CA_TAX_YEAR = 2025
CA_BRACKETS = ((11_079, 0.01), (26_264, 0.02), (41_452, 0.04), (57_542, 0.06),
               (72_724, 0.08), (371_479, 0.093), (445_771, 0.103),
               (742_953, 0.113), (float('inf'), 0.123))
CA_STANDARD_DEDUCTION = 5_706.0
CA_EXEMPTION_CREDIT = 153.0      # a credit, not a deduction; never below zero
# R&TC 17043 (Prop 63). Renamed the Behavioral Health Services Tax for tax
# years beginning on or after 2025-01-01. The statute switches off both
# R&TC 17041 indexing and R&TC 17045 joint doubling, so $1M is flat for every
# filing status, permanently, and applies to TAXABLE income.
CA_BHST_THRESHOLD = 1_000_000.0
CA_MHST_THRESHOLD = CA_BHST_THRESHOLD      # back-compat alias

# R&TC 17045: joint tax is "twice the tax which would be imposed if the taxable
# income were cut in one-half", so every MFJ edge is exactly double — verified
# at all eight edges, not an approximation. MFS uses the single schedule.
# Head of household has its own Schedule Z, which is NOT a multiple of Schedule
# X; rather than invent it, refuse.
CA_SCHEDULE_MULTIPLIER = {'single': 1.0, 'mfs': 1.0, 'mfj': 2.0}

# Federal long-term capital gain, TAX YEAR 2026 — Rev. Proc. 2025-32 §4.03
# (IRC §1(h), §1(j)(5)). Measured against TAXABLE income, so the standard
# deduction sits underneath the 0% ceiling. Both boundaries are marginal, not
# cliffs: §1(h)(1)(C)(i) taxes only the excess over the lower band.
FED_TAX_YEAR = 2026
FED_LTCG_0_TOP = {'single': 49_450.0, 'mfj': 98_900.0,
                  'hoh': 66_200.0, 'mfs': 49_450.0}
FED_LTCG_15_TOP = {'single': 545_500.0, 'mfj': 613_700.0,
                   'hoh': 579_600.0, 'mfs': 306_850.0}
# Rev. Proc. 2025-32 §4.14(1) (IRC §63(c)(2)). This is a FLOOR: §63(f) adds
# $1,650 for aged/blind ($2,050 if unmarried), and post-OBBBA §63(b) has seven
# further subtractions from AGI. Pass `deduction` to model any of them.
FED_STANDARD_DEDUCTION = {'single': 16_100.0, 'mfj': 32_200.0,
                          'hoh': 24_150.0, 'mfs': 16_100.0}
# IRC §1411 net investment income tax. Capital gain is net investment income
# per §1411(c)(1)(A)(iii). The thresholds run on MAGI, NOT taxable income, and
# §1411 contains no inflation-adjustment provision — they have been frozen
# since 2013 and will not move.
NIIT_RATE = 0.038
NIIT_THRESHOLD = {'single': 200_000.0, 'mfj': 250_000.0,
                  'hoh': 200_000.0, 'mfs': 125_000.0}


def _ca_multiplier(filing: str) -> float:
    f = _norm_filing(filing)
    if f not in CA_SCHEDULE_MULTIPLIER:
        raise ValueError(
            f"California {f!r} uses FTB Schedule Z, which is not a multiple of "
            f"Schedule X and is not encoded here — refusing rather than "
            f"returning a single filer's brackets.")
    return CA_SCHEDULE_MULTIPLIER[f]


def ca_tax(income: float, filing: str = 'single',
           deduction: float | None = None, credit: float | None = None) -> float:
    """California tax on `income` — gross/AGI, not taxable income.

    Capital gain enters as ordinary income because that is how California
    treats it. Pass `deduction` to override the standard deduction.
    """
    mult = _ca_multiplier(filing)
    ded = CA_STANDARD_DEDUCTION * mult if deduction is None else float(deduction)
    cred = CA_EXEMPTION_CREDIT * mult if credit is None else float(credit)
    taxable = max(0.0, float(income) - ded)
    tax = 0.0
    lo = 0.0
    for edge, rate in CA_BRACKETS:
        if taxable <= lo:
            break
        hi = edge * mult
        tax += (min(taxable, hi) - lo) * rate
        lo = hi
    if taxable > CA_BHST_THRESHOLD:
        tax += (taxable - CA_BHST_THRESHOLD) * 0.01
    return max(0.0, tax - cred)


def ca_marginal_rate(income: float, filing: str = 'single',
                     deduction: float | None = None) -> float:
    """Rate the next dollar of gain would pay, as a fraction."""
    mult = _ca_multiplier(filing)
    ded = CA_STANDARD_DEDUCTION * mult if deduction is None else float(deduction)
    taxable = max(0.0, float(income) - ded)
    extra = 0.01 if taxable > CA_BHST_THRESHOLD else 0.0
    for edge, rate in CA_BRACKETS:
        if taxable <= edge * mult:
            return rate + extra
    return CA_BRACKETS[-1][1] + extra


def federal_0pct_capacity(other_income: float = 0.0, filing: str = 'single',
                          deduction: float | None = None) -> float:
    """Long-term gain that still fits inside the federal 0% bracket.

    Ordinary income fills the 0% band first (IRC §1(h)(1)(B)(ii) subtracts the
    gain out before measuring the room), and the deduction offsets that
    ordinary income — so capacity is the 0% ceiling plus the deduction, less
    other income. The gain does NOT consume its own capacity.

    This is a GROSS-income capacity, not a taxable-income threshold: the full
    figure only holds when the deduction is otherwise unused.
    """
    f = _norm_filing(filing)
    ded = FED_STANDARD_DEDUCTION[f] if deduction is None else float(deduction)
    return max(0.0, FED_LTCG_0_TOP[f] + ded - max(0.0, float(other_income)))


def niit(gain: float, other_income: float = 0.0, filing: str = 'single') -> float:
    """IRC §1411 net investment income tax: 3.8% of the LESSER of net
    investment income and the excess of MAGI over the threshold.

    Runs on MAGI, not taxable income, so it bites at a different — and much
    lower — point than the 20% capital-gains bracket. The thresholds are not
    indexed and have not moved since 2013.
    """
    f = _norm_filing(filing)
    magi = max(0.0, float(other_income)) + float(gain)
    excess = max(0.0, magi - NIIT_THRESHOLD[f])
    return NIIT_RATE * min(max(0.0, float(gain)), excess)


def federal_tax_on_gain(gain: float, other_income: float = 0.0,
                        filing: str = 'single',
                        deduction: float | None = None) -> dict:
    """Federal tax attributable to realizing `gain` of LONG-TERM capital gain.

    Long-term gain stacks on top of ordinary income, and each boundary is
    marginal rather than a cliff — one dollar over the 0% ceiling costs
    fifteen cents, not 15% of the whole gain.
    """
    f = _norm_filing(filing)
    gain = float(gain)
    ded = FED_STANDARD_DEDUCTION[f] if deduction is None else float(deduction)
    ordinary_taxable = max(0.0, float(other_income) - ded)
    taxable = max(0.0, float(other_income) + gain - ded)
    gain_taxable = max(0.0, taxable - ordinary_taxable)

    at_0 = max(0.0, min(gain_taxable, FED_LTCG_0_TOP[f] - ordinary_taxable))
    at_20 = max(0.0, min(gain_taxable - at_0,
                         taxable - max(FED_LTCG_15_TOP[f], ordinary_taxable)))
    at_15 = max(0.0, gain_taxable - at_0 - at_20)
    ltcg = 0.15 * at_15 + 0.20 * at_20
    surtax = niit(gain, other_income, filing)
    return {
        'ltcg_tax': round(ltcg, 0),
        'niit': round(surtax, 0),
        'federal_tax_on_gain': round(ltcg + surtax, 0),
        'taxed_at_0': round(at_0, 0),
        'taxed_at_15': round(at_15, 0),
        'taxed_at_20': round(at_20, 0),
        'effective_pct': round((ltcg + surtax) / gain * 100, 2) if gain else 0.0,
    }


def combined_tax_on_gain(gain: float, other_income: float = 0.0,
                         filing: str = 'single') -> dict:
    """Federal (including NIIT) plus California, on one realized long-term gain.

    This is the number a sell decision actually turns on. Reporting the federal
    0% bracket alone understates it by the whole state bill; reporting state
    alone misses the 15% and the 3.8% surtax entirely.
    """
    fed = federal_tax_on_gain(gain, other_income, filing)
    st = state_tax_on_gain(gain, other_income, filing)
    total = fed['federal_tax_on_gain'] + st['state_tax_on_gain']
    return {
        'gain': round(float(gain), 0),
        'other_income': round(float(other_income), 0),
        'filing': _norm_filing(filing),
        'federal': fed,
        'state': st,
        'total_tax': round(total, 0),
        'effective_pct': round(total / gain * 100, 2) if gain else 0.0,
        'net_proceeds_pct': round(100 - total / gain * 100, 2) if gain else 100.0,
    }


def state_tax_on_gain(gain: float, other_income: float = 0.0,
                      filing: str = 'single') -> dict:
    """What realizing `gain` on top of `other_income` costs in California.

    Computed as the difference between the two tax bills rather than a flat
    rate, because the gain is what walks you up the brackets: the same $50,000
    costs ~2% on its own and ~9% stacked on a salary.
    """
    gain = float(gain)
    base = ca_tax(other_income, filing)
    total = ca_tax(other_income + gain, filing)
    return {
        'gain': round(gain, 0),
        'other_income': round(float(other_income), 0),
        'state': 'CA',
        'tax_without_gain': round(base, 0),
        'tax_with_gain': round(total, 0),
        'state_tax_on_gain': round(total - base, 0),
        'effective_pct': round((total - base) / gain * 100, 2) if gain else 0.0,
        'marginal_pct': round(ca_marginal_rate(other_income + gain, filing) * 100, 2),
        'federal_0pct_capacity': round(
            federal_0pct_capacity(other_income, filing), 0),
        'tax_year': f'CA {CA_TAX_YEAR} schedule, federal {FED_TAX_YEAR}',
        'note': ('California taxes long-term gain as ordinary income — no 0% '
                 'bracket (R&TC 17041(a)(1); CA never incorporates IRC '
                 'Subchapter A, where §1(h) lives). Marginal rate rises with '
                 'other income, so this figure moves if the startup pays '
                 'anything this year.'),
    }

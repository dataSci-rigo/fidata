"""Shared helpers/constants used by every broker-format parser.

Single source of truth for the "non-equity row" and "cash alias" sets that
used to be defined (and drift) separately in the main position loader and in
the snapshot-diff loader.
"""
import re

import pandas as pd

_OCC_RE = re.compile(r'^[A-Z]{1,6}\d{6}[CP]\d+$')

# Rows to drop from any position export — section totals, futures cash lines,
# empty placeholder rows, etc. Union of what the old cell 0 and cell 5 each used.
NON_EQUITY_SYMBOLS = {
    'Futures Cash',
    'Futures Positions Market Value',
    'Positions Total',
    '--',
    'Cash and Money Market',
}

# Symbol -> 'cash' aliases seen across brokers/exports.
CASH_ALIASES = {
    'Cash & Cash Investments': 'cash',
    'SPAXX**': 'cash',
    'PGC': 'cash',
    'USD999997': 'cash',
    'CASH': 'cash',
}


def clean_num(val) -> float:
    """Strip $, commas, % from a value and return float or NaN."""
    if pd.isna(val):
        return float('nan')
    s = str(val).strip().replace('$', '').replace(',', '').replace('%', '')
    try:
        return float(s)
    except ValueError:
        return float('nan')


def is_option(sym) -> bool:
    return bool(_OCC_RE.match(str(sym).strip()))


def account_key(raw) -> str:
    """Normalize a broker account number to the short key used everywhere.

    Every parser has to agree on this or nothing joins. The trap: pandas types
    Fidelity's numeric `Account Number` column as float64, so the naive
    `str(raw)[-4:]` used to yield '28.0' for 236369828 — which matched no
    position account, silently duplicating transaction rows and making
    analytics.infer_missing_trades re-invent trades it already had.

    Coerce numeric-like values through int first, keep only digits, take the
    last 4, then drop leading zeros. That last step is deliberate: it
    reproduces what parsers/fidelity.py already produced (str(int(...))), so
    app_data/accounts.json keys and the ACC_* name map in .env keep working.
    """
    s = str(raw).strip()
    try:
        s = str(int(float(s)))
    except (TypeError, ValueError):
        pass
    digits = ''.join(ch for ch in s if ch.isdigit())
    if not digits:
        return str(raw).strip()
    return str(int(digits[-4:]))

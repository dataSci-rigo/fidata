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

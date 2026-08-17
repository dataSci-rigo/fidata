"""Broker position-export parsers.

`load_positions()` (full loader, replaces notebook cell 0) and
`load_snapshot()` (historical-snapshot loader, replaces cell 5's
`_load_snapshot()`) both dispatch through `detect_format()` and call the
exact same per-broker parse functions in schwab.py / fidelity.py / etrade.py
— eliminating the duplicated/diverging parsing logic that used to live in
both cells independently.
"""
import os
import re

import pandas as pd

from . import etrade, fidelity, schwab
from .common import clean_num, is_option  # re-exported for convenience

__all__ = ['detect_format', 'load_positions', 'load_snapshot', 'parse_file',
           'clean_num', 'is_option']

_MONTH_MAP = {'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
              'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12}


def detect_format(filepath: str) -> str:
    """Sniff a position-export file and return one of:
    'schwab_all', 'schwab_single', 'fidelity_xlsx', 'etrade', 'fidelity_csv',
    'realized_gain', 'unknown'.
    """
    if filepath.endswith('.xlsx'):
        return 'fidelity_xlsx'
    if not filepath.endswith('.csv'):
        return 'unknown'

    with open(filepath, 'r', encoding='utf-8-sig') as f:
        first_line = f.readline().strip()

    # Not a position export, but they get downloaded into accounts/ too.
    # Naming it means load_positions can skip it quietly instead of crying
    # "unrecognised format" three times a run; transactions.load_realized_lots
    # is what actually reads these.
    if first_line.strip('"').startswith('Realized Gain/Loss'):
        return 'realized_gain'

    fmt = schwab.detect(first_line)
    if fmt:
        return fmt
    if etrade.detect(first_line):
        return 'etrade'

    try:
        df_head = pd.read_csv(filepath, nrows=1)
        if fidelity.detect_csv(df_head.columns):
            return 'fidelity_csv'
    except Exception:
        pass
    return 'unknown'


def _date_from_filename(fn: str) -> pd.Timestamp | None:
    m = re.search(r'([A-Za-z]{3})-(\d{2})-(\d{4})', fn)
    if m:
        mon = _MONTH_MAP.get(m.group(1).lower())
        if mon:
            return pd.Timestamp(int(m.group(3)), mon, int(m.group(2)))
    m = re.search(r'(\d{4})-(\d{2})-(\d{2})', fn)
    if m:
        return pd.Timestamp(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def parse_file(filepath: str, data_dir: str | None = None,
                quiet: bool = False) -> dict[str, pd.DataFrame]:
    """Parse ONE position export -> {acct_suffix: DataFrame}.

    The single dispatch point from detected format to per-broker parser.
    load_positions() loops over it, and splits.export_dates() reuses it to read
    undated exports (load_snapshot can't: it bails out whenever the filename
    carries no date, which is exactly the case that needs price-matching).
    Returns {} for non-position files.
    """
    if data_dir is None:
        data_dir = os.path.dirname(os.path.dirname(os.path.abspath(filepath)))
    filename = os.path.basename(filepath)
    fmt = detect_format(filepath)
    try:
        if fmt == 'fidelity_xlsx':
            acct_num, df = fidelity.parse_holdings_xlsx(filepath)
            return {acct_num: df}
        if fmt == 'schwab_all':
            return schwab.parse_all_accounts(filepath)
        if fmt == 'schwab_single':
            acct_num, df = schwab.parse_single_account(filepath)
            return {acct_num: df}
        if fmt == 'etrade':
            acct_num, df = etrade.parse_account_summary(filepath, data_dir)
            if acct_num is not None:
                return {acct_num: df}
            if not quiet:
                print(f'WARNING: could not find data header in {filename}')
            return {}
        if fmt == 'fidelity_csv':
            return fidelity.parse_positions_csv(filepath)
        if fmt == 'realized_gain':
            return {}  # read by transactions.load_realized_lots, not here
        if filename.endswith('.csv') and not quiet:
            print(f'WARNING: unrecognised format in {filename}, skipping')
    except Exception as e:
        if not quiet:
            print(f'WARNING: could not parse {filename}: {e}')
    return {}


def load_positions(accounts_dir: str, exclude: set[str] = frozenset()) -> dict[str, pd.DataFrame]:
    """Full position loader — replaces cell 0's loop.

    Returns {acct_suffix: DataFrame(index=Symbol, cols=[Quantity, Current_Price, Market_Value])}.
    """
    accounts: dict[str, pd.DataFrame] = {}
    data_dir = os.path.dirname(accounts_dir)

    # Ordered by export date where the filename carries one, mtime otherwise:
    # a later file replaces an account outright, and mtime alone would let a
    # re-downloaded older export silently override a newer one (copying files
    # between machines rewrites every mtime).
    def _order(f: str):
        d = _date_from_filename(f)
        return (0, d.toordinal()) if d is not None else (1, os.path.getmtime(
            os.path.join(accounts_dir, f)))

    for filename in sorted(os.listdir(accounts_dir), key=_order):
        if filename in exclude:
            continue
        accounts.update(parse_file(os.path.join(accounts_dir, filename), data_dir))

    return accounts


def load_snapshot(filepath: str) -> tuple[dict[str, pd.DataFrame], pd.Timestamp | None]:
    """Historical-snapshot loader — replaces cell 5's `_load_snapshot()`.

    Calls the SAME parse_* functions as load_positions(), then projects each
    account's DataFrame down to [Quantity, Price] for the snapshot-diff use
    case. No separate regex/constant set.
    """
    fn = os.path.basename(filepath)
    snap_date = _date_from_filename(fn)
    if snap_date is None or not filepath.endswith('.csv'):
        return {}, snap_date

    fmt = detect_format(filepath)
    data_dir = os.path.dirname(os.path.dirname(filepath))  # parent of accounts/past dir
    positions: dict[str, pd.DataFrame] = {}

    try:
        if fmt == 'schwab_all':
            positions = schwab.parse_all_accounts(filepath)
        elif fmt == 'schwab_single':
            acct_num, df = schwab.parse_single_account(filepath)
            positions = {acct_num: df}
        elif fmt == 'fidelity_csv':
            positions = fidelity.parse_positions_csv(filepath)
        elif fmt == 'etrade':
            acct_num, df = etrade.parse_account_summary(filepath, data_dir)
            if acct_num is not None:
                positions = {acct_num: df}
    except Exception as e:
        print(f'  snap {fn}: {e}')
        return {}, snap_date

    out = {}
    for acct, df in positions.items():
        sub = df[['Quantity', 'Current_Price']].rename(columns={'Current_Price': 'Price'})
        out[acct] = sub.dropna(subset=['Quantity'])
    return out, snap_date

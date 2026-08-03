"""Schwab/TD position-export parsers.

Both the full position loader (parsers.load_positions) and the snapshot-diff
loader (parsers.load_snapshot) call these same functions — there is exactly
one implementation of the Schwab CSV quirks (quoted section headers,
'...XXXX' account markers, trailing commas), instead of the two
near-duplicates that used to live in notebook cells 0 and 5.
"""
import io
import re

import pandas as pd

from .common import CASH_ALIASES, NON_EQUITY_SYMBOLS, clean_num


def _flush_section(acct: str, lines: list[str]) -> pd.DataFrame | None:
    if not acct or not lines:
        return None
    try:
        block = io.StringIO(''.join(lines))
        df = pd.read_csv(block, header=0)
        df = df.loc[:, df.columns.notna() & (df.columns.str.strip() != '')]
        df.columns = df.columns.str.strip()
        df = df[df['Symbol'].notna() & ~df['Symbol'].isin(NON_EQUITY_SYMBOLS)]
        qty = pd.to_numeric(
            df['Qty (Quantity)'].astype(str).str.replace(',', '', regex=False),
            errors='coerce')
        mv = df['Mkt Val (Market Value)'].apply(clean_num)
        result = pd.DataFrame({
            'Quantity': qty.values,
            'Market_Value': mv.values,
            'Current_Price': (mv / qty).values,
        }, index=df['Symbol'].values)
        result.index.name = 'Symbol'
        result = result.dropna(subset=['Market_Value']).rename(index=CASH_ALIASES)
        return result
    except Exception as e:
        print(f'schwab section {acct} error: {e}')
        return None


def parse_all_accounts(filepath: str) -> dict[str, pd.DataFrame]:
    """Schwab 'Positions for All-Accounts...' combined export.

    Returns {acct_suffix: DataFrame(index=Symbol, cols=[Quantity, Market_Value, Current_Price])}.
    """
    with open(filepath, 'r', encoding='utf-8-sig') as f:
        raw_lines = [line.rstrip(',\n') + '\n' for line in f.readlines()]

    out: dict[str, pd.DataFrame] = {}
    current_acct = None
    section_lines: list[str] = []

    def flush():
        df = _flush_section(current_acct, section_lines)
        if df is not None:
            out[current_acct] = df

    for line in raw_lines[1:]:  # skip the "Positions for All-Accounts" header
        stripped = line.strip().strip('"')
        acct_m = re.search(r'\.\.\.(\w+)', stripped)
        if acct_m and not stripped.startswith('Symbol') and 'Positions' not in stripped:
            flush()
            current_acct = acct_m.group(1)
            section_lines = []
        elif stripped.startswith('"Symbol"') or stripped.startswith('Symbol'):
            section_lines = [line]
        elif stripped and current_acct:
            section_lines.append(line)
    flush()
    return out


def parse_single_account(filepath: str) -> tuple[str, pd.DataFrame]:
    """Schwab/TD 'Positions for account ...' single-account export.

    Returns (acct_suffix, DataFrame(index=Symbol, cols=[Quantity, Market_Value, Current_Price])).
    """
    with open(filepath, 'r', encoding='utf-8-sig') as f:
        first_line = f.readline().strip()

    match = re.search(r'\.\.(\w+)', first_line)
    acct_num = match.group(1) if match else filepath

    df = pd.read_csv(filepath, skiprows=2, header=0)
    df = df.loc[:, df.columns.notna() & (df.columns.str.strip() != '')]
    df.columns = df.columns.str.strip()
    df = df[df['Symbol'].notna() & ~df['Symbol'].isin(NON_EQUITY_SYMBOLS)]

    qty = pd.to_numeric(
        df['Qty (Quantity)'].astype(str).str.replace(',', '', regex=False),
        errors='coerce')
    mv = df['Mkt Val (Market Value)'].apply(clean_num)
    result = pd.DataFrame({
        'Quantity': qty.values,
        'Market_Value': mv.values,
        'Current_Price': (mv / qty).values,
    }, index=df['Symbol'].values)
    result.index.name = 'Symbol'
    result = result.dropna(subset=['Market_Value']).rename(index=CASH_ALIASES)
    return acct_num, result


def detect(first_line: str) -> str | None:
    if first_line.startswith('"Positions for All-Accounts') or first_line.startswith('"Positions for all'):
        return 'schwab_all'
    if first_line.startswith('"Positions for account'):
        return 'schwab_single'
    return None

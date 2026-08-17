"""E*Trade position-export parser ('Account Summary' CSV, PortfolioDownload*.csv)."""
import os
import re

import pandas as pd

from .common import CASH_ALIASES, account_key


def detect(first_line: str) -> bool:
    return first_line.strip('"') == 'Account Summary'


def parse_account_summary(filepath: str, data_dir: str) -> tuple[str | None, pd.DataFrame | None]:
    """E*Trade 'Account Summary' export (PortfolioDownload*.csv).

    Returns (acct_suffix, DataFrame(index=Symbol, cols=[Quantity, Current_Price, Market_Value]))
    or (None, None) if the data header row can't be located.

    `data_dir` is where the scratch `file_clean.csv` (trailing-comma-stripped
    copy) gets written — mirrors the original notebook's behavior.
    """
    with open(filepath, 'r', encoding='utf-8-sig') as f:
        raw = f.readlines()

    match = re.search(r'-(\d+)', raw[2])
    acct_num = account_key(match.group(1)) if match else os.path.basename(filepath)

    header_idx = next(
        (i for i, line in enumerate(raw) if line.strip().startswith('Symbol,Last Price')),
        None)
    if header_idx is None:
        return None, None

    clean_lines = [line.rstrip(',\n') + '\n' for line in raw]
    clean_path = os.path.join(data_dir, 'file_clean.csv')
    with open(clean_path, 'w') as f:
        f.writelines(clean_lines)

    df = pd.read_csv(clean_path, skiprows=header_idx, header=0, skipfooter=5, engine='python')
    df.columns = df.columns.str.strip()
    df = df[df['Symbol'].notna() & ~df['Symbol'].isin(['TOTAL', ''])]

    if 'Quantity' in df.columns:
        df['Quantity'] = pd.to_numeric(df['Quantity'], errors='coerce')
    else:
        df['Quantity'] = pd.to_numeric(df['Qty #'], errors='coerce')
    df['Current_Price'] = pd.to_numeric(df['Last Price $'], errors='coerce')
    df['Market_Value'] = pd.to_numeric(df['Value $'], errors='coerce')

    result = (
        df[['Symbol', 'Quantity', 'Current_Price', 'Market_Value']]
        .dropna(subset=['Market_Value'])
        .set_index('Symbol')
        .rename(index=CASH_ALIASES)
    )
    return acct_num, result

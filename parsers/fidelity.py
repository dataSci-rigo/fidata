"""Fidelity position-export parsers (CSV 'Account Number' column, and xlsx Holdings)."""
import pandas as pd

from .common import CASH_ALIASES, account_key, clean_num


def parse_positions_csv(filepath: str) -> dict[str, pd.DataFrame]:
    """Fidelity 'Portfolio_Positions_*.csv' export (has an 'Account Number' column).

    Returns {acct_suffix: DataFrame(index=Symbol, cols=[Quantity, Market_Value, Current_Price])}.
    """
    with open(filepath, 'r', encoding='utf-8-sig') as f:
        lines = [line.rstrip(',\n') + '\n' for line in f.readlines()]

    import io
    df = pd.read_csv(io.StringIO(''.join(lines)), header=0)
    df.columns = df.columns.str.strip()
    if 'Account Number' not in df.columns:
        return {}

    df['Quantity'] = pd.to_numeric(df['Quantity'], errors='coerce')
    df['Current_Price'] = df['Last Price'].apply(clean_num)
    df['Market_Value'] = df['Current Value'].apply(clean_num)
    df = df[pd.to_numeric(df['Account Number'], errors='coerce').notna()]

    out: dict[str, pd.DataFrame] = {}
    for raw_acct, grp in df.groupby('Account Number'):
        acct_num = account_key(raw_acct)
        result = (
            grp[['Symbol', 'Quantity', 'Market_Value', 'Current_Price']]
            .dropna(subset=['Market_Value'])
            .set_index('Symbol')
            .rename(index=CASH_ALIASES)
        )
        out[acct_num] = result
    return out


def parse_holdings_xlsx(filepath: str) -> tuple[str, pd.DataFrame]:
    """Fidelity 'Holdings.xlsx' export.

    Returns (acct_suffix, DataFrame(index=Security ID, cols=[Quantity, Market_Value, Current_Price])).
    """
    cols = pd.read_excel(filepath, nrows=1, skiprows=11, index_col=0)
    cols = cols.iloc[0].to_list()
    df = pd.read_excel(filepath, header=12, sheet_name=0, index_col=0, names=cols)
    acct_num = account_key(df['Account Number'].dropna().iloc[0])

    df['Quantity'] = pd.to_numeric(df['Quantity'], errors='coerce')
    df['Current_Price'] = df['Price'].apply(clean_num)
    df['Market_Value'] = df['Market Value'].apply(clean_num)

    result = (
        df[['Security ID', 'Quantity', 'Market_Value', 'Current_Price']]
        .dropna(subset=['Market_Value'])
        .set_index('Security ID')
        .rename(index=CASH_ALIASES)
    )
    result.index.name = 'Symbol'
    return acct_num, result


def detect_csv(df_columns) -> bool:
    return 'Account Number' in df_columns

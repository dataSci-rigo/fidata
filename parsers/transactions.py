"""Transaction-history parsers: buy/sell history + realized-gain lot detail.

`_parse_action` now distinguishes 'REINVEST' from 'BUY' (previously both
collapsed to 'BUY', which inflated "capital deployed" — dividend reinvestment
isn't new capital in, even though it does add to cost basis).
"""
import csv
import io
import os
import re

import pandas as pd

from .common import account_key, clean_num, is_option


def _parse_action(action_str) -> str | None:
    s = str(action_str).upper()
    if 'REINVEST' in s:  # matches both 'REINVESTED' and 'REINVESTMENT'
        return 'REINVEST'
    if 'BOUGHT' in s:
        return 'BUY'
    if 'SOLD' in s:
        return 'SELL'
    return None


def parse_history_csv(filepath: str) -> list[dict]:
    """Fidelity/generic 'Run Date' transaction-history CSV. Returns raw tx-row dicts."""
    fn = os.path.basename(filepath)
    with open(filepath, 'r', encoding='utf-8-sig') as f:
        lines = f.readlines()
    first = lines[0].strip() if lines else ''
    if first.startswith('"Realized Gain/Loss') or first == 'Account Summary':
        return []
    hdr_idx = next((i for i, l in enumerate(lines) if 'Run Date' in l), None)
    if hdr_idx is None:
        return []
    clean = [l.rstrip(',\n') + '\n' for l in lines]
    try:
        df = pd.read_csv(io.StringIO(''.join(clean[hdr_idx:])), header=0)
    except Exception as e:
        print(f'history skip {fn}: {e}')
        return []
    df.columns = df.columns.str.strip()
    multi_acct = 'Account Number' in df.columns

    rows = []
    for _, row in df.iterrows():
        sym = str(row.get('Symbol', '')).strip()
        if not sym or sym == 'nan' or is_option(sym):
            continue
        action = _parse_action(row.get('Action', ''))
        if not action:
            continue
        dt = pd.to_datetime(row.get('Run Date'), errors='coerce')
        if pd.isna(dt):
            continue
        qty = abs(pd.to_numeric(row.get('Quantity', 0), errors='coerce') or 0)
        price = pd.to_numeric(row.get('Price ($)', None), errors='coerce')
        if multi_acct:
            acct = account_key(row.get('Account Number', ''))
        else:
            m = re.search(r'(\d{9,})', fn)
            acct = account_key(m.group(1)) if m else fn
        rows.append(dict(Symbol=sym, Date=dt, Action=action,
                          Quantity=qty, Price=price, Account=acct))
    return rows


def parse_history_xlsx(filepath: str) -> list[dict]:
    """E*Trade 'History' xlsx format. Returns raw tx-row dicts."""
    fn = os.path.basename(filepath)
    try:
        meta = pd.read_excel(filepath, header=None, nrows=6)
        acct_raw = str(meta.iloc[4, 1]).strip()          # "Account: XRA580898"
        acct_id = re.sub(r'[^A-Z0-9]', '', acct_raw)
        acct = account_key(acct_id)

        df = pd.read_excel(filepath, skiprows=6, header=0)
        df.columns = df.columns.str.strip()
    except Exception as e:
        print(f'xlsx skip {fn}: {e}')
        return []

    rows = []
    for _, row in df.iterrows():
        desc = str(row.get('Activity Description', ''))
        if 'Buy' in desc:
            action = 'BUY'
        elif 'Sell' in desc:
            action = 'SELL'
        else:
            continue

        sym = str(row.get('Security ID', '')).strip()
        if not sym or sym == 'nan' or is_option(sym):
            continue

        dt = pd.to_datetime(row.get('Trade Date') or row.get('Date'), errors='coerce')
        if pd.isna(dt):
            continue

        qty = abs(pd.to_numeric(row.get('Quantity', 0), errors='coerce') or 0)
        price = pd.to_numeric(row.get('Price', None), errors='coerce')
        rows.append(dict(Symbol=sym, Date=dt, Action=action,
                          Quantity=qty, Price=price, Account=acct))
    return rows


def parse_realized_gain_csv(filepath: str) -> list[dict]:
    """Schwab realized-gain lot-detail CSV (single- or multi-account). Returns raw lot dicts."""
    fn = os.path.basename(filepath)
    with open(filepath, 'r', encoding='utf-8-sig') as f:
        first = f.readline()
    if not first.strip().startswith('"Realized Gain/Loss'):
        return []

    rows = []
    col_names = None
    try:
        with open(filepath, 'r', encoding='utf-8-sig') as f:
            reader = csv.reader(f)
            for i, row in enumerate(reader):
                if i == 0:  # file-level header line
                    continue
                if not row or not row[0].strip():
                    continue
                cell0 = row[0].strip().strip('"')
                if cell0 == 'Symbol':
                    col_names = [c.strip().strip('"') for c in row]
                    continue
                if re.search(r'\.\.\.\w+', cell0) or 'no transactions' in cell0.lower():
                    continue
                if col_names is None:
                    continue
                row_dict = dict(zip(col_names, row))
                sym = row_dict.get('Symbol', '').strip().strip('"')
                if not sym or sym == 'nan' or is_option(sym):
                    continue
                opened = pd.to_datetime(row_dict.get('Opened Date'), errors='coerce')
                closed = pd.to_datetime(row_dict.get('Closed Date'), errors='coerce')
                if pd.isna(closed):
                    continue
                qty = pd.to_numeric(str(row_dict.get('Quantity', 0)).replace(',', ''), errors='coerce')
                cost_ps = clean_num(row_dict.get('Cost Per Share'))
                proc_ps = clean_num(row_dict.get('Proceeds Per Share'))
                gl = clean_num(row_dict.get('Gain/Loss ($)'))
                gl_pct = clean_num(str(row_dict.get('Gain/Loss (%)', '')).replace('%', ''))
                rows.append(dict(Symbol=sym, Opened_Date=opened, Closed_Date=closed,
                                  Quantity=qty, Cost_Per_Share=cost_ps,
                                  Proceeds_Per_Share=proc_ps,
                                  Gain_Loss=gl, Gain_Loss_Pct=gl_pct))
    except Exception as e:
        print(f'lot skip {fn}: {e}')
        return []
    return rows


def load_transactions(buysell_dir: str, split_table: dict | None = None) -> pd.DataFrame:
    """All BUY/SELL/REINVEST rows from every transaction-history file in buysell_dir.

    With `split_table`, pre-split fills are restated into today's share terms:
    Quantity is scaled by the cumulative ratio since the trade date and Price
    divided by it. Without this, `analytics.compute_cost_basis` averages prices
    from either side of a split as if they were the same unit, and the result
    is compared against a post-split live price — e.g. KORU bought at $383.39
    (adjusted: $18.68) against a $21.50 quote. Price x Quantity is preserved,
    so `capital_deployed`'s dollar totals are unchanged.
    """
    tx_rows: list[dict] = []
    for fn in sorted(os.listdir(buysell_dir)):
        fp = os.path.join(buysell_dir, fn)
        if fn.endswith('.csv'):
            tx_rows.extend(parse_history_csv(fp))
        elif fn.endswith('.xlsx'):
            tx_rows.extend(parse_history_xlsx(fp))

    tx_df = (pd.DataFrame(tx_rows) if tx_rows
             else pd.DataFrame(columns=['Symbol', 'Date', 'Action', 'Quantity', 'Price', 'Account']))
    if not tx_df.empty:
        tx_df['Date'] = pd.to_datetime(tx_df['Date'])
        # Broker exports overlap: a multi-account history file (e.g. Fidelity's
        # Accounts_History.csv) covers the same trades as the per-account
        # History_for_Account_*.csv files. Dedupe on the trade itself, ignoring
        # Account — the same fill exported twice must not be counted twice in
        # capital_deployed or cost basis.
        before = len(tx_df)
        tx_df = (tx_df.drop_duplicates(subset=['Symbol', 'Date', 'Action', 'Quantity', 'Price'])
                       .reset_index(drop=True))
        dropped = before - len(tx_df)
        if dropped:
            print(f'transactions: dropped {dropped} duplicate row(s) of {before}')

        if split_table:
            import splits as _splits
            factors = pd.Series(
                [_splits.factor_since(split_table, str(s), d)
                 for s, d in zip(tx_df['Symbol'], tx_df['Date'])],
                index=tx_df.index, dtype=float)
            n = int((factors != 1.0).sum())
            if n:
                tx_df['Quantity'] = tx_df['Quantity'] * factors
                tx_df['Price'] = tx_df['Price'] / factors
                print(f'transactions: split-adjusted {n} pre-split row(s) '
                      f'({", ".join(sorted(set(tx_df.loc[factors != 1.0, "Symbol"])))})')
    return tx_df


def load_realized_lots(buysell_dir: str, cutoff: pd.Timestamp, *extra_dirs: str) -> pd.DataFrame:
    """All realized-gain lots from Schwab lot-detail CSVs.

    Scans `buysell_dir` plus any `extra_dirs` — realized-gain exports get
    downloaded into accounts/ as often as into buysell/, and lots that only
    exist there (TRGP, $1,061.90) were being missed entirely, understating
    realized_gl/total_pl/roic_pct. Duplicate lots across directories are
    dropped below.

    `opened` dates before `cutoff` are fudged to `cutoff` (DEFAULT_BUY handling
    lives in analytics.compute_cost_basis via the Cost_Basis_Source column —
    here we just pass the raw parsed dates through and let the caller decide).
    """
    lot_rows: list[dict] = []
    for d in (buysell_dir, *extra_dirs):
        if not d or not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith('.csv'):
                continue
            lot_rows.extend(parse_realized_gain_csv(os.path.join(d, fn)))

    sold_df = (pd.DataFrame(lot_rows) if lot_rows else pd.DataFrame(
        columns=['Symbol', 'Opened_Date', 'Closed_Date', 'Quantity',
                 'Cost_Per_Share', 'Proceeds_Per_Share', 'Gain_Loss', 'Gain_Loss_Pct']))
    if sold_df.empty:
        sold_df['Buy_Date'] = []
        sold_df['Sell_Date'] = []
        sold_df['Hold_Days'] = []
        return sold_df

    # Dedup on Gain_Loss rather than Quantity. Quantity is restated by the
    # broker after a split, so the same lot from an old and a re-downloaded
    # export would look distinct and be counted twice. Gain_Loss is total
    # dollars — split-invariant — while two genuinely different lots opened and
    # closed on the same dates still differ by it. (Dropping the field entirely
    # is not an option: it merges 4 real lots in the current data, losing $890
    # of realized gain.)
    sold_df = sold_df.drop_duplicates(
        subset=['Symbol', 'Opened_Date', 'Closed_Date', 'Gain_Loss']).reset_index(drop=True)
    sold_df['Buy_Date'] = sold_df['Opened_Date'].where(sold_df['Opened_Date'] >= cutoff, cutoff)
    sold_df['Sell_Date'] = sold_df['Closed_Date']
    sold_df['Hold_Days'] = (sold_df['Sell_Date'] - sold_df['Buy_Date']).dt.days
    return sold_df

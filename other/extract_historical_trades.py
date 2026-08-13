#!/usr/bin/env python3
"""One-time script: extract historical realized-gain lot data from old
brokerage PDFs sitting in fiData/other/ (bank statements, tax docs, medical
records, etc. mixed in — this only touches the ones identified as brokerage
trade records). Standalone — does not import anything from parsers/,
analytics/, or any other tracked pipeline file, and is not called from
anywhere in the tracked codebase. Output is a plain CSV for manual review,
not wired into buysell/ or any ingestion path.

Confirmed-clean source formats (single-line-per-lot, high confidence):
  - Schwab "Year-End Gain/Loss Report" PDFs
  - TD Ameritrade "Report for 1099-B" PDFs (R1099_*.pdf)
  - TD Ameritrade "Realized Book Capital Gain/Loss Report" PDFs (RC_*.pdf)

Lower-confidence source (two-line-per-lot, IRS 1099-B layout inside a much
longer composite tax document):
  - Schwab "1099 Composite and Year-End Summary" PDFs

Explicitly NOT attempted this pass (no populated example found while
scoping this script — every quarterly/monthly statement sampled had zero
trade activity in its Transaction Detail section, and guessing at an
unconfirmed table layout was exactly what the plan said to avoid):
  - Schwab quarterly `BrokerageStatement*.pdf` / `Brokerage Statement - XXXX*.pdf`
  - TD Ameritrade monthly `*Statementtd*.pdf`
  - E*Trade monthly `Brokerage Statement - XXXX6198/7249*.pdf`
  - Fidelity NetBenefits / `fidelity_2021.pdf`
  - `R8949_*.pdf` (skipped on purpose — same underlying lots as the cleaner
    `R1099_*.pdf` for the same account; processing both would double-count)

TD Ameritrade accounts here (755-027072, 867-504078) predate Schwab's
acquisition of TD Ameritrade — they may now correspond to one of the
current Schwab accounts. This script does not guess that mapping; it
records the original account number as seen in the document.
"""
import csv
import os
import re

import pdfplumber

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_CSV = os.path.join(HERE, 'extracted_historical_trades.csv')

FIELDS = ['Source_File', 'Broker', 'Account', 'Symbol', 'Action',
          'Open_Date', 'Close_Date', 'Quantity', 'Cost_Basis_Total',
          'Proceeds_Total', 'Gain_Loss', 'Notes']


def _num(s: str) -> float | None:
    s = (s or '').strip().replace('$', '').replace(',', '')
    neg = s.startswith('(') and s.endswith(')')
    s = s.strip('()')
    if not s or s in ('--', '-'):
        return None
    try:
        v = float(s)
        return -v if neg else v
    except ValueError:
        return None


def _pdf_text(path: str) -> str:
    with pdfplumber.open(path) as pdf:
        return '\n'.join(p.extract_text() or '' for p in pdf.pages)


# ── Schwab Year-End Gain/Loss Report ────────────────────────────────────────

_SCHWAB_YE_ACCT_RE = re.compile(r'Account Number\s*\n?\s*(\S+-\S+)|(\d{4}-\d{4})')
_SCHWAB_YE_ROW_RE = re.compile(
    r'^(?P<name>.+?):\s*(?P<symbol>[A-Z][A-Z.]*)\s+'
    r'(?P<qty>[\d,]+\.\d+)\s+'
    r'(?P<opened>\d{2}/\d{2}/\d{2})\s+(?P<closed>\d{2}/\d{2}/\d{2})\s+'
    r'\$?(?P<proceeds>[\d,]+\.\d+)\s+\$?(?P<cost>[\d,]+\.\d+)\s+'
    r'(?P<gl>\(?-?\$?[\d,.]+\)?)$'
)


def extract_schwab_year_end_gl(pdf_path: str) -> list[dict]:
    text = _pdf_text(pdf_path)
    fn = os.path.basename(pdf_path)
    acct_m = re.search(r'\b(\d{4}-\d{4})\b', text)
    account = acct_m.group(1) if acct_m else 'UNKNOWN'

    rows = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('Security Subtotal') or not line:
            continue
        m = _SCHWAB_YE_ROW_RE.match(line)
        if not m:
            continue
        gl = _num(m.group('gl'))
        rows.append({
            'Source_File': fn, 'Broker': 'Schwab', 'Account': account,
            'Symbol': m.group('symbol'), 'Action': 'SELL',
            'Open_Date': m.group('opened'), 'Close_Date': m.group('closed'),
            'Quantity': _num(m.group('qty')),
            'Cost_Basis_Total': _num(m.group('cost')),
            'Proceeds_Total': _num(m.group('proceeds')),
            'Gain_Loss': gl, 'Notes': '',
        })
    return rows


# ── TD Ameritrade Report for 1099-B (R1099_*.pdf) ───────────────────────────

_TD_R1099_ROW_RE = re.compile(
    r'^L\s+[\dA-Z]+\s+.*?\s+(?P<close>\d{2}/\d{2}/\d{4})stock\s+stock\s+'
    r'(?P<symbol>[A-Z.]+)\s+(?P<cusip>\S+)\s+US\s+(?P<name>.+?)\s+'
    r'(?P<open>various|\d{2}/\d{2}/\d{4})\s+'
    r'(?P<units>[\d,]+)\s+(?P<proceeds>[\d,.]+)\s+gross\s+'
    r'(?P<cost>[\d,.]+)\s+(?P<gl>-?[\d,.]+)\s+',
    re.IGNORECASE,
)


def extract_td_r1099(pdf_path: str) -> list[dict]:
    text = _pdf_text(pdf_path)
    fn = os.path.basename(pdf_path)
    acct_m = re.search(r'^(\d{6,10})$', text, re.MULTILINE)
    account = acct_m.group(1) if acct_m else 'UNKNOWN'

    rows = []
    for line in text.splitlines():
        line = line.strip()
        m = _TD_R1099_ROW_RE.match(line)
        if not m:
            continue
        rows.append({
            'Source_File': fn, 'Broker': 'TD Ameritrade', 'Account': account,
            'Symbol': m.group('symbol'), 'Action': 'SELL',
            'Open_Date': m.group('open'), 'Close_Date': m.group('close'),
            'Quantity': _num(m.group('units')),
            'Cost_Basis_Total': _num(m.group('cost')),
            'Proceeds_Total': _num(m.group('proceeds')),
            'Gain_Loss': _num(m.group('gl')), 'Notes': '',
        })
    return rows


# ── TD Ameritrade Realized Book Capital Gain/Loss Report (RC_*.pdf) ────────

_TD_RC_ROW_RE = re.compile(
    r'^(?P<name>.+?)\s+\((?P<symbol>[A-Z.]+)\)\s+Sell\.FIFO\s+'
    r'(?P<qty>[\d,]+)\s+(?P<open>\d{2}/\d{2}/\d{4})\s+'
    r'(?P<cost>[\d,.]+)\s+(?P<close>\d{2}/\d{2}/\d{4})\s+'
    r'(?P<proceeds>[\d,.]+)(?:\s+(?P<staterm>-?[\d,.]+))?(?:\s+(?P<ltterm>-?[\d,.]+))?$'
)


def extract_td_rc(pdf_path: str) -> list[dict]:
    text = _pdf_text(pdf_path)
    fn = os.path.basename(pdf_path)
    acct_m = re.search(r'^(\d{6,10})$', text, re.MULTILINE)
    account = acct_m.group(1) if acct_m else 'UNKNOWN'

    rows = []
    for line in text.splitlines():
        line = line.strip()
        m = _TD_RC_ROW_RE.match(line)
        if not m:
            continue
        st = _num(m.group('staterm'))
        lt = _num(m.group('ltterm'))
        gl = st if st is not None else lt
        rows.append({
            'Source_File': fn, 'Broker': 'TD Ameritrade', 'Account': account,
            'Symbol': m.group('symbol'), 'Action': 'SELL',
            'Open_Date': m.group('open'), 'Close_Date': m.group('close'),
            'Quantity': _num(m.group('qty')),
            'Cost_Basis_Total': _num(m.group('cost')),
            'Proceeds_Total': _num(m.group('proceeds')),
            'Gain_Loss': gl, 'Notes': '',
        })
    return rows


# ── Schwab 1099 Composite (embedded 1099-B, two-line-per-lot) ──────────────

_COMPOSITE_ACCT_RE = re.compile(r'\b(\d{4}-\d{4})\b')
_COMPOSITE_LOT_LINE1_RE = re.compile(
    r'^(?P<qty>[\d,]+)\s+(?P<name>.+?)\s+S\s+'
    r'(?P<open_inline>\d{2}/\d{2}/\d{2}|VARIOUS)?\s*\$?\s*'
    r'(?P<proceeds>[\d,]+\.\d+)\s+\$?\s*(?P<cost>[\d,]+\.\d+)\s+'
    r'.*?\$?\s*(?P<gl>\(?-?[\d,.]+\)?)\$'
)
_COMPOSITE_LOT_LINE2_RE = re.compile(
    r'^\S+\s*/\s*(?P<symbol>[A-Z.]+)\s+(?P<close>\d{2}/\d{2}/\d{2})'
)


def extract_schwab_composite_1099b(pdf_path: str) -> list[dict]:
    fn = os.path.basename(pdf_path)
    rows = []
    with pdfplumber.open(pdf_path) as pdf:
        full_text = '\n'.join(p.extract_text() or '' for p in pdf.pages)
        acct_m = _COMPOSITE_ACCT_RE.search(full_text)
        account = acct_m.group(1) if acct_m else 'UNKNOWN'

        for page in pdf.pages:
            text = page.extract_text() or ''
            if 'Proceeds from Broker Transactions' not in text:
                continue
            lines = text.splitlines()
            for i, line in enumerate(lines):
                line = line.strip()
                m1 = _COMPOSITE_LOT_LINE1_RE.match(line)
                if not m1 or i + 1 >= len(lines):
                    continue
                m2 = _COMPOSITE_LOT_LINE2_RE.match(lines[i + 1].strip())
                if not m2:
                    continue
                open_date = m1.group('open_inline') or m2.group('close')
                rows.append({
                    'Source_File': fn, 'Broker': 'Schwab', 'Account': account,
                    'Symbol': m2.group('symbol'), 'Action': 'SELL',
                    'Open_Date': open_date, 'Close_Date': m2.group('close'),
                    'Quantity': _num(m1.group('qty')),
                    'Cost_Basis_Total': _num(m1.group('cost')),
                    'Proceeds_Total': _num(m1.group('proceeds')),
                    'Gain_Loss': _num(m1.group('gl')),
                    'Notes': 'from 1099 Composite, 2-line layout — verify against source PDF',
                })
    return rows


# ── Dispatch ─────────────────────────────────────────────────────────────────

def classify(filename: str) -> str | None:
    if filename.startswith('YearEndGainLossReporting') and filename.endswith('.pdf'):
        return 'schwab_year_end_gl'
    if filename.startswith('R1099_') and filename.endswith('.pdf'):
        return 'td_r1099'
    if filename.startswith('RC_') and filename.endswith('.pdf'):
        return 'td_rc'
    if re.match(r'^1099\s?CompositeandYearEndSummary.*\.pdf$', filename, re.IGNORECASE) or \
       re.match(r'^1099 Composite and Year-End Summary.*\.PDF$', filename):
        return 'schwab_composite_1099b'
    return None


EXTRACTORS = {
    'schwab_year_end_gl': extract_schwab_year_end_gl,
    'td_r1099': extract_td_r1099,
    'td_rc': extract_td_rc,
    'schwab_composite_1099b': extract_schwab_composite_1099b,
}

NOT_ATTEMPTED_PATTERNS = [
    'BrokerageStatement', 'Brokerage Statement - XXXX', 'Statementtd',
    'fidelity_2021', 'Fidelity Netbenefits', 'R8949_',
]


def main():
    all_rows = []
    processed = {}
    not_attempted = []
    skipped_dupe_r8949 = []

    for fn in sorted(os.listdir(HERE)):
        path = os.path.join(HERE, fn)
        if not os.path.isfile(path):
            continue
        kind = classify(fn)
        if kind:
            rows = EXTRACTORS[kind](path)
            processed[fn] = (kind, len(rows))
            all_rows.extend(rows)
        elif fn.startswith('R8949_'):
            skipped_dupe_r8949.append(fn)
        elif any(p in fn for p in NOT_ATTEMPTED_PATTERNS):
            not_attempted.append(fn)

    with open(OUT_CSV, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(all_rows)

    print(f'Wrote {len(all_rows)} row(s) to {OUT_CSV}\n')
    print('Processed files:')
    for fn, (kind, n) in sorted(processed.items()):
        flag = '  <-- 0 rows, check manually' if n == 0 else ''
        print(f'  [{kind:24s}] {n:4d} row(s)  {fn}{flag}')

    if skipped_dupe_r8949:
        print(f'\nSkipped (duplicate of R1099_ for same account): {len(skipped_dupe_r8949)} file(s)')
        for fn in skipped_dupe_r8949:
            print(f'  {fn}')

    if not_attempted:
        print(f'\nNot attempted this pass (no confirmed populated trade example found): '
              f'{len(not_attempted)} file(s)')
        for fn in not_attempted:
            print(f'  {fn}')

    by_account = {}
    for r in all_rows:
        by_account.setdefault((r['Broker'], r['Account']), 0)
        by_account[(r['Broker'], r['Account'])] += 1
    print('\nRows by account:')
    for (broker, acct), n in sorted(by_account.items()):
        print(f'  {broker:16s} {acct:12s} {n} row(s)')


if __name__ == '__main__':
    main()

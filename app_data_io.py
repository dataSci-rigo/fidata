"""Shared app_data/*.json load + value-formatting helpers.

Previously viewer.py and viewer_app.py each defined their own load()/fmt();
this module consolidates them. The two originals differed slightly:
  - viewer.py's `load()` returns None for a missing file; viewer_app.py's
    `load_json()` lets the FileNotFoundError propagate. Both behaviors are
    kept here as two thin wrappers so neither caller's error handling changes.
  - viewer_app.py's `fmt()` supports more `kind`s ('$auto', 'pct', 'f2') and
    coerces strings via float() in a try/except; viewer.py's `fmt()` is a
    plain dispatch with no coercion. `fmt()` below is the superset
    (viewer_app.py's version), which is backwards compatible with every kind
    viewer.py ever passed it.

COL_FMTS moved here from viewer_app.py so the Tk viewer and the local web
viewer share one column-format map instead of each carrying a copy that drifts.
"""
import json
import os

DATA_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DATA = os.path.join(DATA_DIR, 'app_data')
DATA_STATE = os.path.join(DATA_DIR, 'data')

# Column name -> fmt() kind. Any column not listed renders as-is.
COL_FMTS = {
    # positions
    'Quantity':           'qty',   # fractional after a split — must not truncate
    'Current_Price':      '$',
    'Market_Value':       '$',
    'Total_Market_Value': '$',
    'Avg_Buy_Price':      '$',
    'First_Buy_Date':     'date',
    'Days_Held':          'int',
    # valuation
    'Trailing_PE':        'f2',
    'Forward_PE':         'f2',
    'MarketCap':          '$auto',
    # risk / performance
    'Ann_Vol':            'pct',   # stored as fraction e.g. 0.289
    'Sharpe_1yr':         'f2',
    'Sharpe_6m':          'f2',
    'Sharpe_3m':          'f2',
    'Gain_1yr':           '%',     # stored as percentage e.g. 47.1
    'Gain_6m':            '%',
    'Gain_3m':            '%',
    'Beta':               'f2',
    'Alpha_pct':          '%',
    'Weight_pct':         '%',
    'RiskContrib_pct':    '%',
    'correlation':        'f2',
    'Split_Factor':       'f2',
    # analyst
    'Target_Mean':        '$',
    'Target_Median':      '$',
    'Target_High':        '$',
    'Target_Low':         '$',
    'Target_Upside':      '%',
    'Target_Spread':      '%',
    'Num_Analysts':       'int',
    'Strong_Buy':         'int',
    'Buy':                'int',
    'Hold':               'int',
    'Sell':               'int',
    'Strong_Sell':        'int',
    'currentPriceTarget': '$',
    # earnings
    'EPS_Est':            '$',
    'Rev_Est_High':       '$auto',
    'Rev_Est_Low':        '$auto',
    'Next_Earnings':      'date',
    # capital / closed positions
    'Cost_Basis':         '$',
    'Unreal_GL':          '$',
    'Unreal_GL%':         '%',
    'Total_GL':           '$',
    'Avg_GL_Pct':         '%',
    'Total_Qty':          'qty',
    'Lots':               'int',
    'Avg_Hold':           'int',
    'First_Buy':          'date',
    'Last_Sell':          'date',
    'Bought':             '$big',
    'Sold':               '$big',
    'Net_Deployed':       '$big',
    'Realized_GL':        '$big',
}

# Files the pipeline is expected to produce, for the freshness/empty-state UI.
EXPECTED_ARTIFACTS = [
    ('accounts.json', APP_DATA), ('combined.json', APP_DATA),
    ('sectors.json', APP_DATA), ('flags.json', APP_DATA),
    ('targets.json', APP_DATA), ('earnings.json', APP_DATA),
    ('recommendations.json', APP_DATA),
    ('mpt_summary.json', DATA_STATE), ('portfolio_extras.json', DATA_STATE),
    ('last_run_snapshot.csv', DATA_STATE),
    ('efficient_frontier.png', DATA_DIR), ('correlation_heatmap.png', DATA_DIR),
    ('historical.csv', DATA_DIR),
]


def load(filename: str, app_data_dir: str = APP_DATA):
    """Return the parsed JSON, or None if the file doesn't exist."""
    path = os.path.join(app_data_dir, filename)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def load_json(name: str, app_data_dir: str = APP_DATA):
    """Return the parsed JSON. Raises FileNotFoundError if missing."""
    with open(os.path.join(app_data_dir, name)) as f:
        return json.load(f)


def fmt(val, kind: str = 'str') -> str:
    if val is None:
        return '—'
    try:
        v = float(val)
        if v != v:  # NaN check
            return '—'
        if kind == '$':
            return f'${v:,.2f}'
        if kind == '$big':
            return f'${v:,.0f}'
        if kind == '$auto':
            if abs(v) >= 1e12:
                return f'${v/1e12:.2f}T'
            if abs(v) >= 1e9:
                return f'${v/1e9:.2f}B'
            if abs(v) >= 1e6:
                return f'${v/1e6:.1f}M'
            if abs(v) >= 1e3:
                return f'${v/1e3:.1f}K'
            return f'${v:.2f}'
        if kind == '%':
            return f'{v:.2f}%'
        if kind == 'pct':
            return f'{v*100:.1f}%'
        if kind == 'x':
            return f'{v:.2f}x'
        if kind == 'f2':
            return f'{v:.2f}'
        if kind == 'int':
            return f'{int(v):,}'
        if kind == 'qty':
            # Whole shares read better as integers, but splits and DRIP leave
            # fractions that must not be silently truncated.
            return f'{int(v):,}' if float(v).is_integer() else f'{v:,.4f}'
    except (TypeError, ValueError):
        pass
    if kind == 'date':
        # '2023-01-02T00:00:00.000' -> '2023-01-02'
        return str(val)[:10]
    return str(val)


def pct_color(val) -> str:
    """Rich-markup-formatted % value (only viewer.py's CLI uses this)."""
    if val is None or val != val:
        return '—'
    color = 'green' if val >= 0 else 'red'
    return f'[{color}]{val:.2f}%[/{color}]'


def col_kind(name: str) -> str:
    return COL_FMTS.get(name, 'str')


def humanize(name: str) -> str:
    """Column name -> table header ('Target_Mean' -> 'Target Mean')."""
    return str(name).replace('_', ' ')


def cell(val, kind: str = 'str') -> tuple[str, str]:
    """(display text, css class) — the HTML analogue of pct_color().

    Returns plain text, never Markup, so Jinja's autoescaping still applies.
    The class is only about sign, so templates can colour gains/losses without
    re-parsing the formatted string.
    """
    text = fmt(val, kind)
    if text == '—':
        return text, 'muted'
    if kind in ('%', 'pct', '$', '$big', '$auto', 'f2'):
        try:
            v = float(val)
            if v == v:
                return text, 'pos' if v > 0 else 'neg' if v < 0 else ''
        except (TypeError, ValueError):
            pass
    return text, ''


def data_status(app_data_dir: str = APP_DATA, data_dir: str = DATA_STATE,
                 root_dir: str = DATA_DIR) -> list[dict]:
    """Per-artifact {name, path, exists, mtime, age_hours}, for the freshness
    banner and the 'pipeline hasn't run' empty states."""
    import time
    dir_map = {APP_DATA: app_data_dir, DATA_STATE: data_dir, DATA_DIR: root_dir}
    now = time.time()
    out = []
    for name, default_dir in EXPECTED_ARTIFACTS:
        path = os.path.join(dir_map.get(default_dir, default_dir), name)
        exists = os.path.exists(path)
        mtime = os.path.getmtime(path) if exists else None
        out.append({
            'name': name,
            'path': path,
            'exists': exists,
            'mtime': mtime,
            'age_hours': ((now - mtime) / 3600.0) if mtime else None,
        })
    return out

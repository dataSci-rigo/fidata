"""Filter contract for the discovery screener: one schema shared by the web
form, the AI query translator, and the filter engine.

`PARAMS_SPEC`-derived names are used verbatim as (a) HTML form input names,
(b) properties of the Anthropic tool schema (`tool_input_schema()`), and
(c) keys accepted by `apply_filters()` — so the three can never drift. The
AI's output goes through the same `validate_params()` as the form's, which
is what makes forced-tool-use safe without `strict` schema support in the
pinned anthropic 0.50.0.

Everything here is pure pandas over the universe.csv frame — no I/O, no
network — so tests drive it with synthetic frames.
"""
import math

import pandas as pd

# param base name -> (universe.csv column, unit description for the AI schema)
RANGE_FIELDS = {
    'market_cap': ('MarketCap', 'US dollars, e.g. 1e10 for $10B'),
    'pe_trailing': ('Trailing_PE', 'trailing price/earnings ratio'),
    'pe_forward': ('Forward_PE', 'forward price/earnings ratio'),
    'dividend_yield': ('Dividend_Yield', 'fraction: 0.03 means a 3% yield'),
    'expense_ratio': ('Expense_Ratio', 'fraction: 0.002 means 0.20% (ETFs only)'),
    'ann_vol': ('Ann_Vol', 'annualized volatility fraction: 0.20 means 20%'),
    'sharpe_1yr': ('Sharpe_1yr', 'trailing 1-year Sharpe ratio'),
    'beta': ('Beta', 'beta vs SPY'),
    'gain_3m': ('Gain_3m', 'percent number: 10 means +10% over 3 months'),
    'gain_6m': ('Gain_6m', 'percent number over 6 months'),
    'gain_1yr': ('Gain_1yr', 'percent number over 1 year'),
    'max_drawdown': ('Max_Drawdown',
                     'NEGATIVE fraction; min=-0.25 keeps drawdowns no worse than -25%'),
    'high_52w_ratio': ('High_52w_Ratio', '1.0 = at the 52-week high'),
    'corr_portfolio': ('Corr_Portfolio',
                       'correlation (-1..1) of daily returns vs the current portfolio'),
    'avg_dollar_vol': ('Avg_Dollar_Vol', 'average daily traded dollars'),
}

LIST_FIELDS = {
    'text_any': 'theme phrases ORed together, substring-matched case-insensitively '
                'against symbol/name/sector/industry/category/business summary, '
                'e.g. ["semiconductor", "artificial intelligence"]',
    'quote_types': 'subset of ["EQUITY", "ETF"]',
    'asset_classes': 'subset of ["Equity","Bond","Commodity","Real Estate",'
                     '"International","Mixed"]',
    'sectors': 'GICS-style sector names to include',
    'exclude_sectors': 'sector names to exclude',
}

# tri-state (True / False / absent = don't care)
TRISTATE_FIELDS = {'in_sp500': 'In_SP500', 'in_ndx': 'In_NDX'}

FLAG_FIELDS = {'uptrend_only', 'exclude_held', 'exclude_watchlist'}

SORT_COLUMNS = {**{k: v[0] for k, v in RANGE_FIELDS.items()}, 'symbol': 'Symbol'}

DEFAULTS = {'exclude_held': True, 'exclude_watchlist': False,
            'uptrend_only': False, 'sort_by': 'sharpe_1yr', 'sort_desc': True,
            'limit': 50}
LIMIT_MAX = 200

TEXT_SEARCH_COLS = ['Symbol', 'Name', 'Sector', 'Industry', 'Category', 'Summary']


def _norm_symbol(sym) -> str:
    """Match enrich.yf_symbol without importing it (keeps this module pure):
    broker exports say BRK/B, universe/history say BRK-B."""
    return str(sym).upper().strip().replace('/', '-')


def _to_float(val):
    f = float(val)
    if math.isnan(f) or math.isinf(f):
        raise ValueError('non-finite')
    return f


def validate_params(params: dict) -> tuple[dict, list[str]]:
    """Sanitize any params dict (web form or AI tool output) into the clean
    subset `apply_filters` accepts. Unknown keys and uncoercible values are
    dropped with a warning rather than raising — a bad AI value must never
    500 the page."""
    clean: dict = {}
    warnings: list[str] = []
    range_keys = {f'{base}_{side}' for base in RANGE_FIELDS for side in ('min', 'max')}

    for key, val in (params or {}).items():
        if val is None:
            continue
        try:
            if key in range_keys:
                clean[key] = _to_float(val)
            elif key in LIST_FIELDS:
                vals = [val] if isinstance(val, str) else list(val)
                vals = [str(v).strip() for v in vals if str(v).strip()]
                if vals:
                    clean[key] = vals
            elif key in TRISTATE_FIELDS or key in FLAG_FIELDS:
                if isinstance(val, str):
                    val = val.strip().lower() in ('1', 'true', 'yes', 'on')
                clean[key] = bool(val)
            elif key == 'sort_by':
                if val in SORT_COLUMNS:
                    clean[key] = val
                else:
                    warnings.append(f'unknown sort_by {val!r} — using default')
            elif key == 'sort_desc':
                if isinstance(val, str):
                    val = val.strip().lower() in ('1', 'true', 'yes', 'on')
                clean[key] = bool(val)
            elif key == 'limit':
                clean[key] = max(1, min(LIMIT_MAX, int(float(val))))
            else:
                warnings.append(f'unknown filter {key!r} dropped')
        except (TypeError, ValueError):
            warnings.append(f'bad value for {key!r} dropped: {val!r}')
    return clean, warnings


def params_from_args(args) -> dict:
    """Parse a GET query (werkzeug MultiDict) into a raw params dict for
    validate_params. The form always includes hidden `_form=1`; only then do
    absent checkboxes mean False (an URL without `_form` — a fresh visit —
    gets the defaults instead)."""
    raw: dict = {}
    getlist = getattr(args, 'getlist', None)
    for key in args:
        if key == '_form':
            continue
        vals = getlist(key) if getlist else [args[key]]
        vals = [v for v in (str(v).strip() for v in vals) if v]
        if not vals:
            continue
        if key == 'text_any':
            raw[key] = [p.strip() for v in vals for p in v.split(',') if p.strip()]
        elif key in LIST_FIELDS:
            raw[key] = vals
        elif key in TRISTATE_FIELDS:
            if vals[-1].lower() in ('yes', 'no', 'true', 'false'):
                raw[key] = vals[-1].lower() in ('yes', 'true')
        else:
            raw[key] = vals[-1]
    if '_form' in args:
        for flag in FLAG_FIELDS | {'sort_desc'}:   # checkboxes: absent = off
            raw.setdefault(flag, False)
    return raw


def apply_filters(df: pd.DataFrame, params: dict,
                  held: frozenset = frozenset(),
                  watching: frozenset = frozenset()) -> pd.DataFrame:
    """Filter/sort/limit the universe frame. `params` should already be
    validated. `held`/`watching` are symbol sets in any spelling (normalized
    here). Badge columns Held/In_Watchlist are added before any exclusion so
    they render when the exclusions are off.

    A row with NaN in a field is excluded while a bound on that field is
    active — so `pe_trailing_max` naturally drops ETFs, and `expense_ratio_max`
    naturally drops single stocks.
    """
    p = {**DEFAULTS, **(params or {})}
    out = df.copy()
    norm = out['Symbol'].map(_norm_symbol)
    held_n = {_norm_symbol(s) for s in held}
    watch_n = {_norm_symbol(s) for s in watching}
    out['Held'] = norm.isin(held_n)
    out['In_Watchlist'] = norm.isin(watch_n)

    mask = pd.Series(True, index=out.index)

    phrases = p.get('text_any') or []
    if phrases:
        hay = pd.Series('', index=out.index)
        for col in TEXT_SEARCH_COLS:
            if col in out.columns:
                hay = hay + ' ' + out[col].fillna('').astype(str)
        hay = hay.str.lower()
        text_mask = pd.Series(False, index=out.index)
        for phrase in phrases:
            text_mask |= hay.str.contains(str(phrase).lower(), regex=False)
        mask &= text_mask

    for key, col in (('quote_types', 'Quote_Type'), ('asset_classes', 'Asset_Class'),
                     ('sectors', 'Sector')):
        if p.get(key) and col in out.columns:
            mask &= out[col].isin(p[key])
    if p.get('exclude_sectors') and 'Sector' in out.columns:
        mask &= ~out['Sector'].isin(p['exclude_sectors'])

    for key, col in TRISTATE_FIELDS.items():
        if key in p and col in out.columns:
            mask &= out[col].fillna(False).astype(bool) == p[key]

    for base, (col, _desc) in RANGE_FIELDS.items():
        lo, hi = p.get(f'{base}_min'), p.get(f'{base}_max')
        if (lo is None and hi is None) or col not in out.columns:
            continue
        vals = pd.to_numeric(out[col], errors='coerce')
        mask &= vals.notna()
        if lo is not None:
            mask &= vals >= lo
        if hi is not None:
            mask &= vals <= hi

    if p.get('uptrend_only') and 'Uptrend' in out.columns:
        mask &= out['Uptrend'].fillna(False).astype(bool)
    if p.get('exclude_held'):
        mask &= ~out['Held']
    if p.get('exclude_watchlist'):
        mask &= ~out['In_Watchlist']

    out = out[mask]

    sort_col = SORT_COLUMNS.get(p['sort_by'], 'Sharpe_1yr')
    if sort_col in out.columns:
        asc = not p.get('sort_desc', True)
        out = out.sort_values(sort_col, ascending=asc, na_position='last')
    return out.head(p['limit']).reset_index(drop=True)


def tool_input_schema() -> dict:
    """Anthropic tool input_schema generated from the spec — the AI cannot
    invent parameter names the engine doesn't know."""
    props: dict = {
        'rationale': {
            'type': 'string',
            'description': 'One or two sentences explaining the chosen filters, '
                           'referencing the portfolio context numbers.'},
        'sort_by': {'enum': sorted(SORT_COLUMNS)},
        'sort_desc': {'type': 'boolean'},
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': LIMIT_MAX},
    }
    for key, desc in LIST_FIELDS.items():
        props[key] = {'type': 'array', 'items': {'type': 'string'}, 'description': desc}
    props['quote_types'] = {'type': 'array', 'items': {'enum': ['EQUITY', 'ETF']},
                            'description': LIST_FIELDS['quote_types']}
    props['asset_classes'] = {
        'type': 'array',
        'items': {'enum': ['Equity', 'Bond', 'Commodity', 'Real Estate',
                           'International', 'Mixed']},
        'description': LIST_FIELDS['asset_classes']}
    for key in list(TRISTATE_FIELDS) + sorted(FLAG_FIELDS):
        props[key] = {'type': 'boolean'}
    for base, (_col, desc) in RANGE_FIELDS.items():
        props[f'{base}_min'] = {'type': 'number', 'description': f'minimum; {desc}'}
        props[f'{base}_max'] = {'type': 'number', 'description': f'maximum; {desc}'}
    return {'type': 'object', 'additionalProperties': False,
            'required': ['rationale'], 'properties': props}

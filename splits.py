"""Stock-split normalization.

Brokers restate share counts when a split happens, so anything already inside
an export is consistent. The exposure is the gap between downloading exports
and now — with exports refreshed every month or two and roughly four splits a
year hitting this book, expect a split to land in that gap about once per
refresh cycle.

The whole approach is one idea: **every stale input carries a date, so
normalize each one into today's share terms as it enters the pipeline.**
`factor_since(table, sym, date)` is the cumulative ratio of every split after
`date`; multiply a share count by it and divide a per-share price by it, and
stale data becomes comparable with live prices.

Applied at five boundaries (see REFACTOR_PLAN.md):
  A positions      — stale export quantity      -> today's shares
  B snapshots      — before infer_missing_trades diffs them, or a split looks
                     exactly like a huge unexplained BUY
  C transactions   — pre-split fills            -> today's shares/prices
  D historical.csv — refetch a symbol whole rather than appending across a
                     split, because combine_first never re-adjusts cached rows
  E alerts         — so an ex-date doesn't read as a -95% crash

Split data piggybacks the price download that refresh_historical already makes
(`yf.download(..., actions=True)`), so it costs no extra network round trips,
and is cached to data/splits.csv so offline paths (load_last_run, the local
viewer) keep working.
"""
import os

import pandas as pd

CACHE_NAME = 'splits.csv'

# A split ratio is integer-scale (2, 10, 20) or its reciprocal for a reverse
# split. auto_adjust also folds dividends into historical prices, which moves
# them by a few percent — this is the band that separates the two.
_DIVIDEND_NOISE = 0.06

# Accept a measured ratio as confirming a split when it lands within this
# relative distance of the reported one.
_RATIO_TOL = 0.05


def _naive(idx) -> pd.DatetimeIndex:
    idx = pd.DatetimeIndex(idx)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    return idx.normalize()


# ── cache ─────────────────────────────────────────────────────────────────────

def load_table(cache_file: str) -> dict[str, pd.Series]:
    """{symbol: Series(ratio, indexed by naive split date)}."""
    if not cache_file or not os.path.exists(cache_file):
        return {}
    try:
        df = pd.read_csv(cache_file, parse_dates=['Date'])
    except Exception as e:
        print(f'WARNING (splits): could not read {cache_file}: {e}')
        return {}
    if df.empty or not {'Symbol', 'Date', 'Ratio'} <= set(df.columns):
        return {}
    out: dict[str, pd.Series] = {}
    for sym, grp in df.groupby('Symbol'):
        s = pd.Series(grp['Ratio'].astype(float).values, index=_naive(grp['Date']))
        out[str(sym)] = s[~s.index.duplicated()].sort_index()
    return out


def save_table(table: dict[str, pd.Series], cache_file: str) -> None:
    rows = [{'Symbol': sym, 'Date': d.date(), 'Ratio': float(r)}
            for sym, s in sorted(table.items()) for d, r in s.items()]
    os.makedirs(os.path.dirname(cache_file) or '.', exist_ok=True)
    pd.DataFrame(rows, columns=['Symbol', 'Date', 'Ratio']).to_csv(cache_file, index=False)


def merge_downloaded(table: dict[str, pd.Series],
                      downloaded: dict[str, pd.Series]) -> dict[str, pd.Series]:
    """Fold a batch download's 'Stock Splits' column into the cached table.

    A download only covers the window it was asked for, so newly seen events
    are added and older cached ones are kept — never replaced wholesale.
    """
    out = {sym: s.copy() for sym, s in table.items()}
    for sym, s in (downloaded or {}).items():
        s = s[s > 0]
        if s.empty:
            continue
        s = pd.Series(s.values.astype(float), index=_naive(s.index))
        merged = pd.concat([out.get(sym, pd.Series(dtype=float)), s])
        out[sym] = merged[~merged.index.duplicated(keep='last')].sort_index()
    return out


def load_meta(cache_file: str) -> dict:
    """{'covered_from': iso date, 'symbols': [...]} beside the cache."""
    import json
    path = _meta_path(cache_file)
    if not os.path.exists(path):
        return {'covered_from': None, 'symbols': []}
    try:
        with open(path) as f:
            m = json.load(f)
        return {'covered_from': m.get('covered_from'), 'symbols': list(m.get('symbols') or [])}
    except Exception:
        return {'covered_from': None, 'symbols': []}


def save_meta(meta: dict, cache_file: str) -> None:
    import json
    os.makedirs(os.path.dirname(cache_file) or '.', exist_ok=True)
    with open(_meta_path(cache_file), 'w') as f:
        json.dump({'covered_from': meta.get('covered_from'),
                   'symbols': sorted(meta.get('symbols') or [])}, f, indent=2)


def _meta_path(cache_file: str) -> str:
    base, _ = os.path.splitext(cache_file)
    return base + '_meta.json'


def needs_seed(meta: dict, symbols, start) -> list[str]:
    """Symbols whose split history isn't covered back to `start`.

    The incremental price refresh only fetches from last_date + 1, so its
    download window contains no *past* split — on a normal run the feed learns
    nothing about the 20:1 that happened a month ago. Coverage therefore has to
    be established once, over the window that actually matters (back to the
    oldest transaction), and maintained incrementally from then on.
    """
    start = pd.Timestamp(start).normalize()
    covered = meta.get('covered_from')
    known = set(meta.get('symbols') or [])
    if covered is None or pd.Timestamp(covered) > start:
        return list(symbols)
    return [s for s in symbols if s not in known]


def extract_from_download(raw) -> dict[str, pd.Series]:
    """Pull per-symbol split events out of a yf.download(..., actions=True)
    frame. Returns {yahoo_symbol: Series} — callers map back to portfolio
    symbols (BRK-B -> BRK/B) themselves."""
    if raw is None or getattr(raw, 'empty', True):
        return {}
    out: dict[str, pd.Series] = {}
    cols = raw.columns
    if isinstance(cols, pd.MultiIndex):
        if 'Stock Splits' not in cols.get_level_values(0):
            return {}
        block = raw['Stock Splits']
        for sym in block.columns:
            s = block[sym].dropna()
            s = s[s > 0]
            if not s.empty:
                out[str(sym)] = s
    elif 'Stock Splits' in cols:
        s = raw['Stock Splits'].dropna()
        s = s[s > 0]
        if not s.empty:
            out['__single__'] = s
    return out


# ── the core helper ───────────────────────────────────────────────────────────

def factor_since(table: dict[str, pd.Series], sym: str, since) -> float:
    """Cumulative split ratio strictly after `since`.

    1.0 when nothing applies. Reverse splits (ratio < 1) compose naturally,
    as do several splits in the same window. `since` may be None, meaning
    "unknown age" — treated as fresh, which is the safe direction: guessing
    too new leaves the data as-is, while guessing too old would double-apply
    a split and be off by the ratio.
    """
    if since is None or sym not in table:
        return 1.0
    since = pd.Timestamp(since)
    if since.tz is not None:
        since = since.tz_localize(None)
    s = table[sym]
    after = s[s.index > since.normalize()]
    if after.empty:
        return 1.0
    return float(after.prod())


def events_since(table: dict[str, pd.Series], sym: str, since) -> list[tuple]:
    """[(date, ratio)] after `since` — for alert text."""
    if since is None or sym not in table:
        return []
    since = pd.Timestamp(since)
    if since.tz is not None:
        since = since.tz_localize(None)
    s = table[sym]
    return [(d, float(r)) for d, r in s[s.index > since.normalize()].items()]


# ── safety check ──────────────────────────────────────────────────────────────

def implied_ratio(market_value, quantity, adjusted_price) -> float | None:
    """Measure the split ratio from the export itself.

    A broker export is internally consistent — quantity x its own price equals
    its own market value — and historical.csv is split-adjusted to today. So
    market_value / (quantity * adjusted_close_on_export_date) is the ratio
    between the export's share units and today's, measured from data already
    on hand rather than taken on trust.
    """
    try:
        q = float(quantity)
        mv = float(market_value)
        px = float(adjusted_price)
    except (TypeError, ValueError):
        return None
    if not (q and px) or q != q or px != px or mv != mv:
        return None
    return mv / (q * px)


def verify_ratio(reported: float, measured: float | None, tol: float = _RATIO_TOL) -> bool:
    """Does the export's own arithmetic agree that this split still applies?

    `measured` of None means we couldn't check (no price for that date) — the
    caller decides whether to proceed. A measured ratio near 1.0 while a split
    is reported means the export was already restated, i.e. applying the
    factor would double-count it.
    """
    if measured is None:
        return False
    if reported <= 0:
        return False
    return abs(measured / reported - 1.0) <= tol


def already_applied(measured: float | None) -> bool:
    """True when the export's own numbers say it is already in today's units."""
    return measured is not None and abs(measured - 1.0) <= _DIVIDEND_NOISE


# ── export dating ─────────────────────────────────────────────────────────────

def export_dates(accounts_dir: str, accounts: dict, hist_df: pd.DataFrame,
                  table: dict | None = None) -> dict:
    """{account: as-of Timestamp or None} for the position exports on disk.

    Three sources, in order of trustworthiness:
      1. the date in the filename (Schwab, Fidelity) — reuses the same
         `_date_from_filename` that already backs load_snapshot;
      2. an in-file 'as of' header (Holdings.xlsx);
      3. price matching against historical.csv, for E*Trade's
         PortfolioDownload*.csv which carries no date at all. Copying files
         between machines resets mtime, so that is not a usable fallback.

    None means "unknown age", which factor_since treats as fresh — the safe
    direction, since guessing too new is a no-op while guessing too old would
    double-apply a split.
    """
    from parsers import _date_from_filename, parse_file

    dated: dict = {}
    undated: list[tuple[str, dict]] = []
    if os.path.isdir(accounts_dir):
        for fn in sorted(os.listdir(accounts_dir)):
            fp = os.path.join(accounts_dir, fn)
            parsed = parse_file(fp, quiet=True)
            if not parsed:
                continue
            d = _date_from_filename(fn) or _date_from_header(fp)
            if d is None:
                undated.append((fp, parsed))
                continue
            for acct in parsed:
                if acct not in dated or d > dated[acct]:
                    dated[acct] = d

    # Undated exports (E*Trade's PortfolioDownload*.csv): recover the date from
    # the prices they quote, since nothing else in or around the file says.
    for fp, parsed in undated:
        for acct, df in parsed.items():
            if acct in dated or 'Current_Price' not in df.columns:
                continue
            guess = infer_date_from_prices(
                {str(s): df.at[s, 'Current_Price'] for s in df.index if str(s) != 'cash'},
                hist_df, table=table)
            if guess is not None:
                dated[acct] = guess

    return {acct: dated.get(acct) for acct in accounts}


def _date_from_header(filepath: str) -> pd.Timestamp | None:
    """An 'as of <date>' line inside the file (Holdings.xlsx carries one)."""
    import re
    text = ''
    try:
        if filepath.endswith('.xlsx'):
            head = pd.read_excel(filepath, header=None, nrows=12)
            text = ' '.join(str(v) for v in head.values.ravel() if str(v) != 'nan')
        elif filepath.endswith('.csv'):
            with open(filepath, 'r', encoding='utf-8-sig', errors='replace') as f:
                text = ''.join(f.readline() for _ in range(8))
        else:
            return None
    except Exception:
        return None
    m = re.search(r'as of\s*:?\s*(\d{1,2}/\d{1,2}/\d{4})', text, re.I)
    if not m:
        return None
    try:
        return pd.Timestamp(pd.to_datetime(m.group(1), format='%m/%d/%Y'))
    except Exception:
        return None


def adjust_positions(accounts: dict, as_of: dict, table: dict,
                      hist_df: pd.DataFrame) -> tuple[dict, dict, list[str]]:
    """Restate stale export quantities into today's share terms.

    Returns (accounts, factor_by_symbol, messages). Each account DataFrame is
    only copied when something actually changes, so the common case (no split
    since the export) is a no-op.

    Every adjustment is gated on the export's own arithmetic agreeing — see
    implied_ratio(). A reported split whose measured ratio says 1.0 means the
    export was already restated by the broker, and applying the factor would
    double-count it, so that case is skipped loudly rather than silently.
    """
    out, factors, messages = {}, {}, []
    for acct, df in accounts.items():
        export_date = as_of.get(acct)
        changed = None
        for sym in df.index:
            sym = str(sym)
            if sym == 'cash':
                continue
            factor = factor_since(table, sym, export_date)
            if factor == 1.0:
                continue

            measured = None
            col = sym if sym in hist_df.columns else sym.replace('/', '-')
            if export_date is not None and col in getattr(hist_df, 'columns', []):
                ref = hist_df[col].dropna()
                ref = ref[ref.index <= pd.Timestamp(export_date)]
                if not ref.empty:
                    measured = implied_ratio(df.at[sym, 'Market_Value'],
                                              df.at[sym, 'Quantity'], ref.iloc[-1])

            if already_applied(measured):
                messages.append(
                    f'{sym}: split reported since {pd.Timestamp(export_date).date()} but the '
                    f'{acct} export already reflects it — not adjusting')
                continue
            if not verify_ratio(factor, measured):
                messages.append(
                    f'{sym}: split {factor:g}x reported since '
                    f'{pd.Timestamp(export_date).date()} but the {acct} export measures '
                    f'{"n/a" if measured is None else format(measured, ".2f")} — '
                    f'NOT adjusting, check the export')
                continue

            if changed is None:
                changed = df.copy()
            old_qty = float(changed.at[sym, 'Quantity'])
            new_qty = old_qty * factor
            changed.at[sym, 'Quantity'] = new_qty
            if 'Current_Price' in changed.columns:
                changed.at[sym, 'Current_Price'] = float(changed.at[sym, 'Current_Price']) / factor
            # Per-symbol marker, NOT accumulated: a symbol held in two accounts
            # gets the same factor applied to each, and multiplying them would
            # report 400x for a 20:1. If two accounts have different export
            # dates their factors can genuinely differ — each quantity is still
            # individually correct; the column just shows the largest applied.
            prior = factors.get(sym)
            factors[sym] = factor if prior is None or abs(factor) > abs(prior) else prior
            ev = ', '.join(f'{r:g}:1 on {d.date()}' for d, r in events_since(table, sym, export_date))
            messages.append(
                f'{sym} split {ev} — {acct} export from '
                f'{pd.Timestamp(export_date).date()} predates it; '
                f'{old_qty:g} -> {new_qty:g} shares')
        out[acct] = changed if changed is not None else df
    return out, factors, messages


def infer_date_from_prices(prices: dict, hist_df: pd.DataFrame,
                            table: dict[str, pd.Series] | None = None,
                            lookback: int = 120,
                            max_median_err: float = 0.005) -> pd.Timestamp | None:
    """Recover an undated export's as-of date by matching its quoted prices.

    E*Trade's PortfolioDownload*.csv carries no date anywhere — not in the
    filename, not in a header — and copying files around resets mtime, so this
    is the only signal left. Score every recent trading day by the median
    relative error between the export's prices and that day's closes, and
    accept the best only if it is both tight and clearly better than the
    runner-up.

    Symbols that split inside the window are excluded from scoring: their
    export price is in pre-split units, which is precisely the thing we don't
    know yet.
    """
    if hist_df is None or hist_df.empty or not prices:
        return None
    idx = hist_df.index[-lookback:]
    if len(idx) < 5:
        return None

    usable = {}
    for sym, px in prices.items():
        col = sym if sym in hist_df.columns else sym.replace('/', '-')
        if col not in hist_df.columns:
            continue
        try:
            px = float(px)
        except (TypeError, ValueError):
            continue
        if not px or px != px:
            continue
        if table and factor_since(table, sym, idx[0]) != 1.0:
            continue  # split inside the window — units are ambiguous
        usable[col] = px
    if len(usable) < 3:
        return None

    scores = []
    for d in idx:
        errs = []
        for col, px in usable.items():
            ref = hist_df.at[d, col] if col in hist_df.columns else None
            if ref is None or ref != ref or not ref:
                continue
            errs.append(abs(px / float(ref) - 1.0))
        if len(errs) >= 3:
            scores.append((float(pd.Series(errs).median()), d))
    if not scores:
        return None

    scores.sort()
    best_err, best_date = scores[0]
    if best_err > max_median_err:
        return None
    # require a clear winner: adjacent trading days are highly correlated, so
    # only reject when some *other* day is nearly as good AND far away.
    for err, d in scores[1:]:
        if abs((d - best_date).days) > 5 and err <= best_err * 1.5:
            return None
        break
    return pd.Timestamp(best_date)

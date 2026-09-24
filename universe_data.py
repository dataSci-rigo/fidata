"""Discovery-universe data layer: candidate stocks/ETFs the portfolio does
NOT hold yet, kept fully separate from historical.csv and the 3x/day pipeline.

Sources: S&P 500 + Nasdaq-100 constituents (Wikipedia, with committed .txt
fallbacks in universe/) + the curated universe/etf_list.txt. Prices come from
one batched yf.download per chunk (2y window); fundamentals from Ticker.info
behind a throttled, resumable JSON cache — the enrichment loops in enrich.py
are per-run and uncached, which does not scale to ~700 symbols.

Everything generated lives under data/universe/ (gitignored via *.csv/*.json).
yfinance is imported lazily inside the fetch functions so the local web
server can import the loaders without pulling it in.
"""
import json
import os
import time
from datetime import date, datetime, timedelta, timezone

import pandas as pd

DATA_DIR = os.path.dirname(os.path.abspath(__file__))
SEED_DIR = os.path.join(DATA_DIR, 'universe')
UNIVERSE_DIR = os.path.join(DATA_DIR, 'data', 'universe')

ETF_LIST_FILE = os.path.join(SEED_DIR, 'etf_list.txt')
SP500_FALLBACK = os.path.join(SEED_DIR, 'sp500_fallback.txt')
NDX_FALLBACK = os.path.join(SEED_DIR, 'ndx_fallback.txt')

CONSTITUENTS_FILE = 'constituents.csv'
CLOSES_FILE = 'closes.csv'
VOLUME_FILE = 'volume.csv'
INFO_CACHE_FILE = 'info_cache.json'
UNIVERSE_FILE = 'universe.csv'
TREASURY_FILE = 'treasury_yields.json'
STATUS_FILE = 'universe_status.json'

WIKI_SP500 = 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
# The Nasdaq-100 article itself dropped its components table in 2026; the
# constituents live on a dedicated list page now.
WIKI_NDX = 'https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies'
_UA = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) fiData/1.0'}

YEARS = 2                     # metrics need 252d + corr overlap; 2y is plenty
LOOKBACK = 252
DOLLAR_VOL_WINDOW = 63
MIN_CORR_OVERLAP = 60
SUMMARY_MAX = 300

TREASURY_TICKERS = {'^IRX': '13-week', '^FVX': '5-year',
                    '^TNX': '10-year', '^TYX': '30-year'}

_ASSET_CLASS_BY_PREFIX = [
    ('bond', 'Bond'), ('gold', 'Commodity'), ('commodity', 'Commodity'),
    ('crypto', 'Commodity'), ('real_estate', 'Real Estate'),
    ('sector_realestate', 'Real Estate'), ('intl', 'International'),
    ('mixed', 'Mixed'), ('covered_call', 'Mixed'), ('preferred', 'Mixed'),
]


def asset_class_from_tag(tag: str) -> str:
    tag = (tag or '').strip().lower()
    for prefix, cls in _ASSET_CLASS_BY_PREFIX:
        if tag.startswith(prefix):
            return cls
    return 'Equity'


# ── seed lists / constituents ────────────────────────────────────────────────

def load_etf_list(path: str = ETF_LIST_FILE) -> pd.DataFrame:
    """universe/etf_list.txt -> DataFrame[Symbol, ETF_Tag]. Line format
    `SYMBOL TAG  # comment`; '#' comments and blanks skipped."""
    rows = []
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                body = line.split('#', 1)[0].strip()
                if not body:
                    continue
                parts = body.split()
                rows.append({'Symbol': parts[0].upper(),
                             'ETF_Tag': parts[1] if len(parts) > 1 else ''})
    return pd.DataFrame(rows, columns=['Symbol', 'ETF_Tag'])


def parse_wiki_symbols(tables: list, columns: tuple, bounds: tuple) -> list[str]:
    """Pick the constituents table out of read_html output by column name,
    normalize `.`->`-` (BRK.B -> BRK-B), enforce sanity row bounds."""
    lo, hi = bounds
    for t in tables:
        for col in columns:
            if col in t.columns and len(t) >= lo // 2:
                syms = (t[col].astype(str).str.strip().str.upper()
                        .str.replace('.', '-', regex=False))
                out = sorted({s for s in syms if s and s != 'NAN'})
                if lo <= len(out) <= hi:
                    return out
    return []


def _fetch_wiki(url: str, columns: tuple, bounds: tuple) -> list[str]:
    import io

    import requests
    html = requests.get(url, headers=_UA, timeout=30).text
    return parse_wiki_symbols(pd.read_html(io.StringIO(html)), columns, bounds)


def load_fallback(path: str) -> list[str]:
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return sorted({line.split('#', 1)[0].strip().upper() for line in f
                       if line.split('#', 1)[0].strip()})


def build_constituents(universe_dir: str = UNIVERSE_DIR,
                       etf_file: str = ETF_LIST_FILE,
                       sp500_fallback: str = SP500_FALLBACK,
                       ndx_fallback: str = NDX_FALLBACK,
                       live: bool = True) -> pd.DataFrame:
    """DataFrame[Symbol, In_SP500, In_NDX, ETF_Tag]. Fallback chain per index:
    live Wikipedia fetch -> cached constituents.csv -> committed .txt snapshot.
    A successful live fetch refreshes the cache; a failed one only costs
    freshness, never the feature."""
    cache_path = os.path.join(universe_dir, CONSTITUENTS_FILE)
    cached = pd.read_csv(cache_path) if os.path.exists(cache_path) else pd.DataFrame()

    def resolve(fetcher, cache_col, fallback_path, label):
        if live:
            try:
                syms = fetcher()
                if syms:
                    return syms, 'live'
            except Exception as e:
                print(f'WARNING: {label} live fetch failed: {e}')
        if not cached.empty and cache_col in cached.columns:
            syms = sorted(cached.loc[cached[cache_col].fillna(False), 'Symbol'])
            if syms:
                return syms, 'cache'
        return load_fallback(fallback_path), 'fallback'

    sp, sp_src = resolve(lambda: _fetch_wiki(WIKI_SP500, ('Symbol',), (480, 520)),
                         'In_SP500', sp500_fallback, 'S&P 500')
    ndx, ndx_src = resolve(lambda: _fetch_wiki(WIKI_NDX, ('Ticker', 'Symbol'), (95, 110)),
                           'In_NDX', ndx_fallback, 'Nasdaq-100')
    print(f'constituents: S&P {len(sp)} ({sp_src}), NDX {len(ndx)} ({ndx_src})')

    etfs = load_etf_list(etf_file)
    symbols = sorted(set(sp) | set(ndx) | set(etfs['Symbol']))
    tag_map = dict(zip(etfs['Symbol'], etfs['ETF_Tag']))
    df = pd.DataFrame({
        'Symbol': symbols,
        'In_SP500': [s in set(sp) for s in symbols],
        'In_NDX': [s in set(ndx) for s in symbols],
        'ETF_Tag': [tag_map.get(s, '') for s in symbols],
    })
    if sp_src == 'live' or ndx_src == 'live' or not os.path.exists(cache_path):
        _atomic_csv(df, cache_path)
    return df


# ── prices ───────────────────────────────────────────────────────────────────

def _download_close_volume(symbols: list[str], start: str, end: str
                           ) -> tuple[dict[str, pd.Series], dict[str, pd.Series]]:
    """One batched yf.download carrying Close and Volume together — modeled on
    enrich._download_closes (same MultiIndex/flat and tz handling) but with
    actions=False since split detection is the pipeline's job, not ours."""
    import yfinance as yf
    if not symbols:
        return {}, {}
    try:
        raw = yf.download(list(symbols), start=start, end=end, auto_adjust=True,
                          actions=False, progress=False, threads=True,
                          group_by='column')
    except Exception as e:
        print(f'WARNING (universe batch {start}, {len(symbols)} symbols): {e}')
        return {}, {}
    if raw is None or raw.empty:
        return {}, {}

    def field(name):
        if isinstance(raw.columns, pd.MultiIndex):
            if name not in raw.columns.get_level_values(0):
                return None
            frame = raw[name]
        else:
            if name not in raw.columns:
                return None
            frame = raw[[name]]
            frame.columns = list(symbols)
        out = {}
        for sym in symbols:
            if sym not in frame.columns:
                continue
            s = frame[sym].dropna()
            if s.empty:
                continue
            idx = pd.DatetimeIndex(s.index)
            if idx.tz is not None:
                idx = idx.tz_localize(None)
            s.index = idx.normalize()
            out[sym] = s.rename(sym)
        return out

    return field('Close') or {}, field('Volume') or {}


def refresh_prices(symbols: list[str], universe_dir: str = UNIVERSE_DIR,
                   years: int = YEARS, chunk: int = 150,
                   pause: float = 2.0,
                   status_cb=None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Incremental 2y close+volume refresh into closes.csv / volume.csv.
    Known symbols fetch only last_date+1 onward; new symbols get the full
    window; symbols gone from the universe are dropped."""
    closes_path = os.path.join(universe_dir, CLOSES_FILE)
    vol_path = os.path.join(universe_dir, VOLUME_FILE)
    closes = _read_wide(closes_path)
    volumes = _read_wide(vol_path)

    today = date.today()
    full_start = str(today - timedelta(days=365 * years + 10))
    end = str(today + timedelta(days=1))

    known = [s for s in symbols if s in closes.columns]
    new = [s for s in symbols if s not in closes.columns]
    closes = closes[[c for c in closes.columns if c in set(symbols)]]
    volumes = volumes[[c for c in volumes.columns if c in set(symbols)]]

    plans = []
    if known and not closes.empty:
        tail_start = (closes.index.max() + pd.Timedelta(days=1)).date()
        if str(tail_start) < end:
            plans.append((known, str(tail_start)))
    elif known:
        plans.append((known, full_start))
    if new:
        plans.append((new, full_start))

    done = 0
    total = sum(len(batch) for batch, _ in plans)
    for batch, start in plans:
        for i in range(0, len(batch), chunk):
            part = batch[i:i + chunk]
            got_c, got_v = _download_close_volume(part, start, end)
            closes = _merge_wide(closes, got_c)
            volumes = _merge_wide(volumes, got_v)
            done += len(part)
            if status_cb:
                status_cb(done=done, total=total, message='downloading prices')
            if done < total:
                time.sleep(pause)

    closes = closes.sort_index()
    volumes = volumes.sort_index()
    _atomic_csv(closes, closes_path, index_label='Date')
    _atomic_csv(volumes, vol_path, index_label='Date')
    return closes, volumes


def _read_wide(path: str) -> pd.DataFrame:
    if not os.path.exists(path):
        return pd.DataFrame()
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    return df.sort_index()


def _merge_wide(base: pd.DataFrame, new_cols: dict[str, pd.Series]) -> pd.DataFrame:
    if not new_cols:
        return base
    add = pd.DataFrame(new_cols)
    if base.empty:
        return add
    # fresh data wins on overlapping cells, old data kept elsewhere; one
    # aligned operation instead of 700 column inserts (fragmentation warning)
    return add.combine_first(base)


# ── derived metrics ──────────────────────────────────────────────────────────

def portfolio_daily_returns(hist_df: pd.DataFrame, mv_weights: pd.Series,
                            lookback: int = LOOKBACK) -> pd.Series | None:
    """Daily return series of the current portfolio, mirroring
    analytics.mpt_metrics semantics (ffill, pct_change, fillna(0), rets @ w).
    Weights are Market_Value per symbol (any spelling — normalized here).
    None when nothing usable."""
    if hist_df is None or hist_df.empty or mv_weights is None or mv_weights.empty:
        return None
    w = mv_weights.copy()
    w.index = [str(s).upper().replace('/', '-') for s in w.index]
    cols = [c for c in hist_df.columns if c in set(w.index)]
    if not cols:
        return None
    prices = hist_df[cols].tail(lookback + 1).ffill()
    rets = prices.pct_change().iloc[1:].fillna(0)
    w = w.loc[cols].astype(float)
    if w.sum() <= 0:
        return None
    w = w / w.sum()
    return (rets @ w).rename('portfolio')


def compute_metrics(closes: pd.DataFrame, volumes: pd.DataFrame,
                    port_rets: pd.Series | None, rf_annual: float) -> pd.DataFrame:
    """Per-symbol price-derived metrics over the trailing year. Pure — no
    network, no files. Reuses screener.perf_metrics/ema_trend so Sharpe,
    drawdown and trend flags mean exactly what the alerts' screener means."""
    from screener import ema_trend, perf_metrics

    rows = []
    spy = closes['SPY'].dropna() if 'SPY' in closes.columns else None
    spy_rets = spy.tail(LOOKBACK + 1).pct_change().dropna() if spy is not None else None

    for sym in closes.columns:
        s = closes[sym].dropna()
        row = {'Symbol': sym}
        if len(s) >= 2:
            win = s.tail(LOOKBACK)
            rets = win.pct_change().dropna()
            row['Ann_Vol'] = float(rets.std() * (252 ** 0.5)) if len(rets) > 20 else None
            if len(win) > 20:
                pm = perf_metrics(win, rf_annual)
                row['Sharpe_1yr'] = pm['Sharpe']
                row['Max_Drawdown'] = pm['Max_Drawdown']
            for label, days in (('Gain_3m', 63), ('Gain_6m', 126), ('Gain_1yr', 252)):
                if len(s) > days:
                    row[label] = round(float(s.iloc[-1] / s.iloc[-days - 1] - 1) * 100, 2)
            if len(win) >= 20:
                row['High_52w_Ratio'] = round(float(s.iloc[-1] / win.max()), 4)
            ema50, ema200, ok = ema_trend(s)
            row['Above_EMA50'] = bool(pd.notna(ema50) and s.iloc[-1] > ema50)
            row['Above_EMA200'] = bool(pd.notna(ema200) and s.iloc[-1] > ema200)
            row['Uptrend'] = bool(ok)
            if spy_rets is not None and sym != 'SPY':
                r = s.tail(LOOKBACK + 1).pct_change().dropna()
                joint = pd.concat([r, spy_rets], axis=1, join='inner').dropna()
                if len(joint) >= MIN_CORR_OVERLAP and joint.iloc[:, 1].var() > 0:
                    row['Beta'] = round(float(
                        joint.iloc[:, 0].cov(joint.iloc[:, 1]) / joint.iloc[:, 1].var()), 2)
            elif sym == 'SPY':
                row['Beta'] = 1.0
            if port_rets is not None:
                r = s.tail(LOOKBACK + 1).pct_change().dropna()
                joint = pd.concat([r, port_rets], axis=1, join='inner').dropna()
                if len(joint) >= MIN_CORR_OVERLAP:
                    row['Corr_Portfolio'] = round(
                        float(joint.iloc[:, 0].corr(joint.iloc[:, 1])), 3)
        if sym in volumes.columns:
            v = volumes[sym].dropna().tail(DOLLAR_VOL_WINDOW)
            pv = (closes[sym].reindex(v.index) * v).dropna()
            if len(pv):
                row['Avg_Dollar_Vol'] = float(pv.mean())
        rows.append(row)
    return pd.DataFrame(rows).set_index('Symbol')


# ── fundamentals (.info) cache ───────────────────────────────────────────────

INFO_FIELDS = ('quoteType', 'shortName', 'longName', 'sector', 'industry',
               'category', 'fundFamily', 'marketCap', 'trailingPE', 'forwardPE',
               'dividendYield', 'beta')


def extract_info_fields(info: dict) -> dict:
    """The subset of Ticker.info we keep, normalized to FRACTIONS.

    Yahoo mixes scale conventions per symbol (verified on the live cache
    2026-09-23): dividendYield is usually a fraction (KO 0.0239 = 2.39%) but
    percent for some symbols (SPY 0.98 = 0.98%, MSFT 0.79 = 0.79%), and
    netExpenseRatio is consistently a percent number (GLD 0.40 = 0.40%).
    Thresholds pick the only sane reading: a real yield above 25% or an ETF
    expense ratio above 2% effectively doesn't exist in this universe."""
    out = {k: info.get(k) for k in INFO_FIELDS}
    dy = out.get('dividendYield')
    if isinstance(dy, (int, float)) and dy > 0.25:
        out['dividendYield'] = dy / 100.0
    er = info.get('netExpenseRatio')
    if er is None:
        er = info.get('annualReportExpenseRatio')
    if isinstance(er, (int, float)) and er > 0.02:
        er = er / 100.0
    out['expenseRatio'] = er
    summary = info.get('longBusinessSummary') or info.get('description') or ''
    out['summary'] = str(summary)[:SUMMARY_MAX]
    return out


def load_info_cache(path: str) -> dict:
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def info_fetch_plan(cache: dict, symbols: list[str], full: bool,
                    max_age_days: float, max_n: int | None) -> list[str]:
    """Which symbols to fetch: always the ones missing from the cache; in
    full mode also the stalest entries past max_age. Oldest first, capped."""
    missing = [s for s in symbols if s not in cache]
    stale: list[tuple[str, str]] = []
    if full:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
        stale = sorted(
            ((cache[s].get('fetched_at') or '', s) for s in symbols
             if s in cache and (cache[s].get('fetched_at') or '') < cutoff))
    plan = missing + [s for _, s in stale]
    return plan[:max_n] if max_n else plan


def _default_info_fetcher(symbol: str) -> dict:
    import yfinance as yf
    return yf.Ticker(symbol).info or {}


def refresh_info(symbols: list[str], cache_path: str, sleep: float = 0.4,
                 fetcher=None, status_cb=None, flush_every: int = 50) -> dict:
    """Throttled, resumable .info pass. On failure the stale cache entry is
    kept and no failure placeholder is written (the classify_sectors lesson —
    a cached failure would never retry). `fetcher` is injectable for tests."""
    fetcher = fetcher or _default_info_fetcher
    cache = load_info_cache(cache_path)
    for i, sym in enumerate(symbols):
        info = None
        for attempt in (1, 2):
            try:
                info = fetcher(sym)
                break
            except Exception as e:
                if attempt == 1:
                    time.sleep(5)
                else:
                    print(f'WARNING: info fetch failed for {sym}: {e}')
        if info:
            entry = extract_info_fields(info)
            entry['fetched_at'] = datetime.now(timezone.utc).isoformat()
            cache[sym] = entry
        if status_cb:
            status_cb(done=i + 1, total=len(symbols), message='fetching fundamentals')
        if (i + 1) % flush_every == 0:
            _atomic_json(cache, cache_path)
        if i + 1 < len(symbols) and sleep:
            time.sleep(sleep)
    _atomic_json(cache, cache_path)
    return cache


# ── treasuries ───────────────────────────────────────────────────────────────

def fetch_treasury_yields(out_path: str) -> dict:
    """Latest treasury yields (Yahoo quotes these indexes as percent — same
    convention enrich.risk_free_rate relies on for ^IRX)."""
    import yfinance as yf
    out: dict = {}
    try:
        raw = yf.download(list(TREASURY_TICKERS), period='5d', progress=False,
                          auto_adjust=False, group_by='column')
        closes = raw['Close'] if isinstance(raw.columns, pd.MultiIndex) else raw[['Close']]
        for tkr, label in TREASURY_TICKERS.items():
            if tkr in closes.columns:
                s = closes[tkr].dropna()
                if len(s):
                    out[label] = round(float(s.iloc[-1]), 2)
    except Exception as e:
        print(f'WARNING: treasury fetch failed: {e}')
    if out:
        out['fetched_at'] = datetime.now(timezone.utc).isoformat()
        _atomic_json(out, out_path)
    return out


# ── merge ────────────────────────────────────────────────────────────────────

def build_universe(constituents: pd.DataFrame, metrics: pd.DataFrame,
                   info_cache: dict, out_path: str | None) -> pd.DataFrame:
    """Join constituents + price metrics + cached fundamentals into the one
    table the /discover page loads. Held/watchlist flags are deliberately NOT
    baked in — they are overlaid live at render time."""
    # Same sector vocabulary as the holdings pages (enrich.classify_sectors):
    # equities map through SECTOR_MAP, ETFs keyword-match their category.
    # Imported lazily — enrich pulls yfinance at module level.
    from enrich import ETF_SECTOR_KEYWORDS, SECTOR_MAP

    df = constituents.set_index('Symbol')
    df = df.join(metrics, how='left')

    info_rows = {}
    for sym in df.index:
        e = info_cache.get(sym) or {}
        qtype = e.get('quoteType') or ('ETF' if df.loc[sym, 'ETF_Tag'] else 'EQUITY')
        if qtype == 'EQUITY':
            raw_sector = e.get('sector') or ''
            sector = SECTOR_MAP.get(raw_sector, raw_sector)
        else:
            cat = e.get('category') or ''
            sector = next((v for k, v in ETF_SECTOR_KEYWORDS.items()
                           if k.lower() in cat.lower()), 'Broad Market')
        info_rows[sym] = {
            'Name': e.get('shortName') or e.get('longName') or '',
            'Quote_Type': qtype,
            'Sector': sector,
            'Industry': e.get('industry') or '',
            'Category': e.get('category') or '',
            'Fund_Family': e.get('fundFamily') or '',
            'MarketCap': e.get('marketCap'),
            'Trailing_PE': e.get('trailingPE'),
            'Forward_PE': e.get('forwardPE'),
            'Dividend_Yield': e.get('dividendYield'),
            'Expense_Ratio': e.get('expenseRatio'),
            'Summary': e.get('summary') or '',
            'Info_Age_Days': _age_days(e.get('fetched_at')),
        }
        if pd.isna(df.loc[sym].get('Beta')) and e.get('beta') is not None:
            df.loc[sym, 'Beta'] = e['beta']
    info_df = pd.DataFrame.from_dict(info_rows, orient='index')
    df = df.join(info_df, how='left')

    etf_mask = df['ETF_Tag'].fillna('') != ''
    df.loc[etf_mask & (df['Quote_Type'] == ''), 'Quote_Type'] = 'ETF'
    df['Asset_Class'] = [
        asset_class_from_tag(tag) if tag else 'Equity'
        for tag in df['ETF_Tag'].fillna('')]
    # Tags outrank Yahoo's often-empty ETF categories for sector purposes.
    tag = df['ETF_Tag'].fillna('')
    df.loc[tag.str.startswith('bond'), 'Sector'] = 'Fixed Income'
    df.loc[tag.str.startswith(('gold', 'commodity', 'crypto')), 'Sector'] = 'Commodities'
    df.loc[tag.str.startswith('real_estate'), 'Sector'] = 'Real Estate'
    df.loc[tag.str.startswith('intl'), 'Sector'] = 'International'

    df = df.reset_index()
    if out_path:
        _atomic_csv(df, out_path)
    return df


def load_universe(universe_dir: str = UNIVERSE_DIR) -> pd.DataFrame | None:
    path = os.path.join(universe_dir, UNIVERSE_FILE)
    if not os.path.exists(path):
        return None
    return pd.read_csv(path)


# ── status + small io helpers ────────────────────────────────────────────────

def write_status(path: str, **fields) -> dict:
    """Merge fields into the status JSON, atomically — the /discover page
    polls this while a refresh subprocess runs."""
    status = read_status(path) or {}
    status.update(fields)
    _atomic_json(status, path)
    return status


def read_status(path: str) -> dict | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _age_days(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return round((datetime.now(timezone.utc) - dt).total_seconds() / 86400, 1)
    except ValueError:
        return None


def _atomic_csv(df: pd.DataFrame, path: str, index_label: str | None = None) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    if index_label:
        df.to_csv(tmp, index_label=index_label)
    else:
        df.to_csv(tmp, index=False)
    os.replace(tmp, path)


def _atomic_json(obj, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)

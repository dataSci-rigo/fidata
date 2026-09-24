"""Portfolio news feed: per-position headlines prioritized by volatility x
position size, plus AI-curated market-wide stories.

Writes data/news_feed.json for the panel /positions card, the local viewer's
/news page, and the daily review's appended digest. Never sends Telegram
itself, and never runs as a script — run_pipeline.run() calls build_news_feed
on every run (ungated by market hours: stories break off-hours).

The seen-state (data/news_seen.json) is a FIRST-SEEN REGISTRY, not a
selection filter: the pages always show the current best fresh stories (a
3x/day pipeline must not churn the page by hiding this morning's top story),
while the once-daily Telegram digest includes only stories first seen today —
dedup applied exactly where repetition actually annoys.

anthropic is imported lazily (ai_screener.py pattern) so run_pipeline can
import this module with no API key; curation failure falls back to a
rule-based ranking and can never kill the pipeline.
"""
import difflib
import hashlib
import html
import json
import os
import re
from zoneinfo import ZoneInfo

import pandas as pd

_MODEL = os.getenv('FIDATA_COACH_MODEL', 'claude-sonnet-4-6')

MARKET_TICKERS = ['SPY', 'QQQ', 'IWM', 'TLT', 'GLD', 'USO']
MAX_AGE_HOURS = 48
SEEN_KEEP_DAYS = 14
CURATION_POOL_CAP = 40       # freshest N headlines offered to the model
TOP_POSITIONS = 20           # how many scored holdings get a news fetch
TITLE_TRUNC = 72             # digest line hygiene (phone-width)
TITLE_DUP_RATIO = 0.75       # near-identical headline threshold

# Company names come free from the discovery screener's cache; without them
# relevance matching is ticker-only, which misses most headlines (Yahoo writes
# "Apple", not "AAPL"). Optional — absent file just degrades the ranking.
NAMES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          'data', 'universe', 'info_cache.json')
_NAME_NOISE = {'inc', 'inc.', 'corp', 'corp.', 'corporation', 'co', 'co.',
               'company', 'ltd', 'ltd.', 'limited', 'plc', 'holdings',
               'holding', 'group', 'the', 'etf', 'trust', 'fund', 'series',
               'class', 'shares', 'nv', 'sa', 'ag', 'spa', 'ads', 'adr',
               'common', 'stock', 'plc.', 'international'}

_SYSTEM = """You curate market-wide financial news for one retail investor's
daily feed. From the numbered candidates, pick up to 10 DISTINCT stories that
plausibly move the whole market: Fed/rates, inflation prints, jobs data,
geopolitics, oil, broad index moves, mega-cap results with index-level
impact. Skip single-stock stories without market-wide implications. When
several headlines cover the same event, pick the single best one. Prefer
fresher stories. For each pick write one plain line (<=120 chars) on why it
matters to a diversified portfolio. No hedging, no disclaimers."""


class NewsCurationError(RuntimeError):
    pass


def _today_str(now=None) -> str:
    """LA-date string — shared by first_seen stamping and the daily digest so
    'today' means the same thing on both sides of the comparison."""
    ts = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz='America/Los_Angeles')
    if ts.tzinfo is None:
        ts = ts.tz_localize('America/Los_Angeles')
    return str(ts.tz_convert('America/Los_Angeles').date())


def _strip_html(text: str) -> str:
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', str(text or '')))).strip()


def _normalize_item(symbol: str, item: dict) -> dict | None:
    """One yfinance news item (nested 'content' schema, yf 1.2.x) -> flat
    story dict, or None when it lacks the essentials."""
    content = item.get('content') or item
    title = str(content.get('title') or '').strip()
    pub = content.get('pubDate') or content.get('providerPublishTime')
    if not title or not pub:
        return None
    provider = content.get('provider') or {}
    publisher = (provider.get('displayName') if isinstance(provider, dict)
                 else str(provider)) or ''
    canonical = content.get('canonicalUrl') or {}
    url = (canonical.get('url') if isinstance(canonical, dict)
           else content.get('link')) or ''
    summary = str(content.get('summary') or '').strip() or _strip_html(
        content.get('description'))
    story_id = item.get('id') or hashlib.sha1(
        (url or title).encode()).hexdigest()
    return {'id': str(story_id), 'symbol': symbol, 'title': title,
            'publisher': publisher, 'url': url, 'published_at': str(pub),
            'summary': summary[:400]}


def fetch_symbol_news(symbols: list[str]) -> tuple[list[dict], int]:
    """(stories, n_failed) — n_failed lets the caller tell 'no fresh news'
    from 'yfinance is down' (total failure keeps the previous artifact)."""
    import yfinance as yf
    stories: list[dict] = []
    n_failed = 0
    for sym in symbols:
        try:
            for item in (yf.Ticker(sym).news or []):
                story = _normalize_item(sym, item)
                if story:
                    stories.append(story)
        except Exception as e:
            n_failed += 1
            print(f'news: fetch failed for {sym}: {e}')
    return stories, n_failed


def filter_fresh(stories: list[dict], now=None,
                 max_age_hours: float = MAX_AGE_HOURS) -> list[dict]:
    now_utc = (pd.Timestamp(now) if now is not None
               else pd.Timestamp.now(tz='UTC'))
    if now_utc.tzinfo is None:
        now_utc = now_utc.tz_localize('UTC')
    out = []
    for s in stories:
        try:
            pub = pd.Timestamp(s['published_at'])
            if pub.tzinfo is None:
                pub = pub.tz_localize('UTC')
        except (ValueError, TypeError):
            continue
        if (now_utc - pub).total_seconds() <= max_age_hours * 3600:
            out.append(s)
    return out


def score_positions(combined: pd.DataFrame) -> pd.Series:
    """Ann_Vol x Market_Value per non-cash holding, descending — 'the
    positions where a headline hurts (or pays) the most'. NaN vol falls back
    to the median so a symbol without history still ranks by size."""
    eq = combined[combined.index != 'cash']
    if eq.empty:
        return pd.Series(dtype=float)
    vol = pd.to_numeric(eq.get('Ann_Vol'), errors='coerce')
    med = float(vol.median()) if vol.notna().any() else 1.0
    vol = vol.fillna(med)
    mv = pd.to_numeric(eq.get('Market_Value'), errors='coerce').fillna(0.0)
    return (vol * mv).sort_values(ascending=False)


def load_company_names(path: str = NAMES_FILE) -> dict:
    """{symbol: company name} from the discovery universe's info cache.
    Optional: {} when the screener has never run."""
    try:
        with open(path) as f:
            cache = json.load(f)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return {sym: (e.get('shortName') or e.get('longName') or '')
            for sym, e in cache.items() if isinstance(e, dict)}


def _name_tokens(symbol: str, names: dict | None) -> list[str]:
    tokens = [symbol.lower()]
    raw = (names or {}).get(symbol, '')
    for word in re.split(r'[^A-Za-z0-9&]+', str(raw).lower()):
        if len(word) >= 4 and word not in _NAME_NOISE:
            tokens.append(word)
    return tokens


def is_relevant(story: dict, symbol: str, names: dict | None = None) -> bool:
    """Is this story actually ABOUT the holding? Yahoo's per-ticker feed
    mixes in loosely-related market chatter, so a story naming the company
    outranks a newer one that merely showed up under its ticker."""
    text = f"{story.get('title', '')} {story.get('summary', '')}".lower()
    words = set(re.findall(r'[a-z0-9&]+', text))
    for token in _name_tokens(symbol, names):
        if token in words:
            return True
        # "Exxon" should match ExxonMobil, "Alphabet" match Alphabet Inc.
        if len(token) >= 5 and any(
                len(w) >= 5 and (token.startswith(w) or w.startswith(token))
                for w in words):
            return True
    return False


def select_position_stories(stories: list[dict], scores: pd.Series,
                            n: int = 5, max_per_symbol: int = 2,
                            names: dict | None = None) -> list[dict]:
    """Round-robin over symbols in score order: pass 1 takes each symbol's
    best story, pass 2 its next — diversity by construction (5 stories about
    one company would be a worse feed than 4 about four). Within a symbol,
    stories that actually name the company come first, then recency."""
    by_sym: dict[str, list[dict]] = {}
    for s in sorted(stories, key=lambda s: s['published_at'], reverse=True):
        by_sym.setdefault(s['symbol'], []).append(s)
    for sym, items in by_sym.items():
        items.sort(key=lambda s: not is_relevant(s, sym, names))   # stable
    ordered = [str(sym) for sym in scores.index if str(sym) in by_sym]
    picked: list[dict] = []
    for rank in range(max_per_symbol):
        for sym in ordered:
            if len(picked) >= n:
                return picked
            if len(by_sym[sym]) > rank:
                story = dict(by_sym[sym][rank])
                story['priority_score'] = round(float(scores[sym]), 2)
                picked.append(story)
    return picked


def _age_hours(story: dict, now=None) -> float:
    now_utc = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz='UTC')
    if now_utc.tzinfo is None:
        now_utc = now_utc.tz_localize('UTC')
    pub = pd.Timestamp(story['published_at'])
    if pub.tzinfo is None:
        pub = pub.tz_localize('UTC')
    return max(0.0, (now_utc - pub).total_seconds() / 3600)


def curate_market_stories(stories: list[dict], n: int = 10,
                          now=None) -> list[dict]:
    """One forced tool-use call picks the market-moving n with a why-line
    each. Raises NewsCurationError on any failure — caller falls back."""
    if not stories:
        return []
    pool = sorted(stories, key=lambda s: s['published_at'],
                  reverse=True)[:CURATION_POOL_CAP]
    lines = [f"{i}. [{s['symbol']}] {s['title']} — {s['publisher']}, "
             f"{_age_hours(s, now):.0f}h ago. {s['summary'][:200]}"
             for i, s in enumerate(pool)]
    schema = {
        'type': 'object', 'required': ['selections'],
        'properties': {'selections': {
            'type': 'array', 'maxItems': n,
            'items': {'type': 'object', 'required': ['index', 'why'],
                      'properties': {
                          'index': {'type': 'integer',
                                    'description': 'index from the candidate list'},
                          'why': {'type': 'string',
                                  'description': 'one line, <=120 chars: why this '
                                                 'moves the whole market'}}}}}}
    try:
        import anthropic
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=_MODEL, max_tokens=1024, system=_SYSTEM,
            messages=[{'role': 'user',
                       'content': f'Today is {_today_str(now)}.\n\nCANDIDATES:\n'
                                  + '\n'.join(lines)}],
            tools=[{'name': 'select_market_stories',
                    'description': 'Select the market-moving stories.',
                    'input_schema': schema}],
            tool_choice={'type': 'tool', 'name': 'select_market_stories'},
        )
        block = next((b for b in resp.content
                      if getattr(b, 'type', '') == 'tool_use'), None)
        if block is None:
            raise NewsCurationError('no tool_use block in response')
        selections = (block.input or {}).get('selections') or []
    except NewsCurationError:
        raise
    except Exception as e:
        raise NewsCurationError(str(e)) from e

    picked, seen_idx = [], set()
    for sel in selections:
        try:
            idx = int(sel.get('index'))
        except (TypeError, ValueError):
            continue
        if idx in seen_idx or not 0 <= idx < len(pool):
            continue
        seen_idx.add(idx)
        story = dict(pool[idx])
        story['why'] = str(sel.get('why') or '')[:160]
        picked.append(story)
        if len(picked) >= n:
            break
    if not picked:
        raise NewsCurationError('model selected nothing usable')
    return picked


def _norm_title(title: str) -> str:
    return re.sub(r'[^a-z0-9 ]', '', str(title).lower())


def dedup_by_title(stories: list[dict],
                   threshold: float = TITLE_DUP_RATIO) -> list[dict]:
    """Drop near-identical headlines, keeping the freshest. The same wire
    story surfaces under several tickers with slightly different titles, so
    id-dedup alone leaves visible duplicates; this runs on the pool BEFORE
    curation so the model never has to spend a pick collapsing them."""
    kept: list[dict] = []
    for s in sorted(stories, key=lambda s: s['published_at'], reverse=True):
        norm = _norm_title(s['title'])
        if any(difflib.SequenceMatcher(None, norm, _norm_title(k['title'])).ratio()
               > threshold for k in kept):
            continue
        kept.append(s)
    return kept


def fallback_market_stories(stories: list[dict], n: int = 10) -> list[dict]:
    """No AI: recency order with near-duplicate headlines removed."""
    return [dict(s, why='') for s in dedup_by_title(stories)[:n]]


def load_seen(seen_file: str) -> dict:
    if os.path.exists(seen_file):
        try:
            with open(seen_file) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def prune_seen(seen: dict, today: str, keep_days: int = SEEN_KEEP_DAYS) -> dict:
    cutoff = str((pd.Timestamp(today) - pd.Timedelta(days=keep_days)).date())
    return {k: v for k, v in seen.items() if str(v) >= cutoff}


def build_news_feed(combined: pd.DataFrame, feed_file: str, seen_file: str,
                    n_position: int = 5, n_market: int = 10,
                    now=None, names_file: str = NAMES_FILE) -> dict:
    """Fetch -> select -> curate -> stamp first_seen -> write feed_file."""
    scores = score_positions(combined)
    names = load_company_names(names_file)
    top_syms = [str(s) for s in scores.index[:TOP_POSITIONS]]
    pos_raw, pos_failed = fetch_symbol_news(top_syms)
    mkt_raw, mkt_failed = fetch_symbol_news(MARKET_TICKERS)

    total = len(top_syms) + len(MARKET_TICKERS)
    if pos_failed + mkt_failed >= total and total > 0:
        # yfinance is down, not "no news today" — keep the previous feed.
        if os.path.exists(feed_file):
            print('news: every fetch failed — keeping previous feed')
            with open(feed_file) as f:
                return json.load(f)
        return {}

    position_stories = select_position_stories(
        filter_fresh(pos_raw, now), scores, n=n_position, names=names)
    # A story under both a holding and SPY belongs to the position list only.
    picked_ids = {s['id'] for s in position_stories}
    picked_titles = {_norm_title(s['title']) for s in position_stories}
    mkt_fresh = dedup_by_title([s for s in filter_fresh(mkt_raw, now)
                                if s['id'] not in picked_ids
                                and _norm_title(s['title']) not in picked_titles])

    try:
        market_stories = curate_market_stories(mkt_fresh, n=n_market, now=now)
        curated = True
    except NewsCurationError as e:
        print(f'WARNING: news curation failed ({e}) — using fallback ranking')
        market_stories = fallback_market_stories(mkt_fresh, n=n_market)
        curated = False

    today = _today_str(now)
    seen = prune_seen(load_seen(seen_file), today)
    before = dict(seen)
    for story in position_stories + market_stories:
        story['first_seen'] = seen.setdefault(story['id'], today)
    if seen != before:
        with open(seen_file, 'w') as f:
            json.dump(seen, f, indent=2, sort_keys=True)

    feed = {'generated_at': str(pd.Timestamp.now(tz='America/Los_Angeles')),
            'curated': curated,
            'position_stories': position_stories,
            'market_stories': market_stories}
    with open(feed_file, 'w') as f:
        json.dump(feed, f, indent=2, default=str)
    return feed


def format_news_digest(feed: dict, today: str, panel_url: str) -> str:
    """Compact plain-text digest for the daily review Telegram message —
    only stories FIRST SEEN today (the pages show everything current; the
    once-daily message must not repeat yesterday's headlines). '' when
    nothing is new."""
    def trunc(text, limit):
        text = str(text).strip()
        return text if len(text) <= limit else text[:limit - 1] + '…'

    pos = [s for s in (feed or {}).get('position_stories') or []
           if s.get('first_seen') == today]
    mkt = [s for s in (feed or {}).get('market_stories') or []
           if s.get('first_seen') == today]
    if not pos and not mkt:
        return ''
    # Headlines only — the "why it matters" lines live on the pages, where
    # there is room for them. On a phone a title plus a truncated rationale
    # is two half-sentences and reads worse than the headline alone.
    lines = ['📰 News']
    if pos:
        lines.append('Positions:')
        lines.extend(f"  {s['symbol']} — {trunc(s['title'], TITLE_TRUNC)}"
                     for s in pos)
    if mkt:
        lines.append('Market:')
        lines.extend(f"  {trunc(s['title'], TITLE_TRUNC)}" for s in mkt)
    lines.append(f'Full feed: {panel_url}')
    return '\n'.join(lines)

"""news.py: schema normalization, scoring/selection, seen-state, curation
(+ fallback), artifact write, digest formatting. Zero network — yfinance and
anthropic are monkeypatched; every path comes from tmp_path."""
import json
import os
import sys
from types import SimpleNamespace

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import news  # noqa: E402

NOW = pd.Timestamp('2026-09-23T20:00:00Z')
TODAY = '2026-09-23'          # LA date for NOW


def item(sid, title, hours_ago=1, summary='sum', desc=None, url=None,
         provider='Reuters'):
    pub = (NOW - pd.Timedelta(hours=hours_ago)).strftime('%Y-%m-%dT%H:%M:%SZ')
    content = {'title': title, 'pubDate': pub, 'summary': summary,
               'provider': {'displayName': provider},
               'canonicalUrl': {'url': url or f'https://x/{sid}'}}
    if desc is not None:
        content['description'] = desc
    return {'id': sid, 'content': content}


def story(sid, symbol, title, hours_ago=1, **extra):
    s = news._normalize_item(symbol, item(sid, title, hours_ago))
    s.update(extra)
    return s


# ── normalization ────────────────────────────────────────────────────────────

def test_normalize_nested_schema():
    out = news._normalize_item('NVDA', item('abc', 'Nvidia ships chip'))
    assert out['id'] == 'abc' and out['symbol'] == 'NVDA'
    assert out['title'] == 'Nvidia ships chip'
    assert out['publisher'] == 'Reuters' and out['url'] == 'https://x/abc'
    assert out['published_at'].endswith('Z') and out['summary'] == 'sum'


def test_normalize_strips_html_when_no_summary():
    it = item('h1', 'T', summary='', desc='<p>Hello&amp; <b>world</b></p>')
    assert news._normalize_item('X', it)['summary'] == 'Hello& world'


def test_normalize_hash_id_fallback_and_missing_fields():
    it = item('x', 'T')
    del it['id']
    assert len(news._normalize_item('X', it)['id']) == 40      # sha1 hex
    bad = item('y', '')
    assert news._normalize_item('X', bad) is None
    nodate = {'id': 'z', 'content': {'title': 'T'}}
    assert news._normalize_item('X', nodate) is None


def test_fetch_survives_symbol_failure(monkeypatch):
    class FakeTicker:
        def __init__(self, sym):
            self.sym = sym

        @property
        def news(self):
            if self.sym == 'BOOM':
                raise RuntimeError('rate limited')
            return [item(f'{self.sym}1', f'{self.sym} story')]

    monkeypatch.setitem(sys.modules, 'yfinance',
                        SimpleNamespace(Ticker=FakeTicker))
    stories, n_failed = news.fetch_symbol_news(['AAA', 'BOOM', 'CCC'])
    assert n_failed == 1 and {s['symbol'] for s in stories} == {'AAA', 'CCC'}


def test_freshness_boundary():
    fresh = story('a', 'X', 'fresh', hours_ago=47)
    stale = story('b', 'X', 'stale', hours_ago=49)
    out = news.filter_fresh([fresh, stale], now=NOW)
    assert [s['id'] for s in out] == ['a']


# ── scoring / selection ──────────────────────────────────────────────────────

def _combined():
    return pd.DataFrame(
        {'Ann_Vol': [0.5, 0.1, None, 0.2],
         'Market_Value': [100_000, 500_000, 200_000, 1000]},
        index=pd.Index(['NVDA', 'KO', 'NOVOL', 'cash'], name='Symbol'))


def test_score_positions_nan_vol_and_cash():
    scores = news.score_positions(_combined())
    assert 'cash' not in scores.index
    assert scores['NVDA'] == pytest.approx(50_000)        # 0.5 * 100k
    assert scores['KO'] == pytest.approx(50_000)          # 0.1 * 500k
    # no Ann_Vol -> median of the others (0.3), so size still ranks it
    assert scores['NOVOL'] == pytest.approx(60_000)
    assert scores.index[0] == 'NOVOL'                     # sorted descending
    assert news.score_positions(pd.DataFrame()).empty


def test_select_diversity_cap_and_order():
    scores = pd.Series({'NVDA': 50_000.0, 'KO': 40_000.0, 'XOM': 10_000.0})
    stories = ([story(f'n{i}', 'NVDA', f'nvda {i}', hours_ago=i) for i in range(6)]
               + [story('k1', 'KO', 'ko one', hours_ago=1),
                  story('k2', 'KO', 'ko two', hours_ago=5)]
               + [story('x1', 'XOM', 'xom one', hours_ago=2)])
    picked = news.select_position_stories(stories, scores, n=5, max_per_symbol=2)
    assert len(picked) == 5
    # NVDA had 6 fresh stories but the cap keeps it to 2
    assert sum(1 for p in picked if p['symbol'] == 'NVDA') == 2
    assert [p['symbol'] for p in picked[:3]] == ['NVDA', 'KO', 'XOM']  # score order
    assert picked[0]['id'] == 'n0' and picked[3]['id'] == 'n1'   # newest first
    assert picked[0]['priority_score'] == 50_000.0


def test_is_relevant_matches_company_name_not_just_ticker():
    names = {'AAPL': 'Apple Inc.', 'XOM': 'ExxonMobil Holdings Corporation',
             'META': 'Meta Platforms, Inc.'}
    # Yahoo writes "Apple", never "AAPL" — ticker-only matching misses this
    assert news.is_relevant(story('a', 'AAPL', 'Apple unveils new iPhone'),
                            'AAPL', names)
    # prefix match in the other direction: "Exxon" -> ExxonMobil
    assert news.is_relevant(story('b', 'XOM', 'How high can Exxon go?'),
                            'XOM', names)
    # ticker in the summary still counts
    assert news.is_relevant(
        story('c', 'META', 'Big tech moves', summary='shares of META rose'),
        'META', names)
    assert not news.is_relevant(
        story('d', 'AAPL', 'Beloved travel brand closing after 22 years'),
        'AAPL', names)
    # no names file -> ticker-only, still works
    assert news.is_relevant(story('e', 'NVDA', 'NVDA stock in focus'), 'NVDA', {})


def test_select_prefers_relevant_story_over_newer_noise():
    scores = pd.Series({'AAPL': 1.0})
    names = {'AAPL': 'Apple Inc.'}
    noise = story('noise', 'AAPL', 'Qualcomm targets agentic AI', hours_ago=1)
    real = story('real', 'AAPL', 'Apple unveils new iPhone', hours_ago=6)
    picked = news.select_position_stories([noise, real], scores, n=1, names=names)
    assert [p['id'] for p in picked] == ['real']


def test_dedup_by_title_keeps_freshest():
    a = story('a', 'SPY', 'ETFs Lower, Equity Futures Mixed Pre-Bell', hours_ago=1)
    b = story('b', 'QQQ', 'ETFs Lower; Equity Futures Mixed Pre-Bell!', hours_ago=4)
    c = story('c', 'GLD', 'Gold hits record high', hours_ago=2)
    out = news.dedup_by_title([b, a, c])
    assert [s['id'] for s in out] == ['a', 'c']


def test_build_drops_market_story_duplicating_a_position_headline(tmp_path, monkeypatch):
    pos = story('p1', 'NVDA', 'Nvidia ships a new chip today', hours_ago=1)
    mkt_dup = story('m1', 'SPY', 'Nvidia ships a new chip today!', hours_ago=2)
    mkt_ok = story('m2', 'SPY', 'Fed holds rates steady', hours_ago=3)
    _patch_fetch(monkeypatch, pos=[pos], mkt=[mkt_dup, mkt_ok])
    monkeypatch.setattr(news, 'curate_market_stories',
                        lambda s, n=10, now=None: [dict(x, why='w') for x in s])
    feed = news.build_news_feed(_combined(), str(tmp_path / 'f.json'),
                                str(tmp_path / 's.json'), now=NOW,
                                names_file=str(tmp_path / 'none.json'))
    assert [s['id'] for s in feed['market_stories']] == ['m2']


def test_load_company_names(tmp_path):
    p = tmp_path / 'info_cache.json'
    p.write_text(json.dumps({'AAPL': {'shortName': 'Apple Inc.'},
                             'X': {'longName': 'X Corp'}, 'BAD': 'not a dict'}))
    assert news.load_company_names(str(p)) == {'AAPL': 'Apple Inc.', 'X': 'X Corp'}
    assert news.load_company_names(str(tmp_path / 'missing.json')) == {}


def test_select_prefers_diversity_over_filling_n():
    """Only one symbol has news -> 2 stories, not 5 about the same company."""
    scores = pd.Series({'NVDA': 50_000.0})
    stories = [story(f'n{i}', 'NVDA', f'nvda {i}', hours_ago=i) for i in range(6)]
    assert len(news.select_position_stories(stories, scores, n=5)) == 2


# ── curation ─────────────────────────────────────────────────────────────────

def _fake_anthropic(selections=None, raises=None, capture=None):
    class FakeMessages:
        def create(self, **kw):
            if capture is not None:
                capture.update(kw)
            if raises:
                raise raises
            return SimpleNamespace(content=[SimpleNamespace(
                type='tool_use', input={'selections': selections or []})])

    class FakeClient:
        def __init__(self):
            self.messages = FakeMessages()

    return SimpleNamespace(Anthropic=FakeClient)


def test_curate_validates_indices(monkeypatch):
    pool = [story(f'm{i}', 'SPY', f'story {i}', hours_ago=i) for i in range(3)]
    cap = {}
    monkeypatch.setitem(sys.modules, 'anthropic', _fake_anthropic(
        selections=[{'index': 0, 'why': 'fed'}, {'index': 0, 'why': 'dupe'},
                    {'index': 99, 'why': 'out of range'},
                    {'index': 'x', 'why': 'nonsense'},
                    {'index': 2, 'why': 'oil'}], capture=cap))
    out = news.curate_market_stories(pool, n=10, now=NOW)
    assert [s['id'] for s in out] == ['m0', 'm2']
    assert out[0]['why'] == 'fed'
    assert cap['tool_choice']['name'] == 'select_market_stories'
    assert 'CANDIDATES:' in cap['messages'][0]['content']


def test_curate_raises_on_api_error(monkeypatch):
    monkeypatch.setitem(sys.modules, 'anthropic',
                        _fake_anthropic(raises=RuntimeError('no key')))
    with pytest.raises(news.NewsCurationError):
        news.curate_market_stories([story('a', 'SPY', 't')], now=NOW)


def test_curate_raises_when_nothing_usable(monkeypatch):
    monkeypatch.setitem(sys.modules, 'anthropic',
                        _fake_anthropic(selections=[{'index': 42, 'why': 'x'}]))
    with pytest.raises(news.NewsCurationError):
        news.curate_market_stories([story('a', 'SPY', 't')], now=NOW)


def test_fallback_dedups_similar_titles():
    stories = [
        story('a', 'SPY', 'Fed holds rates steady, signals one cut', hours_ago=1),
        story('b', 'QQQ', 'Fed holds rates steady; signals one cut!', hours_ago=2),
        story('c', 'GLD', 'Gold hits record high', hours_ago=3),
    ]
    out = news.fallback_market_stories(stories, n=10)
    assert [s['id'] for s in out] == ['a', 'c']
    assert out[0]['why'] == ''


# ── seen state ───────────────────────────────────────────────────────────────

def test_prune_seen():
    seen = {'old': '2026-09-01', 'recent': '2026-09-20', 'today': TODAY}
    out = news.prune_seen(seen, TODAY, keep_days=14)
    assert set(out) == {'recent', 'today'}


def test_load_seen_missing_and_corrupt(tmp_path):
    assert news.load_seen(str(tmp_path / 'nope.json')) == {}
    p = tmp_path / 'bad.json'
    p.write_text('{oops')
    assert news.load_seen(str(p)) == {}


# ── build orchestration ──────────────────────────────────────────────────────

def _patch_fetch(monkeypatch, pos, mkt, pos_failed=0, mkt_failed=0):
    def fake(symbols):
        if list(symbols) == news.MARKET_TICKERS:
            return list(mkt), mkt_failed
        return list(pos), pos_failed
    monkeypatch.setattr(news, 'fetch_symbol_news', fake)


def test_build_writes_schema_and_stamps_first_seen(tmp_path, monkeypatch):
    feed_file = str(tmp_path / 'news_feed.json')
    seen_file = str(tmp_path / 'news_seen.json')
    _patch_fetch(monkeypatch,
                 pos=[story('p1', 'NVDA', 'nvda news')],
                 mkt=[story('m1', 'SPY', 'fed news')])
    monkeypatch.setattr(news, 'curate_market_stories',
                        lambda s, n=10, now=None: [{**s[0], 'why': 'matters'}])

    feed = news.build_news_feed(_combined(), feed_file, seen_file, now=NOW)
    assert feed['curated'] is True
    assert feed['position_stories'][0]['id'] == 'p1'
    assert feed['position_stories'][0]['priority_score'] > 0
    assert feed['market_stories'][0]['why'] == 'matters'
    for s in feed['position_stories'] + feed['market_stories']:
        assert s['first_seen'] == TODAY
        assert {'id', 'symbol', 'title', 'publisher', 'url',
                'published_at', 'summary'} <= set(s)
    on_disk = json.load(open(feed_file))
    assert on_disk['generated_at'] and on_disk == feed
    assert json.load(open(seen_file)) == {'p1': TODAY, 'm1': TODAY}

    # rerun a day later: first_seen must stay the ORIGINAL date
    later = NOW + pd.Timedelta(days=1)
    mtime = os.path.getmtime(seen_file)
    feed2 = news.build_news_feed(_combined(), feed_file, seen_file, now=later)
    assert feed2['position_stories'][0]['first_seen'] == TODAY
    assert os.path.getmtime(seen_file) == mtime      # unchanged -> not rewritten


def test_build_falls_back_when_curation_fails(tmp_path, monkeypatch):
    feed_file = str(tmp_path / 'f.json')
    _patch_fetch(monkeypatch, pos=[], mkt=[story('m1', 'SPY', 'fed news')])
    monkeypatch.setitem(sys.modules, 'anthropic',
                        _fake_anthropic(raises=RuntimeError('no key')))
    feed = news.build_news_feed(_combined(), feed_file,
                                str(tmp_path / 's.json'), now=NOW)
    assert feed['curated'] is False
    assert feed['market_stories'][0]['id'] == 'm1'
    assert feed['market_stories'][0]['why'] == ''


def test_build_keeps_previous_on_total_failure(tmp_path, monkeypatch):
    feed_file = str(tmp_path / 'f.json')
    previous = {'generated_at': 'yesterday', 'curated': True,
                'position_stories': [{'id': 'old'}], 'market_stories': []}
    with open(feed_file, 'w') as f:
        json.dump(previous, f)
    n_syms = len(_combined()) - 1 + len(news.MARKET_TICKERS)
    _patch_fetch(monkeypatch, pos=[], mkt=[], pos_failed=3, mkt_failed=6)
    assert 3 + 6 >= n_syms
    feed = news.build_news_feed(_combined(), feed_file,
                                str(tmp_path / 's.json'), now=NOW)
    assert feed == previous
    assert json.load(open(feed_file)) == previous     # untouched


def test_build_dedups_story_across_pools(tmp_path, monkeypatch):
    shared = story('dup', 'NVDA', 'shared headline')
    mkt_copy = {**shared, 'symbol': 'SPY'}
    _patch_fetch(monkeypatch, pos=[shared], mkt=[mkt_copy])
    monkeypatch.setattr(news, 'curate_market_stories',
                        lambda s, n=10, now=None: [dict(x, why='w') for x in s])
    feed = news.build_news_feed(_combined(), str(tmp_path / 'f.json'),
                                str(tmp_path / 's.json'), now=NOW)
    assert [s['id'] for s in feed['position_stories']] == ['dup']
    assert feed['market_stories'] == []               # removed from market pool


# ── digest ───────────────────────────────────────────────────────────────────

def test_digest_layout_and_today_filter():
    feed = {'position_stories': [
                {'symbol': 'NVDA', 'title': 'Nvidia ships chip', 'first_seen': TODAY},
                {'symbol': 'KO', 'title': 'Old news', 'first_seen': '2026-09-20'}],
            'market_stories': [
                {'title': 'Fed holds rates', 'why': 'repricing everywhere',
                 'first_seen': TODAY}]}
    out = news.format_news_digest(feed, TODAY, 'http://x/positions')
    # headlines only — the why-lines stay on the pages (phone readability)
    assert out == ('📰 News\n'
                   'Positions:\n'
                   '  NVDA — Nvidia ships chip\n'
                   'Market:\n'
                   '  Fed holds rates\n'
                   'Full feed: http://x/positions')
    assert 'Old news' not in out and 'repricing everywhere' not in out


def test_digest_empty_when_nothing_new():
    feed = {'position_stories': [{'symbol': 'X', 'title': 't',
                                  'first_seen': '2026-01-01'}],
            'market_stories': []}
    assert news.format_news_digest(feed, TODAY, 'u') == ''
    assert news.format_news_digest({}, TODAY, 'u') == ''
    assert news.format_news_digest(None, TODAY, 'u') == ''


def test_digest_truncates_long_titles():
    feed = {'position_stories': [{'symbol': 'X', 'title': 'y' * 200,
                                  'first_seen': TODAY}],
            'market_stories': []}
    line = news.format_news_digest(feed, TODAY, 'u').split('\n')[2]
    assert len(line) <= news.TITLE_TRUNC + 8 and line.endswith('…')


def test_today_str_is_la_date():
    # 03:00 UTC on the 24th is still the 23rd in Los Angeles
    assert news._today_str(pd.Timestamp('2026-09-24T03:00:00Z')) == '2026-09-23'

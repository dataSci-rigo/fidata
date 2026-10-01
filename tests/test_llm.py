"""llm.py provider shim — both paths driven with fakes, zero network."""
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ('FIDATA_LLM_PROVIDER', 'FIDATA_COACH_MODEL', 'OPEN_ROUTER'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(llm, '_anthropic_client', None)


class FakeResp:
    def __init__(self, status=200, payload=None, text=''):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._payload = payload
        self.text = text or str(payload)

    def json(self):
        if self._payload is None:
            raise ValueError('no json')
        return self._payload


def test_provider_and_model_defaults(monkeypatch):
    assert llm.provider() == 'anthropic'
    assert llm.model_name() == 'claude-sonnet-4-6'
    monkeypatch.setenv('FIDATA_LLM_PROVIDER', 'OpenRouter')      # case/space safe
    assert llm.provider() == 'openrouter'
    assert llm.model_name() == 'anthropic/claude-opus-5.5'
    monkeypatch.setenv('FIDATA_COACH_MODEL', 'anthropic/claude-sonnet-5')
    assert llm.model_name() == 'anthropic/claude-sonnet-5'
    assert llm.model_name('explicit/override') == 'explicit/override'


def test_openrouter_happy_path(monkeypatch):
    monkeypatch.setenv('FIDATA_LLM_PROVIDER', 'openrouter')
    monkeypatch.setenv('OPEN_ROUTER', "'sk-or-test'")            # quoted in .env
    sent = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        sent.update(url=url, headers=headers, json=json, timeout=timeout)
        return FakeResp(payload={'choices': [{'message': {'content': 'hi there'}}]})

    monkeypatch.setattr(llm.requests, 'post', fake_post)
    assert llm.ask('sys prompt', 'user text', max_tokens=123) == 'hi there'
    assert sent['url'] == llm.OPENROUTER_URL
    assert sent['headers']['Authorization'] == 'Bearer sk-or-test'   # quotes stripped
    assert sent['json']['model'] == 'anthropic/claude-opus-5.5'
    assert sent['json']['max_tokens'] == 123 + llm.REASONING_HEADROOM
    assert sent['json']['messages'] == [
        {'role': 'system', 'content': 'sys prompt'},
        {'role': 'user', 'content': 'user text'}]


def test_openrouter_surfaces_error_body(monkeypatch):
    """A wrong model slug 404s and the body names it — without this the
    failure is invisible."""
    monkeypatch.setenv('FIDATA_LLM_PROVIDER', 'openrouter')
    monkeypatch.setenv('OPEN_ROUTER', 'k')
    monkeypatch.setattr(llm.requests, 'post', lambda *a, **k: FakeResp(
        status=404, text='{"error":{"message":"No endpoint for anthropic/bogus"}}'))
    with pytest.raises(llm.LLMError) as e:
        llm.ask('s', 'u')
    assert '404' in str(e.value) and 'anthropic/bogus' in str(e.value)


def test_openrouter_requires_key(monkeypatch):
    monkeypatch.setenv('FIDATA_LLM_PROVIDER', 'openrouter')
    with pytest.raises(llm.LLMError, match='OPEN_ROUTER is not set'):
        llm.ask('s', 'u')


def test_openrouter_malformed_payload(monkeypatch):
    monkeypatch.setenv('FIDATA_LLM_PROVIDER', 'openrouter')
    monkeypatch.setenv('OPEN_ROUTER', 'k')
    monkeypatch.setattr(llm.requests, 'post',
                        lambda *a, **k: FakeResp(payload={'unexpected': 1}))
    with pytest.raises(llm.LLMError, match='unexpected OpenRouter response'):
        llm.ask('s', 'u')


def test_anthropic_path(monkeypatch):
    captured = {}

    class FakeMessages:
        def create(self, **kw):
            captured.update(kw)
            return SimpleNamespace(content=[SimpleNamespace(text='sdk reply')])

    class FakeClient:
        def __init__(self):
            self.messages = FakeMessages()

    monkeypatch.setitem(sys.modules, 'anthropic',
                        SimpleNamespace(Anthropic=FakeClient))
    assert llm.ask('s', 'u', max_tokens=50) == 'sdk reply'
    assert captured['model'] == 'claude-sonnet-4-6'
    assert captured['max_tokens'] == 50
    assert captured['system'] == 's'


def test_unknown_provider(monkeypatch):
    monkeypatch.setenv('FIDATA_LLM_PROVIDER', 'gemini')
    with pytest.raises(llm.LLMError, match='unknown FIDATA_LLM_PROVIDER'):
        llm.ask('s', 'u')


def test_transport_exception_becomes_llmerror(monkeypatch):
    monkeypatch.setenv('FIDATA_LLM_PROVIDER', 'openrouter')
    monkeypatch.setenv('OPEN_ROUTER', 'k')

    def boom(*a, **k):
        raise ConnectionError('dns is down')

    monkeypatch.setattr(llm.requests, 'post', boom)
    with pytest.raises(llm.LLMError, match='dns is down'):
        llm.ask('s', 'u')


def test_ai_review_wraps_llm_errors(monkeypatch):
    """ai_review._ask must keep raising ReviewError for its callers."""
    import ai_review
    monkeypatch.setattr(ai_review.llm, 'ask',
                        lambda *a, **k: (_ for _ in ()).throw(llm.LLMError('nope')))
    with pytest.raises(ai_review.ReviewError, match='nope'):
        ai_review._ask('s', 'u', 10)


def test_openrouter_sends_reasoning_effort_and_headroom(monkeypatch):
    """Reasoning tokens share the max_tokens budget, so the request must ask
    for more than the answer needs and cap the thinking."""
    monkeypatch.setenv('FIDATA_LLM_PROVIDER', 'openrouter')
    monkeypatch.setenv('OPEN_ROUTER', 'k')
    sent = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        sent.update(json or {})
        return FakeResp(payload={'choices': [{'message': {'content': 'ok'},
                                              'finish_reason': 'stop'}]})

    monkeypatch.setattr(llm.requests, 'post', fake_post)
    llm.ask('s', 'u', max_tokens=2000)
    assert sent['max_tokens'] == 2000 + llm.REASONING_HEADROOM
    assert sent['reasoning'] == {'effort': llm.REASONING_EFFORT}


def test_openrouter_empty_content_explains_itself(monkeypatch):
    """The real failure: all tokens went to reasoning, content came back
    null, and a None reached the section parser as an AttributeError."""
    monkeypatch.setenv('FIDATA_LLM_PROVIDER', 'openrouter')
    monkeypatch.setenv('OPEN_ROUTER', 'k')
    monkeypatch.setattr(llm.requests, 'post', lambda *a, **k: FakeResp(
        payload={'choices': [{'message': {'content': None,
                                          'reasoning': 'x' * 2247},
                              'finish_reason': 'length'}]}))
    with pytest.raises(llm.LLMError) as e:
        llm.ask('s', 'u')
    msg = str(e.value)
    assert 'no content' in msg and "'length'" in msg and '2247 chars' in msg

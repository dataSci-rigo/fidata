"""One-call LLM shim so the review jobs can run through either Anthropic
directly or OpenRouter, chosen by env rather than by code.

`FIDATA_LLM_PROVIDER=openrouter` routes the daily/weekly reviews through
OpenRouter (key `OPEN_ROUTER`, already in .env and used the same way by
todo_list/accountability_bot.py), which is what lets the automatic Sunday job
use an Opus-class model on that account. `anthropic` (the default) keeps the
original SDK path.

Both clients are created lazily: with provider=openrouter there is no reason
to require an ANTHROPIC_API_KEY, and importing this module must never fail
just because one of the two keys is absent.

NOTE: news.curate_market_stories deliberately does NOT come through here — it
uses Anthropic's forced `tool_choice`, whose request/response shape is
provider-specific.
"""
import os

import requests

OPENROUTER_URL = 'https://openrouter.ai/api/v1/chat/completions'
DEFAULT_MODELS = {
    'anthropic': 'claude-sonnet-4-6',
    'openrouter': 'anthropic/claude-opus-5.5',
}
TIMEOUT = 300

# Opus-class models on OpenRouter are reasoning models, and reasoning tokens
# come out of the SAME max_tokens budget as the answer. Measured on the real
# weekly-review prompt: with the defaults it spent 4222 tokens thinking, hit
# finish_reason='length' and returned content=null — which surfaced as an
# AttributeError deep inside the section parser. 'low' effort plus headroom
# gives a complete report for ~$0.08 instead of a truncated one for $0.14.
REASONING_EFFORT = (os.getenv('FIDATA_REASONING_EFFORT') or 'low').strip().lower()
REASONING_HEADROOM = 2000        # extra tokens on top of the answer budget


class LLMError(RuntimeError):
    pass


def provider() -> str:
    return (os.getenv('FIDATA_LLM_PROVIDER') or 'anthropic').strip().lower()


def model_name(override: str | None = None) -> str:
    """Explicit override > FIDATA_COACH_MODEL > provider default. The model id
    is provider-specific: 'claude-sonnet-4-6' for the SDK,
    'anthropic/claude-opus-5.5' for OpenRouter."""
    if override:
        return override
    env_model = (os.getenv('FIDATA_COACH_MODEL') or '').strip()
    return env_model or DEFAULT_MODELS.get(provider(), DEFAULT_MODELS['anthropic'])


_anthropic_client = None


def _ask_anthropic(system: str, user_message: str, max_tokens: int,
                   model: str) -> str:
    global _anthropic_client
    import anthropic
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic()
    resp = _anthropic_client.messages.create(
        model=model, max_tokens=max_tokens, system=system,
        messages=[{'role': 'user', 'content': user_message}])
    return resp.content[0].text


def _ask_openrouter(system: str, user_message: str, max_tokens: int,
                    model: str) -> str:
    key = (os.getenv('OPEN_ROUTER') or '').strip().strip("'\"")
    if not key:
        raise LLMError('OPEN_ROUTER is not set')
    body = {'model': model,
            'max_tokens': max_tokens + REASONING_HEADROOM,
            'messages': [{'role': 'system', 'content': system},
                         {'role': 'user', 'content': user_message}]}
    if REASONING_EFFORT not in ('', 'default'):
        body['reasoning'] = {'effort': REASONING_EFFORT}
    resp = requests.post(
        OPENROUTER_URL,
        headers={'Authorization': f'Bearer {key}',
                 'X-Title': 'fiData'},          # shows up in OpenRouter's log
        json=body, timeout=TIMEOUT)
    if not resp.ok:
        # Surface the body: a wrong model slug returns a 404 whose message
        # names the slug, which is otherwise invisible.
        raise LLMError(f'OpenRouter {resp.status_code}: {resp.text[:400]}')
    try:
        choice = resp.json()['choices'][0]
        content = choice['message'].get('content')
    except (KeyError, IndexError, ValueError) as e:
        raise LLMError(f'unexpected OpenRouter response: {resp.text[:400]}') from e
    if not content:
        # Don't hand None to a caller that expects text.
        reasoning = len(choice.get('message', {}).get('reasoning') or '')
        raise LLMError(
            f"{model} returned no content (finish_reason="
            f"{choice.get('finish_reason')!r}, {reasoning} chars of reasoning) — "
            f'the token budget was spent thinking; raise max_tokens or lower '
            f'FIDATA_REASONING_EFFORT')
    return content


def ask(system: str, user_message: str, max_tokens: int = 1000,
        model: str | None = None) -> str:
    """Single-turn completion. Raises LLMError on any provider failure."""
    name = model_name(model)
    which = provider()
    try:
        if which == 'openrouter':
            return _ask_openrouter(system, user_message, max_tokens, name)
        if which == 'anthropic':
            return _ask_anthropic(system, user_message, max_tokens, name)
        raise LLMError(f'unknown FIDATA_LLM_PROVIDER {which!r} '
                       f'(expected anthropic or openrouter)')
    except LLMError:
        raise
    except Exception as e:
        raise LLMError(f'{which} request failed: {e}') from e

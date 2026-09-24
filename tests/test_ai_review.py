"""ai_review section parsing + panel_url.

Regression cover for the weekly review silently producing four empty
sections: the model writes markdown headers ("## Rebalancing:"), while the
original parser only accepted a bare "Rebalancing:" line, so every section
came out empty and both the Telegram digest and the panel page showed
"(no content)" — with the job still exiting 0.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip('anthropic', reason='pip install -r requirements.txt')

from ai_review import split_sections  # noqa: E402
from app_data_io import panel_url  # noqa: E402

NAMES = ['Rebalancing', 'Sector Drift', 'Tax-Loss Harvesting', 'Watch List']


def test_parses_markdown_headers():
    text = ('# Weekly Portfolio Deep Review\n\n---\n\n'
            '## Rebalancing:\n\nTrim SPY.\n\n---\n\n'
            '## Sector Drift:\n\nIT is heavy.\n\n'
            '## Tax-Loss Harvesting:\n\nNothing to harvest.\n\n'
            '## Watch List:\n\nWatch INTC.\n')
    out = split_sections(text, NAMES)
    assert out['Rebalancing'] == 'Trim SPY.'
    assert out['Sector Drift'] == 'IT is heavy.'
    assert out['Tax-Loss Harvesting'] == 'Nothing to harvest.'
    assert out['Watch List'] == 'Watch INTC.'


@pytest.mark.parametrize('header', [
    'Rebalancing:', 'Rebalancing', '## Rebalancing:', '**Rebalancing**',
    '**Rebalancing:**', '### Rebalancing', '1. Rebalancing:', '- Rebalancing:',
    '__Rebalancing__', 'REBALANCING:',
])
def test_header_variants_all_recognized(header):
    out = split_sections(f'{header}\nbody text\n', NAMES)
    assert out['Rebalancing'] == 'body text'


def test_preamble_dropped_and_rules_stripped():
    out = split_sections('Intro prose nobody asked for.\n\n'
                         'Rebalancing:\n---\nkeep this\n***\n', NAMES)
    assert out['Rebalancing'] == 'keep this'
    assert 'Intro prose' not in ''.join(out.values())


def test_missing_section_is_empty_not_missing():
    out = split_sections('Rebalancing:\nonly one section\n', NAMES)
    assert set(out) == set(NAMES)
    assert out['Watch List'] == ''


def test_multiline_body_preserved():
    out = split_sections('Rebalancing:\nline one\n\nline two\nWatch List:\nx\n',
                         NAMES)
    assert out['Rebalancing'] == 'line one\n\nline two'


def test_panel_url_prefers_tailscale_over_localhost(monkeypatch):
    # These links are read on a phone: localhost points the phone at itself.
    monkeypatch.delenv('FIDATA_PANEL_URL', raising=False)
    monkeypatch.setenv('VM_TAILSCALE_IP', '100.79.128.124')
    monkeypatch.setenv('CONTROL_PANEL_PORT', '9000')
    assert panel_url() == 'http://100.79.128.124:9000'
    assert panel_url('positions') == 'http://100.79.128.124:9000/positions'
    assert panel_url('fidata/2026-09-27') == \
        'http://100.79.128.124:9000/fidata/2026-09-27'


def test_panel_url_explicit_override_and_legacy_suffix(monkeypatch):
    monkeypatch.setenv('FIDATA_PANEL_URL', 'http://example:8080')
    assert panel_url('positions') == 'http://example:8080/positions'
    # the old default carried the page path; don't double it up
    monkeypatch.setenv('FIDATA_PANEL_URL', 'http://example:8080/fidata')
    assert panel_url('positions') == 'http://example:8080/positions'


def test_panel_url_falls_back_to_localhost(monkeypatch):
    monkeypatch.delenv('FIDATA_PANEL_URL', raising=False)
    monkeypatch.delenv('VM_TAILSCALE_IP', raising=False)
    monkeypatch.setenv('CONTROL_PANEL_PORT', '9000')
    assert panel_url() == 'http://localhost:9000'

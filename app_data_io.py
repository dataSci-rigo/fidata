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
"""
import json
import os

DATA_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DATA = os.path.join(DATA_DIR, 'app_data')


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
    except (TypeError, ValueError):
        pass
    return str(val)


def pct_color(val) -> str:
    """Rich-markup-formatted % value (only viewer.py's CLI uses this)."""
    if val is None or val != val:
        return '—'
    color = 'green' if val >= 0 else 'red'
    return f'[{color}]{val:.2f}%[/{color}]'

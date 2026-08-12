import os

import pytest


@pytest.fixture
def fixtures_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures')

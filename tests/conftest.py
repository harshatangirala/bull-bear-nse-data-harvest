"""Shared pytest fixtures."""
from __future__ import annotations

import pytest

from .helpers import make_dao


@pytest.fixture()
def dao():
    return make_dao()

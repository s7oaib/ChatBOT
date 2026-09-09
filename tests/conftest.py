"""
Makes the repo root importable for the test suite.

Tests import the bot as `camellia.*` and the helper scripts as `scripts.*`,
both of which live at the repo root rather than beside the tests, so pytest
needs the root on sys.path regardless of where it was invoked from.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def isolate_process_session_cache(monkeypatch):
    """Normal tests must not restore state written by an earlier test run."""
    from camellia.session_memory import session_memory

    monkeypatch.setattr(session_memory, "persistence_enabled", False)
    session_memory._sessions.clear()
    yield
    session_memory._sessions.clear()

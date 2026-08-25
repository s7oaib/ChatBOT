"""
Makes the repo root importable for the test suite.

Tests import the bot as `camellia.*` and the helper scripts as `scripts.*`,
both of which live at the repo root rather than beside the tests, so pytest
needs the root on sys.path regardless of where it was invoked from.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

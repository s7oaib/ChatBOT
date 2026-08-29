"""
Compiled-once index of qa_templates.json trigger phrases.

qa_templates.json defines ~1,065 triggers, and both the deterministic matcher
and the RAG fallback check every one of them against every incoming message.
Building `\\b<trigger>\\b` and its word set inside those loops meant ~1,100
regex compiles and ~1,065 findall calls per message: Python's own regex cache
holds 512 entries, so with more triggers than that it thrashed and recompiled
nearly all of them every time. Profiling the live matcher put regex
compilation at 43% of total CPU.

Compiling each trigger once costs ~1,065 small pattern objects and drops that
loop from 23.8ms to 1.2ms per message. The cache is keyed by the trigger
string itself, so a live menu reload picks up new triggers with no
invalidation step, and it is shared by both callers so the same phrase is
never compiled twice.
"""

import re
from typing import Dict, FrozenSet, Pattern, Tuple

_INDEX: Dict[str, Tuple[Pattern, FrozenSet[str]]] = {}


def trigger_index(trigger_lower: str) -> Tuple[Pattern, FrozenSet[str]]:
    """
    Returns `(compiled \\b<trigger>\\b pattern, set of words in the trigger)`.

    `trigger_lower` must already be lowercased and stripped — the caller does
    that once and the result is what gets cached.
    """
    entry = _INDEX.get(trigger_lower)
    if entry is None:
        entry = (
            re.compile(r'\b' + re.escape(trigger_lower) + r'\b'),
            frozenset(re.findall(r'\b\w+\b', trigger_lower)),
        )
        _INDEX[trigger_lower] = entry
    return entry


def size() -> int:
    """Number of triggers compiled so far. Used by tests and /health."""
    return len(_INDEX)

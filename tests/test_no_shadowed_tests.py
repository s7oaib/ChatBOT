"""
A test defined twice in one class is a test that does not run.

`TestCamelliaBot.test_bot_identity_queries` existed twice. Python kept the
second and threw the first away at class-creation time, so the earlier body —
including two phrasings the survivor never checked — had never executed. The
suite reported it as passing because it counted the surviving copy.

Nothing warns about this: not pytest, not a linter that is not configured for
it, and not the pass count, which stays green while coverage silently drops.
"""
import ast
from collections import Counter
from pathlib import Path

import pytest

TESTS = sorted(Path(__file__).parent.glob("test_*.py"))


@pytest.mark.parametrize("path", TESTS, ids=lambda p: p.name)
def test_no_test_is_defined_twice_in_the_same_scope(path):
    """
    Checks classes and module level. A redefinition anywhere in one namespace
    deletes the earlier body, whichever scope it lives in.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))

    def _shadowed(body, scope):
        names = [
            node.name
            for node in body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        ]
        return [
            f"{scope}.{name} (defined {count} times)"
            for name, count in Counter(names).items()
            if count > 1
        ]

    problems = _shadowed(tree.body, path.name)
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            problems += _shadowed(node.body, f"{path.name}::{node.name}")

    assert not problems, (
        "these tests are silently dead — a later definition replaced them and "
        f"they never run: {problems}"
    )

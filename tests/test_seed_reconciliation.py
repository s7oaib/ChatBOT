"""
Repository template changes have to reach customers.

The volume copy of qa_templates.json used to be seeded once and never again,
so editing the repo file, committing, pushing and deploying changed nothing:
the deploy succeeded, /health said healthy, and customers went on reading the
original. A confirmed bento price sat undelivered that way for two days.

Reconciliation is deliberate rather than automatic — only a higher
seed_version replaces the live file — so an ordinary deploy still leaves
edits made through /admin alone.
"""
import json
import os

import pytest

from camellia import bootstrap


def _write(path, templates, seed_version=None, extra=None):
    data = {"templates": templates}
    if seed_version is not None:
        data["seed_version"] = seed_version
    data.update(extra or {})
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


SEED_T = [{"id": "a", "response": "seed copy"}]
LIVE_T = [{"id": "a", "response": "live copy"}]


def _run(monkeypatch, tmp_path, seed, live=None):
    """Runs the bootstrap against a temp seed and volume."""
    monkeypatch.setattr(bootstrap, "DATA_DIR", tmp_path / "image")
    target = tmp_path / "volume" / "qa_templates.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(bootstrap, "_SEEDED_FILES", (("QA_TEMPLATES_PATH", seed),))
    monkeypatch.setenv("QA_TEMPLATES_PATH", str(target))
    actions = bootstrap.prepare_writable_storage()
    return target, actions


def test_a_fresh_volume_is_seeded_and_stamped(monkeypatch, tmp_path):
    seed = _write(tmp_path / "seed.json", SEED_T, seed_version=1)
    target, actions = _run(monkeypatch, tmp_path, seed)

    live = json.loads(target.read_text(encoding="utf-8"))
    assert live["templates"] == SEED_T
    assert live["seed_version"] == 1
    assert live["seeded_from_hash"] == bootstrap.seed_fingerprint(seed)


def test_a_higher_seed_version_replaces_the_live_file(monkeypatch, tmp_path):
    """This is the fix: a bumped seed actually reaches customers."""
    seed = _write(tmp_path / "seed.json", SEED_T, seed_version=2)
    target = tmp_path / "volume" / "qa_templates.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    _write(target, LIVE_T, seed_version=1)

    monkeypatch.setattr(bootstrap, "DATA_DIR", tmp_path / "image")
    monkeypatch.setattr(bootstrap, "_SEEDED_FILES", (("QA_TEMPLATES_PATH", seed),))
    monkeypatch.setenv("QA_TEMPLATES_PATH", str(target))
    actions = bootstrap.prepare_writable_storage()

    live = json.loads(target.read_text(encoding="utf-8"))
    assert live["templates"] == SEED_T, "the bumped seed did not reach the live file"
    assert live["seed_version"] == 2
    assert any("seed_version 1 to 2" in a for a in actions)


def test_the_superseded_file_is_kept(monkeypatch, tmp_path):
    """An operator edit lost to a bump must still be recoverable."""
    seed = _write(tmp_path / "seed.json", SEED_T, seed_version=2)
    target = tmp_path / "volume" / "qa_templates.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    _write(target, LIVE_T, seed_version=1)

    monkeypatch.setattr(bootstrap, "DATA_DIR", tmp_path / "image")
    monkeypatch.setattr(bootstrap, "_SEEDED_FILES", (("QA_TEMPLATES_PATH", seed),))
    monkeypatch.setenv("QA_TEMPLATES_PATH", str(target))
    bootstrap.prepare_writable_storage()

    kept = list(target.parent.glob("qa_templates.superseded-*.json"))
    assert len(kept) == 1
    assert json.loads(kept[0].read_text(encoding="utf-8"))["templates"] == LIVE_T


def test_an_equal_seed_version_leaves_admin_edits_alone(monkeypatch, tmp_path):
    """An ordinary deploy must not throw away what the kitchen edited."""
    seed = _write(tmp_path / "seed.json", SEED_T, seed_version=1)
    target = tmp_path / "volume" / "qa_templates.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    _write(target, LIVE_T, seed_version=1)

    monkeypatch.setattr(bootstrap, "DATA_DIR", tmp_path / "image")
    monkeypatch.setattr(bootstrap, "_SEEDED_FILES", (("QA_TEMPLATES_PATH", seed),))
    monkeypatch.setenv("QA_TEMPLATES_PATH", str(target))
    bootstrap.prepare_writable_storage()

    assert json.loads(target.read_text(encoding="utf-8"))["templates"] == LIVE_T


def test_a_seed_without_a_version_never_touches_the_live_file(monkeypatch, tmp_path):
    """
    menu.json is the one that matters here: it is rewritten at runtime by menu
    uploads, so reconciling it would throw away the week the kitchen posted.
    """
    seed = _write(tmp_path / "seed.json", SEED_T)  # no seed_version
    target = tmp_path / "volume" / "qa_templates.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    _write(target, LIVE_T)

    monkeypatch.setattr(bootstrap, "DATA_DIR", tmp_path / "image")
    monkeypatch.setattr(bootstrap, "_SEEDED_FILES", (("QA_TEMPLATES_PATH", seed),))
    monkeypatch.setenv("QA_TEMPLATES_PATH", str(target))
    bootstrap.prepare_writable_storage()

    assert json.loads(target.read_text(encoding="utf-8"))["templates"] == LIVE_T


def test_the_shipped_seed_declares_a_version():
    """Without one the shipped file can never be delivered by a deploy."""
    seed = bootstrap.DATA_DIR / "qa_templates.json"
    assert bootstrap._seed_version(seed) is not None, (
        "data/qa_templates.json has no seed_version, so repo changes to it "
        "will silently never reach customers"
    )

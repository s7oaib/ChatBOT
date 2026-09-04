"""
/health has to say when the repository's templates are not what customers read.

Undelivered seed changes are invisible everywhere else: the deploy succeeds,
the process is healthy, and the bot answers confidently from an old file. The
only reason today's bento bug was found is that someone tested by hand.

An operator editing a template through /admin moves the live file too, and
that must stay silent — otherwise the warning cries wolf on the intended
workflow and gets ignored.
"""
import json

import pytest
from fastapi.testclient import TestClient

from camellia import bootstrap
from camellia.config import config
import camellia.database as db_module
from camellia.app import app

db_module.db.init_db()
client = TestClient(app)


@pytest.fixture
def live_file(tmp_path, monkeypatch):
    """A volume copy of the real seed, wired up as the live templates file."""
    seed = bootstrap.DATA_DIR / "qa_templates.json"
    data = json.loads(seed.read_text(encoding="utf-8"))
    data["seeded_from_hash"] = bootstrap.seed_fingerprint(seed)
    data["seed_version"] = bootstrap._seed_version(seed)
    target = tmp_path / "qa_templates.json"
    target.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config, "QA_TEMPLATES_PATH", str(target))
    return target


def _health():
    r = client.get("/health")
    assert r.status_code in (200, 503)
    return r.json()


def _drift_warnings(body):
    return [w for w in body.get("warnings", []) if "not being served" in w]


def test_an_in_sync_deployment_raises_no_drift_warning(live_file):
    body = _health()
    assert body["answering"]["template_seed"]["undelivered_seed_changes"] is False
    assert _drift_warnings(body) == []


def test_forced_drift_is_flagged(live_file):
    """
    The scenario this exists for, forced by hand: the repository's templates
    moved on and nothing delivered them.
    """
    data = json.loads(live_file.read_text(encoding="utf-8"))
    data["seeded_from_hash"] = "0000stale0000"  # as if seeded from an older repo file
    live_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    body = _health()

    assert body["answering"]["template_seed"]["undelivered_seed_changes"] is True
    warnings = _drift_warnings(body)
    assert len(warnings) == 1, body.get("warnings")
    assert "seed_version" in warnings[0]
    assert "/admin/templates" in warnings[0], "the warning must say how to fix it"


def test_an_admin_edit_does_not_raise_the_warning(live_file):
    """
    The false positive that would make this useless. Editing a template live
    changes the file but not the fingerprint of the seed it came from.
    """
    data = json.loads(live_file.read_text(encoding="utf-8"))
    data["templates"][0] = dict(data["templates"][0])
    data["templates"][0]["response"] = "edited by the kitchen through /admin"
    live_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    body = _health()

    assert body["answering"]["template_seed"]["undelivered_seed_changes"] is False
    assert _drift_warnings(body) == []


def test_the_block_is_reported_even_without_a_volume():
    """A deployment reading the seed directly is in sync by definition."""
    body = _health()
    assert "template_seed" in body["answering"]
    assert body["answering"]["template_seed"]["undelivered_seed_changes"] is False

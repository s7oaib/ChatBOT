"""
The manual backup step ahead of the Custom Q&A migration — nothing in this
repo can reach the Railway volume directly, so this endpoint is what makes
"back up production DB before migration" something that can actually be
done from here.
"""
import os

from fastapi.testclient import TestClient

from camellia import app as app_module
from camellia.config import config
from camellia.database import Database

client = TestClient(app_module.app)


def _auth():
    return {"X-Admin-Key": config.ADMIN_API_KEY, "X-Admin-Pin": config.ADMIN_API_KEY}


def test_backup_requires_admin_auth():
    resp = client.post("/admin/db-backup")
    assert resp.status_code in (401, 403)


def test_backup_copies_the_live_file(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "backup_source.db"))
    monkeypatch.setattr(app_module, "db", database)

    resp = client.post("/admin/db-backup", headers=_auth())
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "backed_up"
    assert os.path.exists(data["backup_path"])
    assert data["size_bytes"] > 0
    assert data["backup_path"] != data["source_path"]


def test_backup_fails_cleanly_when_the_file_is_missing(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "backup_source2.db"))
    monkeypatch.setattr(database, "db_path", str(tmp_path / "does_not_exist.db"))
    monkeypatch.setattr(app_module, "db", database)

    resp = client.post("/admin/db-backup", headers=_auth())
    assert resp.status_code == 404

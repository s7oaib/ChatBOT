"""Focused client mapping layout and classification-only Topic checks."""

from fastapi.testclient import TestClient

from camellia import app as app_module
from camellia.config import config
from camellia.database import Database


def test_mapping_ui_uses_one_source_description_and_manage_topics():
    page = TestClient(app_module.app).get("/admin").text
    modal = page[page.index('id="topicModal"'):page.index('id="manageTopicsModal"')]
    assert 'id="topicName"' in modal
    assert 'id="topicDescription"' not in modal
    assert 'id="topicDefaultMenu"' not in modal
    assert 'id="topicResponseOrder"' not in modal
    assert 'id="manageTopicsList"' in page
    assert "renderManageTopics()" in page
    assert "data-topic-edit" in page and "data-topic-delete" in page
    assert 'id="igEditTopicButton"' not in page
    assert 'id="igStoryEditTopicButton"' not in page
    assert 'id="igSourceDescription"' in page
    assert 'id="igStorySourceDescription"' in page
    assert 'id="igMediaDescription"' in page
    assert 'id="igMediaMenuSelected"' in page
    assert 'id="igStoryMenuSelected"' in page
    assert 'id="igMenuSelected"' in page
    assert 'id="igStoryDeleteSection"' in page
    story_picker = page[page.index('function populateInstagramStoryPickers()'):page.index('function onInstagramStoryTopicChange()')]
    assert "const selectedTopicId = topics?.value || '';" in story_picker
    assert "topics.value = selectedTopicId" in story_picker
    assert '.source-map-form' in page and '@media (max-width:660px)' in page
    assert 'id="igMediaAutoReply"' in page and 'id="igStoryActive"' in page


def test_named_topic_preserves_description_and_source_owns_new_copy(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "ui-topics.db"))
    monkeypatch.setattr("camellia.database.db", database)
    monkeypatch.setattr(app_module, "db", database)
    monkeypatch.setattr(config, "ADMIN_API_KEY", "ui-topic-key-123456")
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    owner_id = "17841455615744812"
    monkeypatch.setattr(config, "INSTAGRAM_ACCOUNT_ID", owner_id)
    monkeypatch.setattr(config, "INSTAGRAM_GRAPH_ACCOUNT_ID", owner_id)
    client = TestClient(app_module.app)
    headers = {"x-admin-key": config.ADMIN_API_KEY}
    created = client.post("/admin/instagram-topics", headers=headers, json={
        "name": "Bengali Bento", "description": "Approved classification description.",
    })
    assert created.status_code == 200, created.text
    topic = created.json()["topic"]
    assert topic["description"] == "Approved classification description."
    legacy = client.post("/admin/instagram-topics", headers=headers, json={
        "name": "Legacy Topic", "description": "Old internal Topic description",
    }).json()["topic"]
    renamed = client.patch(f"/admin/instagram-topics/{legacy['topic_id']}", headers=headers, json={"name": "Renamed Topic"})
    assert renamed.status_code == 200
    assert renamed.json()["topic"]["description"] == "Old internal Topic description"
    media = database.upsert_instagram_media({
        "instagram_media_id": "exact-ui-post", "media_type": "IMAGE",
        "permalink": "https://www.instagram.com/p/EXACTUI/", "shortcode": "EXACTUI",
        "owner_instagram_account_id": owner_id, "owner_username": "camellia.artisanal",
        "last_sync_generation": "verified-ui-test", "inventory_state": "CURRENT",
    })
    database.reconcile_instagram_media_inventory(owner_id, "verified-ui-test")
    menu = database.list_menu_catalogue()[0]
    mapped = client.put(f"/admin/instagram-media/{media['id']}/mapping", headers=headers, json={
        "topic_id": legacy["topic_id"], "source_description": "Only this post's description.",
        "menu_mode": "SELECTED", "menu_id": menu["menu_id"], "auto_reply_enabled": True,
    })
    assert mapped.status_code == 200, mapped.text
    assert mapped.json()["route"]["source_description"] == "Only this post's description."
    assert mapped.json()["route"]["menu_id"] == menu["menu_id"]
    assert mapped.json()["route"]["active"] is True
    detached = client.put(f"/admin/instagram-media/{media['id']}/mapping", headers=headers, json={
        "topic_id": legacy["topic_id"], "source_description": "Only this post's description.",
        "menu_mode": "NONE", "auto_reply_enabled": False,
    })
    assert detached.status_code == 200
    assert detached.json()["route"]["menu_id"] is None
    assert detached.json()["route"]["active"] is False
    assert database.get_menu_catalogue_entry(menu["menu_id"])["menu_id"] == menu["menu_id"]
    assert database.get_instagram_media(media["id"])["instagram_media_id"] == "exact-ui-post"
    assert database.get_instagram_topic(legacy["topic_id"])["description"] == "Old internal Topic description"

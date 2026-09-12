import json

from camellia.content_integrity import apply_safe_content_remediations, canonical_override_responses
from camellia.database import Database
from camellia.responder import DeterministicResponder


def _fixtures(tmp_path):
    with open("data/menu.json", encoding="utf-8") as handle:
        menu = json.load(handle)
    with open("data/qa_templates.json", encoding="utf-8") as handle:
        templates = json.load(handle)["templates"]
    database = Database(str(tmp_path / "integrity.db"))
    return database, menu, templates


def test_safe_remediation_creates_versioned_overrides_from_canonical_menu(tmp_path):
    database, menu, templates = _fixtures(tmp_path)
    actions = apply_safe_content_remediations(database, menu, templates)

    assert actions["weekly_menu_veg"] == "created"
    assert actions["weekly_menu_nonveg"] == "created"
    assert actions["combo_bento"] == "created"
    assert actions["bento_details"] == "created"
    assert actions["sugar_free_options"] == "created"
    veg = database.get_response_template_override("weekly_menu_veg")["response"]
    assert "Aloor Dom" in veg and "₹280" in veg and "Dhokar Dalna" not in veg
    bento = database.get_response_template_override("combo_bento")["response"]
    assert "Veg Bento" in bento and "₹400" in bento and "Tomato Aamsottwo Chatni" in bento
    sugar = database.get_response_template_override("sugar_free_options")["response"].lower()
    assert "glycemic" not in sugar and "diabetic-friendly" not in sugar
    assert database.get_response_template_override_versions("weekly_menu_veg")
    assert "glycemic" in database.get_response_template_override_versions("sugar_free_options")[0]["response"].lower()


def test_remediation_does_not_overwrite_client_authored_copy(tmp_path):
    database, menu, templates = _fixtures(tmp_path)
    database.save_response_template_override("weekly_menu_veg", "Client approved wording", "client")
    actions = apply_safe_content_remediations(database, menu, templates)
    assert actions["weekly_menu_veg"] == "skipped"
    assert database.get_response_template_override("weekly_menu_veg")["response"] == "Client approved wording"


def test_unsupported_health_copy_is_replaced_even_if_previously_overridden(tmp_path):
    database, menu, templates = _fixtures(tmp_path)
    database.save_response_template_override("sugar_free_options", "Low glycemic and diabetic-friendly", "client")
    actions = apply_safe_content_remediations(database, menu, templates)
    assert actions["sugar_free_options"] == "updated"
    assert "glycemic" not in database.get_response_template_override("sugar_free_options")["response"].lower()


def test_dynamic_bento_renderer_uses_managed_copy(tmp_path, monkeypatch):
    database, menu, templates = _fixtures(tmp_path)
    apply_safe_content_remediations(database, menu, templates)
    responder = DeterministicResponder()
    responder.menu_data = menu
    monkeypatch.setattr("camellia.responder.db", database)
    rendered, _images = responder.render_approved_template("bento_details")
    assert rendered == database.get_response_template_override("bento_details")["response"]


def test_neutral_sugar_free_copy_has_no_unsupported_health_claims(tmp_path):
    _database, menu, templates = _fixtures(tmp_path)
    desired = canonical_override_responses(menu)["sugar_free_options"].lower()
    for phrase in ("low glycemic", "lower glycemic", "diabetic-friendly", "sugar-safe", "healthier for diabetes"):
        assert phrase not in desired

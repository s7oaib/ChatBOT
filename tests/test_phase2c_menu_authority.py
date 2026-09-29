"""Every customer menu consumer must enforce approval, validity and pending state."""
import copy
import json
import shutil
from datetime import date

import pytest

from camellia import menu_catalogue, menu_dates, menu_images, menu_review, order_calculator
from camellia.config import config
from camellia.database import db
from camellia.rag_fallback import rag_fallback_engine
from camellia.responder import responder
from camellia.session_memory import session_memory
from camellia import weekly_savoury


@pytest.mark.parametrize("weeks,expected", [
    ([], False),
    ([{"week_of": "25 Sep - 30 Sep 2026", "is_published": False}], False),
    ([{"week_of": "Current Week", "is_published": True}], False),
    ([{"week_of": "1 Oct - 7 Oct 2026", "is_published": True}], False),
    ([{"week_of": "18 Sep - 24 Sep 2026", "is_published": True}], False),
    ([{"week_of": "31 Sep - 32 Sep 2026", "is_published": True}], False),
    ([{"week_of": "25 Sep - 30 Sep 2026", "is_published": True}], True),
])
def test_real_legacy_baseline_eligibility(authority, weeks, expected):
    authority.write_text(json.dumps({"weekly_menus": weeks}), encoding="utf-8")
    assert weekly_savoury.legacy_baseline_current() is expected


def test_routing_fixture_keeps_real_authority_checks(eligible_weekly_asset):
    assert weekly_savoury.legacy_baseline_current()
    assert not responder._weekly_menu_expired()
    assert menu_images.current_weekly_url() == eligible_weekly_asset
    path = config.MENU_JSON_PATH
    with open(path, encoding="utf-8") as stream:
        baseline = json.load(stream)
    baseline["weekly_menus"][-1]["is_published"] = False
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(baseline, stream)
    responder.menu_data = responder._load_json(path)
    assert not weekly_savoury.legacy_baseline_current()
    assert responder._weekly_menu_expired()
    assert menu_review.customer_blocked_keys() & set(weekly_savoury.LEGACY_WEEKLY_KEYS)
    assert menu_images.current_weekly_url() is None


@pytest.fixture
def authority(tmp_path, monkeypatch):
    path = tmp_path / "menu.json"
    shutil.copy(config.MENU_JSON_PATH, path)
    monkeypatch.setattr(config, "MENU_JSON_PATH", str(path))
    monkeypatch.setattr(db, "db_path", str(tmp_path / "authority.db"))
    monkeypatch.setattr(menu_dates, "today_ist", lambda: date(2026, 9, 27))
    db.init_db()
    for engine in (responder, rag_fallback_engine):
        monkeypatch.setattr(engine, "menu_path", str(path))
        monkeypatch.setattr(engine, "menu_data", copy.deepcopy(engine.menu_data))
    monkeypatch.setattr(order_calculator, "_weekly_cache", {"mtime": None, "table": {}})
    return path


def _approved(key, price=450, item="Chicken Kosha", start=None, end=None, active=1, image=None):
    row = {"item": item, "quantity": "250g", "price_inr": price, "category": "vegetarian", "description": "", "aliases": []}
    approval = {"rows": [row], "approved_at": "2026-09-25T10:00:00Z", "image_path": image}
    with db.get_connection() as conn:
        conn.execute("UPDATE menu_versions SET is_current=0 WHERE menu_key=?", (key,))
        cursor = conn.execute("INSERT INTO menu_versions(menu_key,snapshot_json,approval_json,is_current,valid_from,valid_until) VALUES(?,?,?,?,?,?)",
                              (key, "[]", json.dumps(approval), active, start, end))
        conn.commit()
        return cursor.lastrowid


def _pending(key, item="Chicken Kosha"):
    with db.get_connection() as conn:
        conn.execute("INSERT INTO menu_drafts(menu_key,draft_json,review_json) VALUES(?,'[]',?)",
                     (key, json.dumps({"status": "NEEDS_REVIEW", "created_at": "2026-09-28T10:00:00Z",
                                       "rows": [{"item": item, "price_inr": 999999}]})))
        conn.commit()


def test_shared_customer_versions_exclude_pending_without_hiding_admin_history(authority):
    version = _approved("aamar_mishti", item="Test sweet")
    _pending("aamar_mishti", "Test sweet")
    assert any(v["id"] == version for v in menu_review.current_versions())
    assert not menu_review.customer_versions()
    assert "aamar_mishti" in menu_review.customer_blocked_keys()
    assert db.get_menu_version(version)["approval_json"]


@pytest.mark.parametrize("start,end", [("2026-10-01", "2026-10-07"), ("2026-09-18", "2026-09-24")])
def test_custom_dated_versions_are_not_current_outside_window(authority, start, end):
    menu = menu_catalogue.create_menu("Festival menu", "Other", "Festival description", customer_topic="SAVOURY")
    _approved(menu["menu_id"], start=start, end=end)
    assert menu_review.current_versions() == []
    result = rag_fallback_engine.retrieve_approved_menu("Chicken Kosha price")
    assert not result.auto_response and result.contains_unapproved_facts


def test_current_replacement_price_beats_older_and_static_price(authority):
    _approved("weekly_vegetarian", 400, start="2026-09-18", end="2026-09-24", active=0)
    current = _approved("weekly_vegetarian", 450, start="2026-09-25", end="2026-09-30", active=0)
    result = rag_fallback_engine.retrieve_approved_menu("Chicken Kosha price")
    assert "₹450" in result.auto_response and "₹400" not in result.auto_response
    assert any(f"version:{current}" in ref for ref in result.source_refs)
    assert order_calculator.order_calculator._parse_single_clause("Chicken Kosha")["unit_price"] == 450


def test_pending_price_blocked_in_direct_rag_and_calculator(authority):
    _approved("weekly_vegetarian", start="2026-09-25", end="2026-09-30", active=0)
    _pending("weekly_vegetarian")
    result = rag_fallback_engine.retrieve("Chicken Kosha price", persist_candidate=False)
    assert result.contains_unapproved_facts and not result.source_chunks and not result.auto_response
    assert order_calculator.weekly_meal_entries() == {}
    assert order_calculator.order_calculator._parse_single_clause("Chicken Kosha") is None
    assert not menu_review.canonical_menu()["weekly_menus"][-1]["vegetarian"]


def test_expired_weekly_cannot_resurrect_static_chicken_price(authority):
    _approved("weekly_vegetarian", start="2026-09-18", end="2026-09-24", active=0)
    assert order_calculator.order_calculator._parse_single_clause("Chicken Kosha") is None
    assert not rag_fallback_engine.retrieve("Chicken Kosha price", persist_candidate=False).auto_response


def test_expired_legacy_week_cannot_resurrect_static_price_without_reviewed_timeline(authority):
    authority.write_text(json.dumps({"weekly_menus": [{"week_of": "18 Sep - 24 Sep 2026",
        "non_vegetarian": [{"item": "Chicken Kosha", "quantity": "250g", "price_inr": 400}]}]}), encoding="utf-8")
    assert menu_review.menu_question_blocked("Chicken Kosha price")
    assert order_calculator.order_calculator._parse_single_clause("Chicken Kosha") is None
    assert not rag_fallback_engine.retrieve_approved_menu("Chicken Kosha price").auto_response


def test_reviewed_sweet_price_and_approved_alias_replace_static_table(authority):
    _approved("aamar_mishti", price=777, item="Kanchagolla")
    item = order_calculator.order_calculator._parse_single_clause("Kanchagolla 250g")
    assert item["unit_price"] == 777
    assert order_calculator.order_calculator._parse_single_clause("Rose 250g") is None


def test_calculator_merge_fallback_cannot_resurrect_removed_sweet(authority):
    _approved("aamar_mishti", price=777, item="Kanchagolla")
    assert order_calculator.order_calculator.parse_and_calculate("order rose 250g") is None


def test_stale_cart_price_cannot_survive_pending_replacement(authority):
    _approved("aamar_mishti", item="Kanchagolla")
    _pending("aamar_mishti", "Kanchagolla")
    cart = [{"type": "sweet", "collection": "aamar_mishti", "flavour": "classic", "size": "250g",
             "quantity": 1, "unit_price": 400, "total_price": 400}]
    assert order_calculator.order_calculator.parse_and_calculate("add another 250g", existing_cart=cart) is None


def test_delivery_faq_exemption_does_not_hide_explicit_pending_price(authority):
    _approved("weekly_vegetarian", start="2026-09-25", end="2026-09-30", active=0)
    _pending("weekly_vegetarian")
    assert not menu_review.menu_question_blocked("Do you deliver?")
    assert menu_review.menu_question_blocked("Can you deliver Chicken Kosha, what's the price?")


@pytest.mark.parametrize("pending,inactive", [(True, False), (False, True)])
def test_customer_image_excludes_pending_or_inactive_menu(authority, pending, inactive):
    _approved("aamar_mishti", item="Test sweet", image="/uploads/menu-images/current.png")
    if pending:
        _pending("aamar_mishti", "Test sweet")
    if inactive:
        menu_catalogue.update_menu("aamar_mishti", active=False)
    assert menu_images.current_url("aamar_mishti", "/images/AAMAR%20MISHTI.jpeg") is None
    assert not menu_review.canonical_menu()["collections"]["aamar_mishti"]["products"]


def test_weekly_image_does_not_escape_pending_replacement(authority):
    _approved("weekly_vegetarian", start="2026-09-25", end="2026-09-30", active=0, image="/uploads/menu-images/current.png")
    _pending("weekly_vegetarian")
    assert menu_images.current_weekly_url() is None


def test_inactive_legacy_weekly_route_cannot_select_customer_image(authority):
    _approved("weekly_vegetarian", start="2026-09-25", end="2026-09-30", active=0, image="/uploads/menu-images/current.png")
    menu_catalogue.update_menu("weekly_vegetarian", active=False)
    assert menu_images.current_url("weekly_vegetarian") is None


def test_ambiguous_standing_current_versions_fail_closed(authority):
    _approved("aamar_mishti", item="Kanchagolla", price=450)
    other = _approved("aamar_mishti", item="Kanchagolla", price=777)
    with db.get_connection() as conn:
        conn.execute("UPDATE menu_versions SET is_current=1 WHERE menu_key='aamar_mishti' AND id<>?", (other,))
        conn.commit()
    assert menu_review.customer_versions() == []
    assert menu_review.menu_question_blocked("Kanchagolla price")
    assert not rag_fallback_engine.retrieve_approved_menu("Kanchagolla price").auto_response


def test_custom_pending_source_followup_hands_off_but_explicit_stable_faq_survives(authority):
    menu = menu_catalogue.create_menu("Festival menu", "Other", "Festival description", customer_topic="SAVOURY")
    _approved(menu["menu_id"])
    _pending(menu["menu_id"])
    facts = {"source_route": {"menu_id": menu["menu_id"], "menu_mode": "SELECTED"}}
    assert menu_review.menu_question_blocked("What's the price?", facts)
    assert not menu_review.menu_question_blocked("Do you deliver?", facts)
    assert not menu_review.menu_question_blocked("How do I pay for Home Table?", facts)
    session_memory.clear_session("phase2c-custom", "instagram")
    session_memory.get_facts("phase2c-custom", "instagram").update(facts)
    answer = responder.match_and_respond("What's the price?", "phase2c-custom", "instagram")
    assert answer[2] == "pending_menu_handoff" and answer[3] is None
    assert "999999" not in answer[0] and "450" not in answer[0]


@pytest.mark.parametrize("topic", ["subscription", "new_custom", "unrelated_topic"])
def test_current_conversation_does_not_regain_superseded_pending_source(authority, topic):
    old = menu_catalogue.create_menu("Old festival", "Other", "Old source description")
    _pending(old["menu_id"], "Old item")
    if topic == "new_custom":
        topic = menu_catalogue.create_menu("New festival", "Other", "Current description", customer_topic="SAVOURY")["menu_id"]
        _approved(topic, item="Current item")
    facts = {"last_topic": topic, "source_context_superseded": True,
             "source_route": {"menu_id": old["menu_id"], "menu_mode": "SELECTED"}}
    assert not menu_review.menu_question_blocked("What's the price?", facts)


def test_published_current_replacement_releases_pending_block(authority):
    _approved("aamar_mishti", item="Kanchagolla")
    draft = menu_review.extract("aamar_mishti", "admin", text="Veg:\nKanchagolla 250g ₹777")
    assert menu_review.menu_question_blocked("Kanchagolla price")
    monkey_current = menu_review.publish_approved_menu("aamar_mishti", draft["draft_id"], draft["revision"], "admin")
    assert monkey_current["status"] == "PUBLISHED"
    assert not menu_review.menu_question_blocked("Kanchagolla price")
    assert "₹777" in rag_fallback_engine.retrieve_approved_menu("Kanchagolla price").auto_response


def test_reviewed_template_price_cannot_be_overridden_by_old_response_copy(authority, monkeypatch):
    _approved("aamar_mishti", price=777, item="Kanchagolla")
    monkeypatch.setattr(db, "get_response_template_override", lambda *_: {"status": "ACTIVE", "response": "OLD PRICE ₹400"})
    template = responder._template_by_id("aamar_mishti_collection")
    assert "₹777" in template["response"] and "₹400" not in template["response"]


def test_materialized_templates_cannot_bypass_reviewed_price(authority, monkeypatch):
    _approved("aamar_mishti", price=777, item="Kanchagolla")
    override = {"status": "ACTIVE", "response": "OLD PRICE ₹400"}
    monkeypatch.setattr(db, "get_response_template_override", lambda *_: override)
    monkeypatch.setattr(db, "list_response_template_overrides", lambda: {"aamar_mishti_collection": override})
    template = next(row for row in responder._templates_with_overrides() if row["id"] == "aamar_mishti_collection")
    assert "₹777" in template["response"] and "₹400" not in template["response"]


def test_inactive_menu_with_approved_history_cannot_quote_template_price(authority):
    _approved("aamar_mishti", item="Kanchagolla")
    menu_catalogue.update_menu("aamar_mishti", active=False)
    template = responder._template_by_id("aamar_mishti_collection")
    assert "₹" not in template.get("response", "")


@pytest.mark.parametrize("state", ["pending", "upcoming", "expired", "superseded", "inactive"])
def test_direct_consumers_withhold_non_authoritative_custom_menu(authority, state):
    menu = menu_catalogue.create_menu("Audit menu", "Other", "Synthetic audit menu", customer_topic="SAVOURY")
    key = menu["menu_id"]
    start, end = {"upcoming": ("2026-10-01", "2026-10-07"),
                  "expired": ("2026-09-18", "2026-09-24")}.get(state, (None, None))
    version = _approved(key, item="Audit dish", start=start, end=end,
                        image="/uploads/menu-images/audit.png")
    if state == "pending":
        _pending(key, "Audit dish")
    if state == "inactive":
        menu_catalogue.update_menu(key, active=False)
    if state == "superseded":
        with db.get_connection() as conn:
            conn.execute("UPDATE menu_versions SET weekly_state='SUPERSEDED' WHERE id=?", (version,))
            conn.commit()
    result = rag_fallback_engine.retrieve("Audit dish price", persist_candidate=False)
    assert not result.auto_response and not result.source_chunks
    assert result.contains_unapproved_facts
    assert order_calculator.order_calculator._parse_single_clause("Audit dish") is None
    assert menu_images.current_url(key) is None


@pytest.mark.parametrize("today,allowed", [(date(2026, 9, 24), False),
    (date(2026, 9, 25), True), (date(2026, 9, 27), True),
    (date(2026, 9, 30), True), (date(2026, 10, 1), False)])
def test_custom_menu_inclusive_date_boundaries(authority, monkeypatch, today, allowed):
    menu = menu_catalogue.create_menu("Boundary menu", "Other", "Synthetic dated menu", customer_topic="SAVOURY")
    version = _approved(menu["menu_id"], item="Boundary dish",
        start="2026-09-25", end="2026-09-30", image="/uploads/menu-images/boundary.png")
    monkeypatch.setattr(menu_dates, "today_ist", lambda: today)
    assert any(v["id"] == version for v in menu_review.customer_versions()) is allowed
    result = rag_fallback_engine.retrieve_approved_menu("Boundary dish price")
    assert bool(result.auto_response) is allowed
    assert bool(order_calculator.order_calculator._parse_single_clause("Boundary dish")) is allowed
    assert bool(menu_images.current_url(menu["menu_id"])) is allowed


def test_standing_menu_dates_remain_optional_but_pending_withholds(authority):
    menu = menu_catalogue.create_menu("Standing audit", "Other", "Synthetic standing menu", customer_topic="SAVOURY")
    version = _approved(menu["menu_id"], item="Standing dish", image="/uploads/menu-images/standing.png")
    assert db.get_menu_version(version)["valid_from"] is None
    assert db.get_menu_version(version)["valid_until"] is None
    assert "₹450" in rag_fallback_engine.retrieve_approved_menu("Standing dish price").auto_response
    assert order_calculator.order_calculator._parse_single_clause("Standing dish")["unit_price"] == 450
    assert menu_images.current_url(menu["menu_id"])
    _pending(menu["menu_id"], "Standing dish")
    assert not rag_fallback_engine.retrieve_approved_menu("Standing dish price").auto_response
    assert order_calculator.order_calculator._parse_single_clause("Standing dish") is None
    assert menu_images.current_url(menu["menu_id"]) is None

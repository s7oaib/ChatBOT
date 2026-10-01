"""A replacement OCR draft cannot lend prices or current-menu facts to customers."""
import copy
import builtins
import io
import re
import shutil
import json
from pathlib import Path

import pytest
from PIL import Image

from camellia import approved_qa, client_qa, menu_review
from camellia.config import config
from camellia.database import db
from camellia.responder import _pending_menu_affects_question
from camellia.responder import _approved_chicken_kosha_price_question, ai_responder, rag_fallback_engine
from camellia.responder import responder
from camellia.session_memory import session_memory


@pytest.fixture
def menu_env(tmp_path, monkeypatch, current_legacy_baseline):
    menu_path = tmp_path / "menu.json"
    shutil.copy(config.MENU_JSON_PATH, menu_path)
    monkeypatch.setattr(config, "MENU_JSON_PATH", str(menu_path))
    monkeypatch.setattr(db, "db_path", str(tmp_path / "menu.db"))
    db.init_db()
    monkeypatch.setattr(responder, "menu_path", str(menu_path))
    monkeypatch.setattr(responder, "menu_data", copy.deepcopy(responder.menu_data))
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    yield
    for user in ("phase2b-price", "phase2b-faq", "phase2b-home"):
        session_memory.clear_session(user, "instagram")


def _draft(price=700):
    draft = menu_review.extract("aamar_mishti", "operator", text=f"Kanchagolla 250g ₹{price}")
    rows = copy.deepcopy(draft["rows"])
    for row in rows:
        row["category"] = "Sondesh"
    return menu_review.edit("aamar_mishti", draft["draft_id"], draft["revision"], rows, "reviewer")


def _weekly_draft():
    return menu_review.extract(
        menu_review.weekly_savoury.WEEKLY_KEY,
        "operator",
        text="NON-VEGETARIAN\nChicken Kosha 250g ₹750",
        weekly=True,
    )


def _approve(draft):
    return menu_review.publish_approved_menu("aamar_mishti", draft["draft_id"], draft["revision"], "approver")


def _reply(text, user="phase2b-price"):
    return responder.match_and_respond(text, user_id=user, channel="instagram")


def test_approved_price_without_pending_menu(menu_env):
    _approve(_draft(700))
    assert menu_review.pending_review_keys() == set()
    reply, matched, _, _ = _reply("How much is Kanchagolla 250g?")
    assert matched and "₹700" in reply


def test_newer_draft_blocks_old_approved_price(menu_env):
    _approve(_draft(700))
    _draft(800)
    reply, matched, route, images = _reply("How much is Kanchagolla 250g?")
    assert "aamar_mishti" in menu_review.pending_review_keys()
    assert not matched and route == "pending_menu_handoff" and images is None
    assert "₹700" not in reply and "₹800" not in reply
    assert "team confirm" in reply


def test_ocr_extracted_price_never_answers_before_approval(menu_env, monkeypatch):
    output = io.BytesIO()
    Image.new("RGB", (100, 100), "white").save(output, format="PNG")
    monkeypatch.setattr(menu_review.menu_parser, "parse_image_and_update_menu", lambda *_: {
        "status": "success", "parsed": {"norom_pak_sondesh": [
            {"item": "Kanchagolla", "quantity": "250g", "price_inr": 9999}
        ]},
    })
    draft = menu_review.extract("aamar_mishti", "operator", data=output.getvalue(), mime="image/png")
    assert draft["status"] == "NEEDS_REVIEW"
    reply, matched, route, _ = _reply("Kanchagolla price?")
    assert not matched and route == "pending_menu_handoff" and "9999" not in reply
    assert menu_review.current_versions() == []


@pytest.mark.parametrize("source", ["Kanchagolla 250g", "Kanchagolla 250g ₹uncertain"])
def test_incomplete_price_stays_in_review_and_handoffs(menu_env, source):
    draft = menu_review.extract("aamar_mishti", "operator", text=source)
    assert draft["status"] == "NEEDS_REVIEW"
    reply, matched, route, _ = _reply("Kanchagolla price?")
    assert not matched and route == "pending_menu_handoff" and "team confirm" in reply


def test_new_approval_releases_pending_gate(menu_env):
    _approve(_draft(700))
    replacement = _draft(800)
    assert _reply("Kanchagolla price?")[2] == "pending_menu_handoff"
    _approve(replacement)
    assert menu_review.pending_review_keys() == set()
    reply, matched, route, _ = _reply("How much is Kanchagolla 250g?")
    assert matched and route != "pending_menu_handoff" and "₹800" in reply


@pytest.mark.parametrize("question", [
    "What is the price of Moonlight Pastry?",
    "How much is Moonlight Pastry?",
    "What does Moonlight Pastry cost?",
    "Moonlight Pastry price?",
])
def test_unknown_price_without_approved_fact_handoffs(menu_env, question):
    reply, matched, route, _ = _reply(question)
    assert not matched and route == "unknown_price_handoff", (reply, route)
    assert "₹" not in reply


def test_pending_sondesh_does_not_block_separate_home_table_answer(menu_env):
    _draft(800)
    reply, matched, route, _ = _reply("What is the Home Table price?", user="phase2b-home")
    assert matched and route != "pending_menu_handoff" and reply


@pytest.mark.parametrize("question", ["Is delivery available?", "Do you deliver?", "Is pickup available?"])
def test_stable_delivery_faq_continues_while_weekly_menu_pending(menu_env, question):
    _weekly_draft()
    reply, matched, route, _ = _reply(question, user="phase2b-faq")
    assert route != "pending_menu_handoff", (reply, route)
    assert not _pending_menu_affects_question(question, {})
    assert reply


def test_approved_delivery_answer_survives_unrelated_weekly_pending_draft(menu_env):
    _weekly_draft()
    reply, matched, route, _ = _reply("Do you deliver to xyz city?", user="phase2b-faq")
    assert matched and route != "pending_menu_handoff" and reply


def test_subscription_price_is_not_blocked_by_unrelated_weekly_draft(menu_env):
    _weekly_draft()
    _, _, route, _ = _reply("What is the subscription price?", user="phase2b-faq")
    assert route != "pending_menu_handoff"


def test_current_weekly_menu_request_is_blocked_while_weekly_replacement_pending(menu_env):
    _weekly_draft()
    reply, matched, route, _ = _reply("Show me the weekly menu", user="phase2b-faq")
    assert not matched and route == "pending_menu_handoff"
    assert "team confirm" in reply


def test_current_weekly_menu_request_keeps_existing_behavior_without_pending_draft(menu_env):
    reply, matched, route, _ = _reply("Show me the weekly menu", user="phase2b-faq")
    assert route != "pending_menu_handoff"
    assert matched and reply, (reply, route)


@pytest.mark.parametrize("question", [
    "What is the current price of Chicken Kosha?",
    "Is Chicken Kosha available?",
    "Is Chicken Kosha available this week?",
])
def test_pending_weekly_menu_blocks_volatile_fact_for_its_current_item(menu_env, question):
    _weekly_draft()
    reply, matched, route, _ = _reply(question, user="phase2b-price")
    assert not matched and route == "pending_menu_handoff"
    assert "₹" not in reply


@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize("question", [
    "How much is chicken kosha",
    "How much is chicken kosha?",
    "What is the price of chicken kosha?",
    "Chicken kosha price?",
    "Chicken kosha rate?",
])
def test_active_approved_chicken_price_outranks_pending_weekly_menu(menu_env, monkeypatch, question, pending):
    if pending:
        _weekly_draft()

    def unexpected_provider(*args, **kwargs):
        raise AssertionError("Approved price reached Groq/RAG")

    monkeypatch.setattr(ai_responder, "_query_groq", unexpected_provider)
    monkeypatch.setattr(rag_fallback_engine, "retrieve", unexpected_provider)
    monkeypatch.setattr(rag_fallback_engine, "retrieve_approved_menu", unexpected_provider)
    reply, matched, route, _ = _reply(question)
    facts = session_memory.get_facts("phase2b-price", "instagram")
    approved = client_qa.effective_entry("chicken_kosha_price_client")
    assert matched and route != "pending_menu_handoff", (reply, route, facts.get("last_grounding"), facts.get("last_route_source"))
    assert facts["client_qa_id"] == approved.qa_id
    assert reply.startswith(approved.answer.split("\n\n", 1)[0])
    assert "₹400" in reply and "₹750" not in reply
    assert facts["last_route_source"] == "EXACT_APPROVED"
    assert not facts["last_llm_called"]
    if pending:
        assert all(block.get("asset_key") != "weekly_menu" for block in facts["ordered_response_blocks"])


@pytest.mark.parametrize("question", [
    "Is Chicken Kosha on this week's menu?",
    "Do you have Chicken Kosha in stock?",
    "Is Chicken Kosha sold out?",
])
def test_explicit_chicken_availability_keeps_pending_menu_protection(menu_env, question):
    _weekly_draft()
    reply, matched, route, _ = _reply(question)
    assert not matched and route == "pending_menu_handoff"
    assert "₹" not in reply


@pytest.mark.parametrize("inactive", ["archived", "expired", "scheduled"])
def test_inactive_approved_chicken_price_cannot_bypass_pending_menu(menu_env, inactive):
    _weekly_draft()
    qa_id = "chicken_kosha_price_client"
    approved_qa.archive_or_restore(qa_id, "isolated-test")
    if inactive != "archived":
        with db.get_connection() as conn:
            column = "valid_until" if inactive == "expired" else "valid_from"
            value = "2000-01-01T00:00:00+00:00" if inactive == "expired" else "2099-01-01T00:00:00+00:00"
            conn.execute(f"UPDATE approved_qa_overrides SET status='ACTIVE', {column}=? WHERE qa_id=?", (value, qa_id))
            conn.commit()
    question = "How much is chicken kosha?"
    assert client_qa.match(question, {}) is None
    assert not _approved_chicken_kosha_price_question(question, {})
    reply, matched, route, _ = _reply(question)
    assert not matched and route == "approved_qa_unavailable"
    assert "₹400" not in reply and "₹750" not in reply


def test_historical_menu_question_is_not_made_current_by_pending_week(menu_env):
    _weekly_draft()
    _, _, route, _ = _reply("What was last week's weekly menu?", user="phase2b-faq")
    assert route != "pending_menu_handoff"


@pytest.mark.parametrize("status", ["EXTRACTING", "NEEDS_REVIEW", "READY_TO_APPROVE", "FAILED"])
def test_unresolved_review_status_remains_pending_until_operator_resolution(menu_env, status):
    draft = _draft(800)
    with db.get_connection() as conn:
        stored = menu_review.get_draft("aamar_mishti", conn)
        stored["status"] = status
        menu_review._store(conn, stored, "operator")
        conn.commit()
    assert "aamar_mishti" in menu_review.pending_review_keys()


@pytest.mark.parametrize("status", ["PUBLISHED", "REJECTED"])
def test_terminal_review_status_is_not_pending(menu_env, status):
    _draft(800)
    with db.get_connection() as conn:
        stored = menu_review.get_draft("aamar_mishti", conn)
        stored["status"] = status
        menu_review._store(conn, stored, "operator")
        conn.commit()
    assert "aamar_mishti" not in menu_review.pending_review_keys()


def test_stable_faq_continues_while_draft_pending(menu_env):
    _weekly_draft()
    reply, matched, route, _ = _reply("How do I order?", user="phase2b-faq")
    assert matched and route != "pending_menu_handoff" and reply


def test_extraction_exception_keeps_reviewable_draft_and_safe_diagnostic(menu_env, monkeypatch):
    output = io.BytesIO()
    Image.new("RGB", (100, 100), "white").save(output, format="PNG")

    def fail_with_private_detail(*_args):
        raise RuntimeError("private provider payload and token")

    monkeypatch.setattr(menu_review.menu_parser, "parse_image_and_update_menu", fail_with_private_detail)
    draft = menu_review.extract("aamar_mishti", "operator", data=output.getvalue(), mime="image/png")
    assert draft["status"] == "NEEDS_REVIEW"
    assert draft["rows"] == []
    assert draft["diagnostic_codes"] == ["EXTRACTION_EXCEPTION"]
    assert draft["diagnostic_details"] == {"exception_type": "RuntimeError"}
    assert "private provider payload" not in json.dumps(draft)
    assert menu_review.get_draft("aamar_mishti")["status"] == "NEEDS_REVIEW"
    assert menu_review.current_versions() == []

    reply, matched, route, _ = _reply("Kanchagolla price?")
    assert not matched and route == "pending_menu_handoff"
    assert "₹" not in reply
    assert menu_review.current_versions() == []


def test_benchmark_artifacts_are_unreachable_from_runtime_menu_knowledge(menu_env, monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[1]
    frozen_source_hashes = {
        "f758836253cf875c86aa76a0ad8c4f488f78e81d40fdd0d3d94c7d5d3c9b3836",
        "f7b4ccacfc1cec4532106c54f81bf24dd9c97e5c762b7e705762e867388b513f",
    }
    forbidden_source = re.compile(
        r"camellia_gate2b_|f0[12].{0,80}(?:truth|manifest)|"
        r"(?:truth|manifest).{0,80}f0[12]|benchmark.{0,80}(?:response|fixture)|"
        r"(?:response|fixture).{0,80}benchmark|(?:appdata|/tmp|\\temp\\).{0,80}benchmark",
        re.I,
    )
    for source in (root / "camellia").rglob("*.py"):
        content = source.read_text(encoding="utf-8", errors="ignore")
        assert not forbidden_source.search(content), source
        assert not frozen_source_hashes.intersection(re.findall(r"\b[a-f0-9]{64}\b", content, re.I)), source

    # These historical artifacts coexist beside the isolated runtime menu file;
    # they are decoys only and must never enter responder provenance.
    artifact_dir = Path(config.MENU_JSON_PATH).parent
    artifacts = [
        artifact_dir / "camellia_gate2b_f01_truth_manifest.json",
        artifact_dir / "camellia_gate2b_f02_truth_manifest.json",
        artifact_dir / "camellia_gate2b_f01_response.json",
        artifact_dir / "camellia_gate2b_f02_response.json",
        artifact_dir / "F01_historical_flyer.jpeg",
        artifact_dir / "F02_historical_flyer.jpeg",
    ]
    for artifact in artifacts:
        artifact.write_text("BENCHMARK_ONLY_SENTINEL_91357", encoding="utf-8")

    opened = []
    original_open = builtins.open
    original_path_open = Path.open

    def track_file(file, *args, **kwargs):
        if Path(file).resolve() in {path.resolve() for path in artifacts}:
            opened.append(Path(file))
        return original_open(file, *args, **kwargs)

    def track_path(path, *args, **kwargs):
        if path.resolve() in {item.resolve() for item in artifacts}:
            opened.append(path)
        return original_path_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", track_file)
    monkeypatch.setattr(Path, "open", track_path)

    approved = _approve(_draft(700))
    canonical = menu_review.canonical_menu()
    assert approved["status"] == "PUBLISHED"
    assert "700" in json.dumps(canonical)
    approved_reply, approved_match, approved_route, _ = _reply("How much is Kanchagolla 250g?")
    assert approved_match and approved_route != "pending_menu_handoff"
    assert "₹700" in approved_reply
    replacement = _draft(91357)
    canonical_while_pending = menu_review.canonical_menu()
    assert "700" in json.dumps(canonical_while_pending)
    assert "91357" not in json.dumps(canonical_while_pending)

    reply, matched, route, _ = _reply("How much is Kanchagolla 250g?")
    assert not matched and route == "pending_menu_handoff"
    assert "91357" not in reply and "BENCHMARK_ONLY_SENTINEL_91357" not in reply
    assert menu_review.get_draft("aamar_mishti")["draft_id"] == replacement["draft_id"]
    assert opened == []
    # Legitimate overlapping production facts/names are not prohibited by the
    # isolation check; only benchmark provenance is excluded.
    assert "Mango (Aam Ada)" in json.dumps(menu_review.canonical_menu())

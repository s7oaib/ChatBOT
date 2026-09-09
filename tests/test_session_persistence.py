"""Active conversation state survives process-local cache loss."""

import json
import time

from camellia.database import Database
from camellia.session_memory import SessionMemoryManager


def test_session_round_trip_and_clear(tmp_path):
    database = Database(str(tmp_path / "sessions.db"))
    first = SessionMemoryManager(database=database, persistence_enabled=True)
    first.extract_and_update_facts("u1", "whatsapp", "My name is Priya")
    first.get_facts("u1", "whatsapp")["awaiting_area_confirmation"] = True
    first.add_turn("u1", "whatsapp", "Do you deliver?", "Which area?")

    restarted = SessionMemoryManager(database=database, persistence_enabled=True)
    restored = restarted.get_session("u1", "whatsapp")
    assert restored["facts"]["name"] == "Priya"
    assert restored["facts"]["awaiting_area_confirmation"] is True
    assert restored["turns"][-1]["content"] == "Which area?"

    restarted.clear_session("u1", "whatsapp")
    assert SessionMemoryManager(database=database, persistence_enabled=True).get_session(
        "u1", "whatsapp"
    )["turns"] == []


def test_expired_persisted_session_is_not_restored(tmp_path):
    database = Database(str(tmp_path / "expired.db"))
    stale = SessionMemoryManager._new_session(time.time() - 200000)
    stale["facts"]["name"] = "Old Name"
    stale["facts"]["awaiting_area_confirmation"] = True
    database.save_active_session("u2", "instagram", stale, stale["last_updated"])

    restored = SessionMemoryManager(database=database, persistence_enabled=True).get_session(
        "u2", "instagram"
    )
    assert restored["facts"]["name"] == "Old Name"
    assert restored["facts"]["awaiting_area_confirmation"] is False


def test_jolbhora_menu_prices_match_order_tables():
    from camellia.config import config
    from camellia.order_calculator import PRICING_TABLE

    menu = json.loads(open(config.MENU_JSON_PATH, encoding="utf-8").read())
    for collection, calculator_key in (
        ("aamar_mishti", "aamar_mishti"),
        ("signature_collection", "signature"),
    ):
        product = next(
            item for item in menu["collections"][collection]["products"]
            if item["item"] == "Jolbhora"
        )
        actual = {row["pack_size"]: row["price_inr"] for row in product["prices"]}
        assert actual == PRICING_TABLE[calculator_key]["jolbhora"]

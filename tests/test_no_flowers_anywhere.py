"""
The client asked for flowers gone from everything their people see.

That is not only the reply path. 28 of 32 pending candidate templates in
production carried a flower, because the model drafting them was never told
not to and nothing stripped them before saving — so staff met them in the
approval queue on the client's own dashboard.
"""

from unittest.mock import patch

import pytest

from camellia.database import db
from camellia.text_rules import FLOWER_RE, strip_flower_emoji


@pytest.mark.parametrize("flower", ["🌸", "🌺", "🌻", "🌼", "🌹", "🌷", "💐", "🏵"])
def test_every_flower_the_client_named_is_removed(flower):
    assert strip_flower_emoji(f"Hello {flower} there") == "Hello there"
    assert not FLOWER_RE.search(strip_flower_emoji(f"{flower}"))


def test_the_surrounding_space_goes_with_it():
    # Removing the emoji alone left a double space mid-sentence.
    assert strip_flower_emoji("Both! 🌸 Our weekly meals") == "Both! Our weekly meals"


def test_price_tables_are_not_reflowed():
    # Several replies are ASCII tables whose columns are runs of spaces.
    table = "| Classic / Rose        | ₹450  | ₹600  |"
    assert strip_flower_emoji(table) == table


def test_nested_captions_are_cleaned_too():
    cards = [{"url": "/a.jpg", "caption": "Aamar Mishti 🌸"}]
    assert strip_flower_emoji(cards) == [{"url": "/a.jpg", "caption": "Aamar Mishti"}]


class TestTheApprovalQueue:
    def test_a_drafted_candidate_is_stored_without_flowers(self):
        candidate_id = db.save_candidate_template(
            name="flower_test",
            response="Hi there! 🌸 Our Sondesh is lovely 🌺",
            user_message="tell me about sondesh",
        )

        with db.get_connection() as conn:
            stored = conn.execute(
                "SELECT response FROM candidate_templates WHERE id = ?", (candidate_id,)
            ).fetchone()["response"]

        assert not FLOWER_RE.search(stored)
        assert "Our Sondesh is lovely" in stored

    def test_rows_saved_before_the_rule_existed_are_cleaned_on_the_way_out(self):
        # The 28 already in production were written without the strip, so the
        # dashboard has to clean them on read rather than wait for a migration.
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO candidate_templates (name, status, user_message, response) "
                "VALUES ('legacy_flower', 'PENDING_APPROVAL', 'q', 'Legacy 🌸 answer')"
            )
            conn.commit()

        pending = db.get_pending_candidates()
        legacy = [c for c in pending if c["name"] == "legacy_flower"]

        assert legacy, "the legacy row should still be listed"
        assert not FLOWER_RE.search(legacy[0]["response"])


class TestTheModelIsToldNotTo:
    def test_both_generating_prompts_forbid_flowers(self):
        from camellia.ai_responder import ai_responder

        with patch.object(ai_responder, "_query_groq", return_value=("", False, "x")) as q, \
             patch.object(ai_responder, "get_active_provider", return_value="groq"):
            ai_responder.generate_response("anything")
            drafting_prompt = q.call_args.args[0]

            ai_responder.answer_from_retrieved_context("anything", ["an excerpt"])
            grounded_prompt = q.call_args.args[0]

        assert "flower emoji" in drafting_prompt
        assert "flower emoji" in grounded_prompt

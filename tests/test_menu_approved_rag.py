import pytest

from camellia.rag_fallback import rag_fallback_engine
from camellia.responder import responder
from test_menu_review_approval import review_env, create_draft, ready, approve


@pytest.mark.parametrize("query", ["How much is Kanchagolla?", "Kanchagolla price?",
    "What does the Kanchagolla cost?", "Price for Kanchagolla please"])
def test_approved_menu_end_to_end_paraphrases(review_env, query):
    approve(ready(create_draft()))
    result = rag_fallback_engine.retrieve(query)
    assert result.auto_response == "Kanchagolla 250g is ₹650."
    assert result.candidate_id is None
    assert result.source_refs[0].startswith("approved_menu:aamar_mishti:version:")
    answer = responder.match_and_respond(query, user_id="menu-review-e2e", channel="instagram")
    assert "₹650" in answer[0]


@pytest.mark.parametrize("claim", ["Kanchagolla 250g is ₹750.", "Kanchagolla 500g is ₹1200.",
    "Kanchagolla 250g is ₹650, available today.", "Kanchagolla is gluten free.",
    "Kanchagolla has free delivery.", "Kanchagolla contains pistachios.", "Call 1234567890.",
    "Kanchagolla is delivered in Gurgaon.", "Kanchagolla 650g is ₹250."])
def test_generated_claims_fail_grounding(review_env, claim):
    approve(ready(create_draft()))
    result = rag_fallback_engine.retrieve("Kanchagolla price?")
    assert not rag_fallback_engine.menu_grounding_report(claim, result.source_chunks)[0]


@pytest.mark.parametrize("query", ["Is Kanchagolla available today?", "Is Kanchagolla gluten free?",
    "Kanchagolla price and free delivery?", "Kanchagolla 500g price?", "Kanchagolla price in Gurgaon?"])
def test_unsupported_menu_intent_handoff(review_env, query):
    approve(ready(create_draft()))
    assert not rag_fallback_engine.retrieve(query).auto_response
    answer = responder.match_and_respond(query, user_id="menu-review-unsupported", channel="instagram")[0]
    assert "₹650" not in answer
    assert "available today" not in answer.lower()


def test_unapproved_draft_never_retrieved_then_publish_hot_reload(review_env):
    draft = ready(create_draft("Test Sandesh ₹999"))
    query = "How much is Test Sandesh?"
    assert "999" not in rag_fallback_engine.retrieve(query, persist_candidate=False).auto_response
    assert "999" not in responder.match_and_respond(query, user_id="draft-only", channel="instagram")[0]
    approve(draft)
    assert "999" in responder.match_and_respond(query, user_id="now-approved", channel="instagram")[0]

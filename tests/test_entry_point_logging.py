"""
Phase 1 instrumentation: record how each Instagram message was composed and
which ad it arrived from.

The question these answer is the client's: fifty people asked whether we
deliver outside Delhi NCR and then named cities inside it. Either Instagram is
offering that as a tappable prompt or they typed it, and the logs could not
tell the two apart because every entry point was flattened into one text field.
"""

from unittest.mock import patch

from camellia import app
from camellia.ad_gate import describe_entry_point
from camellia.database import db
from camellia.session_memory import session_memory


class TestEntryPointClassification:
    def test_a_typed_message_is_recorded_as_typed(self):
        entry = describe_entry_point(
            {"sender": {"id": "u1"}, "message": {"mid": "m1", "text": "do you deliver to gurgaon"}}
        )
        assert entry.source == "typed"
        assert entry.from_ad_click is False

    def test_metas_own_ice_breaker_tap_is_identified(self):
        entry = describe_entry_point(
            {
                "sender": {"id": "u1"},
                "postback": {
                    "mid": "m1",
                    "title": "Do you deliver outside Delhi NCR?",
                    "payload": "ICE_BREAKER_01",
                },
            }
        )
        assert entry.source == "ice_breaker"
        assert entry.raw_payload == "ICE_BREAKER_01"

    def test_an_unrecognised_button_is_still_recorded_as_a_tap(self):
        # Not typing, but not a shape we recognise either. It must not be
        # silently filed as typed — that is the distinction being measured.
        entry = describe_entry_point(
            {"sender": {"id": "u1"}, "postback": {"title": "See menu", "payload": "MENU_BTN"}}
        )
        assert entry.source == "postback"
        assert entry.raw_payload == "MENU_BTN"

    def test_an_ad_click_carries_its_id_and_title(self):
        entry = describe_entry_point(
            {
                "sender": {"id": "u1"},
                "message": {
                    "text": "is this available",
                    "referral": {
                        "source": "ADS",
                        "ad_id": "120210000",
                        "ads_context_data": {"ad_title": "Fresh Bengali Bento — Delhi NCR"},
                    },
                },
            }
        )
        assert entry.source == "typed"
        assert entry.from_ad_click is True
        assert entry.ad_id == "120210000"
        assert entry.ad_title == "Fresh Bengali Bento — Delhi NCR"

    def test_an_ad_click_without_context_data_still_gives_the_ad_id(self):
        # ads_context_data is the unverified half of the assumption; the ad id
        # is not. Missing context must not lose the id too.
        entry = describe_entry_point(
            {
                "sender": {"id": "u1"},
                "message": {"text": "price?", "referral": {"source": "ADS", "ad_id": "999"}},
            }
        )
        assert entry.ad_id == "999"
        assert entry.ad_title is None

    def test_a_story_reply_is_distinguished_from_a_normal_message(self):
        entry = describe_entry_point(
            {
                "sender": {"id": "u1"},
                "message": {"text": "want this", "reply_to": {"story": {"id": "s1"}}},
            }
        )
        assert entry.source == "story_reply"

    def test_an_event_with_nothing_usable_is_unknown_not_typed(self):
        assert describe_entry_point({"sender": {"id": "u1"}}).source == "unknown"


class TestConversationLogging:
    def test_the_entry_point_is_persisted_with_the_message(self):
        db.log_conversation(
            "ep_user_1", "instagram", "do you deliver outside delhi ncr", "reply", True, "delivery_area",
            message_source="ice_breaker", ad_id="ad-7", ad_title="Sondesh Gift Boxes",
        )

        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT message_source, ad_id, ad_title FROM conversations "
                "WHERE user_id = 'ep_user_1' ORDER BY id DESC LIMIT 1"
            ).fetchone()

        assert row["message_source"] == "ice_breaker"
        assert row["ad_id"] == "ad-7"
        assert row["ad_title"] == "Sondesh Gift Boxes"

    def test_callers_that_do_not_pass_an_entry_point_still_work(self):
        # WhatsApp and the web tester log without one, and the rows already on
        # the production volume have none either.
        db.log_conversation("ep_user_2", "whatsapp", "hi", "hello", True, "greeting")

        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT message_source FROM conversations WHERE user_id = 'ep_user_2' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()

        assert row["message_source"] is None


class TestBreakdown:
    def test_repeated_questions_are_grouped_by_how_they_arrived(self):
        for sender in ("ep_tap_1", "ep_tap_2", "ep_tap_3"):
            db.log_conversation(
                sender, "instagram", "Do you deliver outside Delhi NCR?", "r", True,
                "delivery_area", message_source="ice_breaker",
            )
        db.log_conversation(
            "ep_typed_1", "instagram", "can u deliver to my area in noida", "r", True,
            "delivery_area", message_source="typed",
        )

        report = db.entry_point_breakdown(channel="instagram", days=3650)
        sources = {row["source"]: row for row in report["by_source"]}

        assert sources["ice_breaker"]["senders"] >= 3
        assert sources["typed"]["senders"] >= 1

        ncr = [
            q for q in report["repeated_questions"]
            if q["message"] == "do you deliver outside delhi ncr?"
        ]
        assert ncr, "the repeated question should be surfaced"
        assert ncr[0]["source"] == "ice_breaker"
        assert ncr[0]["senders"] >= 3

    def test_a_question_one_person_repeats_is_not_reported_as_a_pattern(self):
        # Three sends from one annoyed customer is not fifty people being shown
        # a prompt, so the report groups on distinct senders rather than volume.
        for _ in range(3):
            db.log_conversation(
                "ep_solo", "instagram", "hello is anyone there please", "r", False,
                None, message_source="typed",
            )

        report = db.entry_point_breakdown(channel="instagram", days=3650)
        assert not [
            q for q in report["repeated_questions"]
            if q["message"] == "hello is anyone there please"
        ]

    def test_ad_titles_are_counted_so_we_know_if_meta_sends_them(self):
        for sender in ("ep_ad_1", "ep_ad_2"):
            db.log_conversation(
                sender, "instagram", "is this available", "r", True, "know_more",
                message_source="typed", ad_id="ad-bento-1", ad_title="Bengali Bento This Week",
            )

        report = db.entry_point_breakdown(channel="instagram", days=3650)
        titles = {a["ad_title"] for a in report["by_ad"]}

        assert "Bengali Bento This Week" in titles
        assert report["ad_titles_seen"] >= 1


def test_a_tapped_prompt_arrives_at_the_log_tagged_as_a_tap():
    """The whole path: webhook payload in, correctly tagged row out."""
    user_id = "ep_end_to_end"
    session_memory.clear_session(user_id, "instagram")

    payload = {
        "entry": [
            {
                "messaging": [
                    {
                        "sender": {"id": user_id},
                        "recipient": {"id": "page-1"},
                        "postback": {
                            "mid": "m-ice-1",
                            "title": "Do you deliver outside Delhi NCR?",
                            "payload": "ICE_BREAKER_DELIVERY",
                            "referral": {
                                "source": "ADS",
                                "ad_id": "ad-42",
                                "ads_context_data": {"ad_title": "Nolen Gur Season"},
                            },
                        },
                    }
                ]
            }
        ]
    }

    class _Tasks:
        def __init__(self):
            self.queued = []

        def add_task(self, fn, *args):
            self.queued.append((fn, args))

    tasks = _Tasks()
    result = app._dispatch_instagram_payload(payload, tasks)

    assert result["results"][0]["message_source"] == "ice_breaker"
    assert result["results"][0]["ad_title"] == "Nolen Gur Season"

    with patch.object(app, "send_instagram_reply"), patch.object(
        app.responder, "match_and_respond", return_value=("a reply", True, "delivery_area", None)
    ):
        fn, args = tasks.queued[0]
        fn(*args)

    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT message_source, ad_id, ad_title FROM conversations "
            "WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()

    assert row["message_source"] == "ice_breaker"
    assert row["ad_id"] == "ad-42"
    assert row["ad_title"] == "Nolen Gur Season"

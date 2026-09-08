"""
The order the client asked for, twice over:

    "First the general text
     Then the aamar collection note
     Then the aamar collection menu
     Then signature note
     Signature menu"

Instagram has no caption field, so a captioned card is an image message plus a
text message. The bot used to send the image first and the note after it, which
is the reverse of what was asked for.
"""

import uuid
from unittest.mock import MagicMock, patch

from camellia import app as app_module
from camellia.responder import responder
from camellia.session_memory import session_memory


def instagram_sequence(message):
    """The literal order of messages Instagram would receive."""
    user_id = "seq_" + uuid.uuid4().hex[:8]
    session_memory.clear_session(user_id, "whatsapp")
    text, _matched, template_id, images = responder.match_and_respond(
        message, user_id=user_id, channel="whatsapp"
    )

    sent = []

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {}

    def post(url, headers=None, json=None):
        body = json["message"]
        if "text" in body:
            sent.append(("text", body["text"]))
        else:
            sent.append(("image", body["attachment"]["payload"]["url"]))
        return _Resp()

    client = MagicMock()
    client.__enter__.return_value.post = post
    with patch.object(app_module.config, "INSTAGRAM_ACCESS_TOKEN", "IGAAtoken"), \
         patch.object(app_module.config, "PUBLIC_BASE_URL", "https://example.test"), \
         patch.object(app_module.httpx, "Client", return_value=client):
        app_module.send_instagram_reply("u1", text, image_url=images)

    return template_id, sent


class TestTheClientsRequestedOrder:
    def test_the_price_flow_runs_text_note_card_note_card(self):
        template_id, sent = instagram_sequence("what is the price of sondesh")
        kinds = [kind for kind, _ in sent]

        assert template_id == "sondesh_price_detailed"
        assert kinds[:5] == ["text", "text", "image", "text", "image"]
        assert "Sondesh boxes start at" in sent[0][1]
        assert sent[1][1].startswith("Collection 1. Aamar Mishti")
        assert "AAMAR" in sent[2][1]
        assert sent[3][1].startswith("Collection 2. Signature collection")
        assert "Signature" in sent[4][1]

    def test_show_me_the_menu_runs_the_same_order(self):
        template_id, sent = instagram_sequence("Show me the menu")
        kinds = [kind for kind, _ in sent]

        assert template_id == "know_more"
        assert kinds[:5] == ["text", "text", "image", "text", "image"]
        assert sent[1][1].startswith("Collection 1. Aamar Mishti")
        assert sent[3][1].startswith("Collection 2. Signature collection")

    def test_each_note_arrives_before_the_card_it_describes(self):
        for message in ("Show me the menu", "what is the price of sondesh"):
            _template_id, sent = instagram_sequence(message)
            for index, (kind, body) in enumerate(sent):
                if kind == "image" and "AAMAR" in body:
                    assert sent[index - 1][0] == "text"
                    assert "Aamar Mishti" in sent[index - 1][1], message
                if kind == "image" and "Signature" in body:
                    assert sent[index - 1][0] == "text"
                    assert "Signature collection" in sent[index - 1][1], message

    def test_the_notes_use_the_clients_own_collection_wording(self):
        _template_id, sent = instagram_sequence("Show me the menu")
        aamar = sent[1][1]
        signature = sent[3][1]

        assert "Good local dairy milk" in aamar
        assert "partially mechanised" in aamar
        assert "desi A2 cow milk that has not been industrially processed" in signature
        assert "completely hand crafted end to end" in signature


class TestWhatsAppKeepsTheSameOrder:
    def test_a_note_is_its_own_message_before_the_card(self):
        # A WhatsApp caption renders under its image, so honouring the order
        # means sending the note separately rather than attaching it.
        from camellia.whatsapp_adapter import whatsapp_adapter

        user_id = "wa_seq_" + uuid.uuid4().hex[:6]
        session_memory.clear_session(user_id, "whatsapp")
        text, _m, _t, images = responder.match_and_respond(
            "what is the price of sondesh", user_id=user_id, channel="whatsapp"
        )

        posted = []

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {}

        def post(url, headers=None, json=None):
            posted.append(json)
            return _Resp()

        client = MagicMock()
        client.__enter__.return_value.post = post
        with patch.object(app_module.config, "WHATSAPP_MOCK_MODE", False), \
             patch.object(app_module.config, "WHATSAPP_ACCESS_TOKEN", "t"), \
             patch.object(app_module.config, "WHATSAPP_PHONE_NUMBER_ID", "1"), \
             patch.object(app_module.config, "PUBLIC_BASE_URL", "https://example.test"), \
             patch("camellia.whatsapp_adapter.httpx.Client", return_value=client):
            whatsapp_adapter.send_message("919876543210", text, image_url=images)

        kinds = [p["type"] for p in posted]
        assert kinds[:5] == ["text", "text", "image", "text", "image"]
        # The note must not also be stuck under the card as a caption.
        for payload in posted:
            if payload["type"] == "image":
                assert "caption" not in payload["image"]


class TestTheTesterUiMatchesTheRealSendOrder:
    """
    The web tester renders a reply as one HTML bubble built by client-side
    JS, not by replaying the same code path as send_instagram_reply/
    whatsapp_adapter — so getting the wire order right (tested above) does
    not by itself get the on-screen order right. appendMessage() used to
    unconditionally draw every image card before the outer reply text,
    regardless of text_first, so a customer opening the tester saw the
    general text arrive dead last: the opposite of what real channels send
    and of the order the client asked for.
    """

    def _tester_script(self):
        from unittest.mock import patch

        with patch("camellia.app.require_web_tester"):
            html = app_module.get_web_chat().body.decode("utf-8")
        start = html.index("function appendMessage")
        # appendMessage is a few dozen lines; a generous fixed window avoids
        # guessing at the indentation of whatever function follows it.
        return html[start:start + 4500]

    def test_leadswithtext_is_computed_before_the_card_loop(self):
        script = self._tester_script()
        assert script.index("leadsWithText") < script.index("menu-card-wrap")

    def test_the_outer_text_bubble_is_skipped_when_it_already_led(self):
        # Otherwise a text_first reply would be shown twice.
        script = self._tester_script()
        assert "if (!leadsWithText)" in script

    def test_a_text_first_reply_draws_the_lead_in_paragraph(self):
        script = self._tester_script()
        assert "if (leadsWithText && text)" in script

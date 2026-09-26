"""
The flyer reader must not give up on a flyer it can actually read.

Groq's JSON mode returns 400 json_validate_failed when the completion does not
satisfy the schema, and it does so non-deterministically: the same flyer, byte
for byte, failed once and parsed on the next attempt during live testing. The
reader ran a single attempt, so roughly one upload in three simply did not
extract — the operator posted this week's menu and the dish list stayed on
last week's.
"""
import json
from unittest import mock

import pytest

from camellia.config import config
from camellia import menu_parser as mp
from camellia.menu_parser import menu_parser

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 32

MENU_JSON = {
    "week_of": "2nd September - 8th September, 2026",
    "service_window": "Friday Lunch - Thursday Dinner",
    "vegetarian": [{"item": "Shukto", "description": "d", "price_inr": 280, "quantity": "250g"}],
    "non_vegetarian": [{"item": "Contai Dhaba Chicken", "description": "d", "price_inr": 400, "quantity": "150g"}],
    "bengali_bento": [],
    "norom_pak_sondesh": [],
}


class _Resp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


def _ok(content):
    return _Resp(200, {"choices": [{"message": {"content": content}}]})


_JSON_VALIDATE_FAILED = _Resp(
    400,
    text=json.dumps({"error": {
        "message": "Failed to validate JSON. Please adjust your prompt.",
        "type": "invalid_request_error",
        "code": "json_validate_failed",
        "failed_generation": "",
    }}),
)


@pytest.fixture(autouse=True)
def groq_on(tmp_path):
    original_path, original_enabled, original_key = (
        config.MENU_JSON_PATH, config.GROQ_ENABLED, config.GROQ_API_KEY,
    )
    with open(original_path, "r", encoding="utf-8") as f:
        (tmp_path / "menu.json").write_text(f.read(), encoding="utf-8")
    config.MENU_JSON_PATH = str(tmp_path / "menu.json")
    config.GROQ_ENABLED = True
    config.GROQ_API_KEY = "gsk-test"
    # Nothing here should actually sleep between attempts.
    with mock.patch.object(mp.time, "sleep"):
        yield
    config.MENU_JSON_PATH, config.GROQ_ENABLED, config.GROQ_API_KEY = (
        original_path, original_enabled, original_key,
    )


def _run(responses):
    """Drives the parser with a scripted sequence of HTTP responses."""
    posts = mock.MagicMock(side_effect=responses)
    client = mock.MagicMock()
    client.__enter__ = mock.MagicMock(return_value=mock.MagicMock(post=posts))
    client.__exit__ = mock.MagicMock(return_value=False)
    with mock.patch.object(mp.httpx, "Client", return_value=client):
        result = menu_parser.parse_image_and_update_menu(JPEG, "image/jpeg")
    return result, posts


def test_a_transient_json_validation_failure_is_retried():
    """The exact failure seen in the logs, followed by the same flyer parsing."""
    result, posts = _run([_JSON_VALIDATE_FAILED, _ok(json.dumps(MENU_JSON))])

    assert result["status"] == "success", result
    assert posts.call_count == 2


def test_two_failures_still_leave_a_third_attempt():
    result, posts = _run([
        _JSON_VALIDATE_FAILED, _JSON_VALIDATE_FAILED, _ok(json.dumps(MENU_JSON)),
    ])

    assert result["status"] == "success"
    assert posts.call_count == 3


def test_the_last_attempt_drops_strict_json_mode():
    """
    A model that keeps tripping the schema validator is usually still writing
    good JSON, just wrapped. Insisting on strict mode throws away a flyer we
    can read.
    """
    fenced = "Here is the menu:\n```json\n" + json.dumps(MENU_JSON) + "\n```\nHope that helps!"
    result, posts = _run([_JSON_VALIDATE_FAILED, _JSON_VALIDATE_FAILED, _ok(fenced)])

    assert result["status"] == "success"
    sent_last = posts.call_args_list[-1].kwargs["json"]
    assert "response_format" not in sent_last, "final attempt still forced strict JSON"


def test_a_bad_key_is_not_retried():
    """Retrying a credential problem just makes the operator wait three times."""
    result, posts = _run([_Resp(401, text='{"error":{"message":"Invalid API Key"}}')])

    assert result["status"] == "failed"
    assert posts.call_count == 1


def test_a_rate_limit_is_retried():
    result, posts = _run([_Resp(429, text="rate limited"), _ok(json.dumps(MENU_JSON))])

    assert result["status"] == "success"
    assert posts.call_count == 2


def test_giving_up_reports_why_rather_than_a_bare_failure():
    result, posts = _run([_JSON_VALIDATE_FAILED] * 3)

    assert result["status"] == "failed"
    assert posts.call_count == 3
    assert result.get("reason"), "no reason given for the failure"


def test_a_completion_ceiling_is_always_sent():
    """A truncated completion is invalid JSON, indistinguishable from a bad read."""
    _, posts = _run([_ok(json.dumps(MENU_JSON))])

    # Big enough for a full flyer of dish names and prices; the upper bound
    # that keeps it inside the output quota is asserted separately below.
    assert posts.call_args_list[0].kwargs["json"]["max_tokens"] >= 512


def test_json_is_recovered_from_prose_and_fences():
    from camellia.menu_parser import _extract_json_object

    obj = {"week_of": "x", "vegetarian": [{"item": "Shukto {special}", "price_inr": 280}]}
    body = json.dumps(obj)
    assert _extract_json_object(body) == obj
    assert _extract_json_object(f"```json\n{body}\n```") == obj
    assert _extract_json_object(f"Sure!\n```\n{body}\n```\nAnything else?") == obj
    assert _extract_json_object(f"Here you go: {body} — done") == obj
    assert _extract_json_object("no json here at all") is None
    assert _extract_json_object("") is None


def test_the_completion_reservation_stays_inside_the_token_budget():
    """
    Groq bills max_tokens as reserved budget against the per-minute limit, not
    as what the reply costs. The free tier allows 8000 TPM and a flyer photo
    is already ~5000 of them, so an over-generous reservation turns a request
    that would have worked into a guaranteed 429. A first pass used 4096 and
    broke uploads that had previously succeeded.

    Output is capped far lower than input, and that is the binding limit: the
    vision model allows 1000 output tokens per minute, so reserving more than
    that is rejected outright as "Request too large" before a single token is
    generated. 2048 did exactly that to every upload the client attempted.
    """
    _, posts = _run([_ok(json.dumps(MENU_JSON))])
    assert posts.call_args_list[0].kwargs["json"]["max_tokens"] < 1000


def test_a_spent_quota_waits_longer_than_a_malformed_reply():
    """A per-minute window refills on a clock; retrying in 1.5s wastes the attempt."""
    quota = mp._vision_backoff_seconds(_Resp(429, text="rate limited"), 429)
    malformed = mp._vision_backoff_seconds(_JSON_VALIDATE_FAILED, 400)
    assert quota > malformed


def test_the_providers_own_retry_after_hint_is_honoured():
    class _WithHeader(_Resp):
        headers = {"retry-after": "5"}

    assert mp._vision_backoff_seconds(_WithHeader(429, text="x"), 429) == 5.0


def test_backoff_is_capped_so_the_admin_request_does_not_hang():
    class _Absurd(_Resp):
        headers = {"retry-after": "600"}

    assert mp._vision_backoff_seconds(_Absurd(429, text="x"), 429) <= mp._VISION_MAX_BACKOFF_SECONDS


def test_a_spent_quota_is_reported_as_quota_not_as_an_unreadable_flyer():
    """Otherwise the operator goes hunting for a clearer photo that would not help."""
    result, _ = _run([_Resp(429, text="rate limited")] * 3)

    assert result["status"] == "failed"
    assert "quota" in result["reason"].lower()
    assert "published menu is unchanged" in result["reason"]


def test_the_model_is_told_not_to_think():
    """
    The vision model reasons by default and bills those thinking tokens as
    output. Against a real flyer it spent 700-800 of its 1000-per-minute
    output allowance thinking, wrote no JSON before the ceiling cut it off,
    and Groq returned 400 json_validate_failed with an empty
    failed_generation - the validator rejecting a reply that never existed.
    Every menu upload the client made failed that way: the photo went live
    and the dish list stayed on the previous week. With reasoning off the
    same flyer's date costs 19 tokens.
    """
    _, posts = _run([_ok(json.dumps(MENU_JSON))])
    assert posts.call_args_list[0].kwargs["json"]["reasoning_effort"] == "none"


def test_the_flyer_prompt_does_not_ask_for_descriptions():
    """
    Descriptions are the bulk of a flyer's text. Asking for them pushed a
    full menu past 900 output tokens mid-JSON, and the object never closed -
    and the 1000-per-minute output ceiling leaves no room to raise the cap.
    Names, prices and quantities fit with room to spare; the descriptions are
    filled in afterwards.
    """
    _, posts = _run([_ok(json.dumps(MENU_JSON))])
    prompt = posts.call_args_list[0].kwargs["json"]["messages"][0]["content"][0]["text"]
    assert "description" not in prompt


def test_description_is_not_copied_from_an_older_menu():
    lean = {**MENU_JSON, "vegetarian": [{"item": "Shukto", "price_inr": 280, "quantity": "250g"}]}
    lean["non_vegetarian"] = []
    result = menu_parser._apply_extracted_data_to_menu(lean)
    assert result["parsed"]["vegetarian"][0]["description"] == ""


def test_missing_extracted_facts_remain_blank():
    result = menu_parser._apply_extracted_data_to_menu({"vegetarian": [{"item": "Notun Kichu"}]})
    item = result["parsed"]["vegetarian"][0]
    assert item["price_inr"] is None
    assert item["quantity"] == ""
    assert item["description"] == ""


def test_image_extraction_without_descriptions_remains_draft_only():
    lean = {
        "week_of": "2nd September - 8th September, 2026",
        "service_window": "Friday Lunch - Thursday Dinner",
        "vegetarian": [{"item": "Shukto", "price_inr": 280, "quantity": "250g"}],
        "non_vegetarian": [{"item": "Kosha Mangsho", "price_inr": 600, "quantity": "200g"}],
        "bengali_bento": [],
        "norom_pak_sondesh": [],
    }

    before = open(config.MENU_JSON_PATH, encoding="utf-8").read()
    result, _ = _run([_ok(json.dumps(lean))])

    assert result["status"] == "success"
    assert result["published"] is False
    assert result["week_of"] == "2nd September - 8th September, 2026"
    assert all(dish["description"] == "" for dish in result["parsed"]["vegetarian"])
    assert open(config.MENU_JSON_PATH, encoding="utf-8").read() == before

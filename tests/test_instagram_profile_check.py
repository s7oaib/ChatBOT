import json
from unittest.mock import patch, MagicMock
from camellia import app as app_module


def _call():
    with patch.object(app_module, "require_admin"):
        return app_module.get_instagram_messaging_profile(x_admin_key="k", x_admin_pin="p")


def test_reports_when_no_token_is_configured():
    with patch.object(app_module.config, "INSTAGRAM_ACCESS_TOKEN", ""):
        assert _call()["configured"] is False


def test_surfaces_metas_error_instead_of_pretending_it_is_configured():
    # The exact failure seen on the local token: the Meta app behind it was
    # deleted. That must read as a broken credential, not "no ice breakers".
    resp = MagicMock(status_code=400)
    resp.json.return_value = {
        "error": {"message": "Error validating application. Application has been deleted.",
                  "code": 190, "type": "OAuthException"}
    }
    client = MagicMock()
    client.__enter__.return_value.get.return_value = resp
    with patch.object(app_module.config, "INSTAGRAM_ACCESS_TOKEN", "EAAtoken"), \
         patch.object(app_module.httpx, "Client", return_value=client):
        out = _call()
    assert out["ok"] is False
    assert out["meta_error"]["code"] == 190
    assert "ice_breaker_questions" not in out


def test_lists_the_questions_instagram_is_showing():
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"data": [{"ice_breakers": [
        {"call_to_actions": [
            {"question": "Do you deliver outside Delhi NCR?", "payload": "P1"},
            {"question": "What is the price?", "payload": "P2"},
        ]}
    ]}]}
    client = MagicMock()
    client.__enter__.return_value.get.return_value = resp
    with patch.object(app_module.config, "INSTAGRAM_ACCESS_TOKEN", "EAAtoken"), \
         patch.object(app_module.httpx, "Client", return_value=client):
        out = _call()
    assert out["ok"] is True
    assert out["ice_breakers_set"] is True
    assert "Do you deliver outside Delhi NCR?" in out["ice_breaker_questions"]


def test_none_configured_reads_as_none_rather_than_an_error():
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"data": []}
    client = MagicMock()
    client.__enter__.return_value.get.return_value = resp
    with patch.object(app_module.config, "INSTAGRAM_ACCESS_TOKEN", "EAAtoken"), \
         patch.object(app_module.httpx, "Client", return_value=client):
        out = _call()
    assert out["ok"] is True
    assert out["ice_breakers_set"] is False
    assert out["ice_breaker_questions"] == []


# --- /health token validation ----------------------------------------------

def _reset_token_cache():
    app_module._ig_token_state["checked_at"] = 0.0
    app_module._ig_token_state["result"] = None


def _meta_returning(status, body):
    resp = MagicMock(status_code=status, content=b"x")
    resp.json.return_value = body
    client = MagicMock()
    client.__enter__.return_value.get.return_value = resp
    return client


def test_no_token_configured_is_not_reported_as_a_dead_credential():
    _reset_token_cache()
    with patch.object(app_module.config, "INSTAGRAM_ACCESS_TOKEN", ""):
        assert app_module.instagram_token_status()["state"] == "not_configured"


def test_a_token_meta_accepts_reads_as_valid():
    _reset_token_cache()
    with patch.object(app_module.config, "INSTAGRAM_ACCESS_TOKEN", "EAAtoken"), \
         patch.object(app_module.httpx, "Client", return_value=_meta_returning(200, {"data": []})):
        assert app_module.instagram_token_status()["state"] == "valid"


def test_the_password_change_invalidation_reads_as_invalid():
    # The exact failure in production: Meta invalidated the token after an
    # account password change, while /health stayed green.
    _reset_token_cache()
    body = {"error": {
        "message": "Error validating access token: The session has been invalidated "
                   "because the user changed their password or Facebook has changed "
                   "the session for security reasons.",
        "code": 190, "type": "OAuthException",
    }}
    with patch.object(app_module.config, "INSTAGRAM_ACCESS_TOKEN", "EAAtoken"), \
         patch.object(app_module.httpx, "Client", return_value=_meta_returning(400, body)):
        out = app_module.instagram_token_status()
    assert out["state"] == "invalid"
    assert out["meta_code"] == 190


def test_meta_being_unreachable_is_unknown_not_invalid():
    # A network blip must not be reported as a dead credential, or the warning
    # stops meaning anything.
    _reset_token_cache()
    client = MagicMock()
    client.__enter__.side_effect = OSError("connection reset")
    with patch.object(app_module.config, "INSTAGRAM_ACCESS_TOKEN", "EAAtoken"), \
         patch.object(app_module.httpx, "Client", return_value=client):
        assert app_module.instagram_token_status()["state"] == "unknown"


def test_the_result_is_cached_so_health_polling_does_not_hammer_meta():
    _reset_token_cache()
    client = _meta_returning(200, {"data": []})
    with patch.object(app_module.config, "INSTAGRAM_ACCESS_TOKEN", "EAAtoken"), \
         patch.object(app_module.httpx, "Client", return_value=client) as ctor:
        for _ in range(5):
            app_module.instagram_token_status()
    assert ctor.call_count == 1


def test_an_invalid_token_raises_a_health_warning_without_a_503():
    # Returning 503 here would have the host restart the service over a
    # credential it cannot fix, taking WhatsApp down with it.
    _reset_token_cache()
    body = {"error": {"message": "Session invalidated", "code": 190, "type": "OAuthException"}}
    with patch.object(app_module.config, "INSTAGRAM_ACCESS_TOKEN", "EAAtoken"), \
         patch.object(app_module.httpx, "Client", return_value=_meta_returning(400, body)):
        resp = app_module.health_check()

    payload = json.loads(resp.body)
    assert resp.status_code == 200
    assert payload["instagram_token"]["state"] == "invalid"
    assert any("Instagram access token is rejected" in w for w in payload["warnings"])
    _reset_token_cache()

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

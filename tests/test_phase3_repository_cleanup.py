"""Cleanup equivalence: evaluate Config syntax without loading local secrets/DB."""
import ast
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _config_class():
    tree = ast.parse((ROOT / "camellia/config.py").read_text(encoding="utf-8"))
    return next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Config")


@pytest.mark.parametrize("name", [
    "LLM_TRIAGE_ENABLED", "LLM_ASSISTED_RESOLUTION_ENABLED", "LLM_AUTO_RESOLVE_ENABLED",
    "ENABLE_WEB_TESTER", "REQUIRE_WEBHOOK_SIGNATURE",
])
@pytest.mark.parametrize("value,expected", [(None, None), ("true", True), ("false", False), ("1", True), ("yes", True), ("", False)])
def test_sensitive_flags_keep_effective_defaults_and_parsing(name, value, expected):
    import os
    from unittest.mock import patch

    assignments = [node for node in _config_class().body if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name]
    assert len(assignments) == 1
    expression = ast.Expression(assignments[0].value)
    environment = {} if value is None else {name: value}
    with patch.dict(os.environ, environment, clear=True):
        actual = eval(compile(expression, "config-flag", "eval"), {"os": os})
    if value is None:
        expected = name == "REQUIRE_WEBHOOK_SIGNATURE"
    assert actual is expected


def test_config_has_no_duplicate_top_level_class_assignments():
    names = [node.target.id for node in _config_class().body if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)]
    assert len(names) == len(set(names))
    assert "ADMIN_PIN" not in names


def test_retired_pin_environment_cannot_authenticate(monkeypatch):
    from fastapi import HTTPException
    from camellia.app import require_admin, _admin_context
    from camellia.config import config

    monkeypatch.setenv("ADMIN_PIN", "retired-test-pin")
    monkeypatch.setattr(config, "ADMIN_API_KEY", "machine-test-key")
    token = _admin_context.set(None)
    try:
        with pytest.raises(HTTPException) as error:
            require_admin(provided_key="retired-test-pin", provided_pin="retired-test-pin")
        assert error.value.status_code == 403
        require_admin(provided_key="machine-test-key")
    finally:
        _admin_context.reset(token)


def test_operational_and_backup_endpoints_require_authentication():
    from fastapi.testclient import TestClient
    from camellia.app import app

    client = TestClient(app)
    assert client.get("/admin/operations").status_code == 403
    assert client.post("/admin/db-backup").status_code == 403


def test_no_executable_retired_provider_or_live_configuration_reference():
    for path in (ROOT / "camellia").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [item.name for item in node.names] + [getattr(node, "module", "") or ""]
                assert not any(name.startswith(("google.generative", "google.genai", "mistralai", "azure.ai", "boto3")) for name in names)
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert "GEMINI_" not in node.value
                assert node.value != "ADMIN_PIN"
    assert not (ROOT / "camellia/gemini_responder.py").exists()


def test_deployment_matches_existing_railpack_start_contract():
    config = json.loads((ROOT / "railway.json").read_text(encoding="utf-8"))
    assert config["build"]["builder"] == "RAILPACK"
    assert config["deploy"]["startCommand"] == "python main.py"
    assert config["deploy"]["restartPolicyType"] == "ON_FAILURE"
    assert config["deploy"]["restartPolicyMaxRetries"] == 10


def test_workflows_no_longer_configure_retired_credentials():
    text = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "GEMINI_ENABLED" not in text
    assert "ADMIN_PIN:" not in text
    assert "python -m pytest -q" in text

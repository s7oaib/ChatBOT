import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "export_anonymized_conversations.py"
spec = importlib.util.spec_from_file_location("anonymized_export", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_anonymized_export_removes_phone_and_email_without_changing_prices():
    result = module.anonymize("Call +91 99021 81650 or 9876543210; email hi@example.com. Bento ₹350.")
    assert "+91" not in result
    assert "9876543210" not in result
    assert "hi@example.com" not in result
    assert result.count("[PHONE]") == 2
    assert "₹350" in result

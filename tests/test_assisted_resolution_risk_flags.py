from camellia.assisted_resolution import business_risk_flags


def test_generic_offer_is_not_a_discount_risk():
    assert "discount" not in business_risk_flags("What do you offer?")
    assert "discount" not in business_risk_flags("Do you offer Bengali meals?")


def test_explicit_promotional_language_remains_a_discount_risk():
    for message in (
        "Can I get a discount?",
        "Any promo for this order?",
        "Do you have a 20% off offer?",
        "Is there a coupon or voucher?",
        "What is the special price?",
        "Is this a special offer?",
        "Do you have a discount code?",
    ):
        assert "discount" in business_risk_flags(message), message

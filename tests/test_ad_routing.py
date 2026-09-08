from unittest.mock import patch

from camellia.ad_routing import product_for_ad
from camellia.config import config
from camellia.database import db


def test_configured_ad_id_beats_a_changed_title():
    with patch.object(config, "INSTAGRAM_AD_PRODUCT_MAP", '{"ad-1":"savoury"}'):
        assert product_for_ad("ad-1", "Sondesh festival") == "savoury"


def test_unambiguous_title_is_only_a_fallback():
    with patch.object(config, "INSTAGRAM_AD_PRODUCT_MAP", "{}"):
        assert product_for_ad(None, "Fresh Bengali Bento — Delhi NCR") == "bento"
        assert product_for_ad(None, "Camellia Artisanal") is None


def test_admin_saved_ad_route_beats_environment_and_title():
    ad_id = "test-admin-route-48291"
    db.delete_ad_product_route(ad_id)
    try:
        db.set_ad_product_route(ad_id, "bento")
        with patch.object(config, "INSTAGRAM_AD_PRODUCT_MAP", '{"test-admin-route-48291":"sondesh"}'):
            assert product_for_ad(ad_id, "Savoury launch") == "bento"
    finally:
        db.delete_ad_product_route(ad_id)

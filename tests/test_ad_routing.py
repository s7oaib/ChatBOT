from unittest.mock import patch

from camellia.ad_routing import product_for_ad
from camellia.config import config


def test_configured_ad_id_beats_a_changed_title():
    with patch.object(config, "INSTAGRAM_AD_PRODUCT_MAP", '{"ad-1":"savoury"}'):
        assert product_for_ad("ad-1", "Sondesh festival") == "savoury"


def test_unambiguous_title_is_only_a_fallback():
    with patch.object(config, "INSTAGRAM_AD_PRODUCT_MAP", "{}"):
        assert product_for_ad(None, "Fresh Bengali Bento — Delhi NCR") == "bento"
        assert product_for_ad(None, "Camellia Artisanal") is None

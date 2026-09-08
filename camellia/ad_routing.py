"""Turn an Instagram ad referral into a safe, limited product preference."""

import json
import re
from typing import Optional

from camellia.config import config

_PRODUCTS = {"sondesh", "savoury", "bento"}


def product_for_ad(ad_id: Optional[str], ad_title: Optional[str]) -> Optional[str]:
    """Prefer an admin-configured Meta ad ID; use titles only as a fallback."""
    normalized_ad_id = str(ad_id or "").strip()
    if normalized_ad_id:
        # Kept as a local import so this small routing helper does not make
        # database initialisation a requirement for tools that only import it.
        from camellia.database import db

        saved = db.get_ad_product_route(normalized_ad_id)
        if saved in _PRODUCTS:
            return saved

    try:
        configured = json.loads(config.INSTAGRAM_AD_PRODUCT_MAP or "{}")
    except json.JSONDecodeError:
        configured = {}
    if isinstance(configured, dict):
        value = str(configured.get(normalized_ad_id, "")).lower().strip()
        if value in _PRODUCTS:
            return value

    # Meta sometimes sends no stable title.  This heuristic intentionally only
    # recognizes unambiguous product words and never overrides customer text.
    title = (ad_title or "").lower()
    if re.search(r"\b(bento|meal box|meal subscription|savoury|savory|curry|lunch|dinner)\b", title):
        return "bento" if re.search(r"\b(bento|meal box)\b", title) else "savoury"
    if re.search(r"\b(sondesh|mishti|sweet|sweets|mithai|jolbhora|kanchagolla)\b", title):
        return "sondesh"
    return None

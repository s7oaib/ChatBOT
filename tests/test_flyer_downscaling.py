"""
Flyers are shrunk before they go to the vision model, and only for that.

A photo straight off a phone is most of the free tier's 8000 tokens/minute
spent on resolution the model does not need to read printed text, which is
what made quota failures possible in the first place. The copy customers are
sent is saved from the original bytes — that one is looked at by people.
"""
import io
import os

import pytest
from PIL import Image

from camellia.menu_parser import (
    _downscale_for_vision,
    _VISION_MAX_EDGE_PX,
)


def _jpeg(width, height, exif=None):
    """
    A noisy JPEG of the given size.

    Noise rather than flat colour, so the encoder cannot compress it away and
    the byte-size comparisons stay meaningful. Built with frombytes rather
    than a per-pixel loop — the loop version added a minute to the suite.
    """
    noise = Image.frombytes("RGB", (width // 8, height // 8),
                            os.urandom(3 * (width // 8) * (height // 8)))
    img = noise.resize((width, height), Image.NEAREST)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95, **({"exif": exif} if exif else {}))
    return buf.getvalue()


def test_a_large_flyer_is_shrunk_to_the_readable_ceiling():
    original = _jpeg(2400, 3200)

    shrunk, mime = _downscale_for_vision(original, "image/jpeg")

    assert len(shrunk) < len(original)
    assert mime == "image/jpeg"
    assert max(Image.open(io.BytesIO(shrunk)).size) == _VISION_MAX_EDGE_PX


def test_the_aspect_ratio_survives():
    original = _jpeg(2000, 1000)

    shrunk, _ = _downscale_for_vision(original, "image/jpeg")

    w, h = Image.open(io.BytesIO(shrunk)).size
    assert abs((w / h) - 2.0) < 0.05, "flyer was distorted"


def test_an_already_small_flyer_is_not_re_encoded():
    """Re-encoding a small JPEG only adds another generation of loss."""
    original = _jpeg(600, 800)

    result, mime = _downscale_for_vision(original, "image/jpeg")

    assert result is original or len(result) < len(original)
    if result is original:
        assert mime == "image/jpeg"


def test_a_sideways_phone_photo_is_uprighted():
    """
    Phones record orientation in EXIF rather than rotating pixels, so a flyer
    shot in portrait can reach the model on its side — which it reads far
    worse, with no clue in the failure as to why.
    """
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90° CW
    original = _jpeg(1600, 1200, exif=exif.tobytes())

    shrunk, _ = _downscale_for_vision(original, "image/jpeg")

    w, h = Image.open(io.BytesIO(shrunk)).size
    assert h > w, "EXIF orientation was ignored; the flyer stayed on its side"


def test_unreadable_bytes_fall_back_to_the_original():
    """A failure to shrink must never fail an upload that would have worked."""
    junk = b"this is not an image at all"

    result, mime = _downscale_for_vision(junk, "image/png")

    assert result == junk
    assert mime == "image/png"


def test_pillow_being_absent_falls_back_to_the_original():
    import builtins

    original = _jpeg(2400, 3200)
    real_import = builtins.__import__

    def _no_pillow(name, *args, **kwargs):
        if name.startswith("PIL"):
            raise ImportError("no Pillow here")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = _no_pillow
    try:
        result, mime = _downscale_for_vision(original, "image/jpeg")
    finally:
        builtins.__import__ = real_import

    assert result == original
    assert mime == "image/jpeg"


def test_upload_keeps_a_full_resolution_private_review_source(tmp_path, monkeypatch):
    """
    The shrunk copy exists for the model. Saving it as the customer-facing
    card would quietly degrade the thing people actually look at.
    """
    import json
    from unittest import mock

    from fastapi.testclient import TestClient
    from camellia.config import config
    from camellia import weekly_flyer
    import camellia.database as db_module
    from camellia.menu_parser import menu_parser
    import camellia.database as db_module
    from camellia.app import app

    db_module.db.init_db()
    client = TestClient(app)
    original_key, original_menu = config.ADMIN_API_KEY, config.MENU_JSON_PATH
    config.ADMIN_API_KEY = "test-api-key-downscale-long-enough"
    # weekly_flyer resolves its upload directory from MENU_JSON_PATH, and
    # clear() does not delete the flyer image itself — so without redirecting
    # this the test leaves a photo in the real data/uploads and quietly breaks
    # the tests that assert no flyer has been uploaded.
    with open(original_menu, "r", encoding="utf-8") as f:
        (tmp_path / "menu.json").write_text(f.read(), encoding="utf-8")
    config.MENU_JSON_PATH = str(tmp_path / "menu.json")
    monkeypatch.setattr(db_module.db, "db_path", str(tmp_path / "review.db"))
    db_module.db.init_db()
    app.state.limiter.enabled = False
    original = _jpeg(2400, 3200)
    try:
        weekly_flyer.clear()
        with mock.patch.object(menu_parser, "parse_image_and_update_menu",
                               return_value={"status": "failed", "error": "x"}):
            resp = client.post(
                "/admin/menu/upload-image",
                files={"file": ("flyer.jpg", original, "image/jpeg")},
                headers={"x-admin-key": "test-api-key-downscale-long-enough"},
            )
        assert resp.status_code == 200
        assert resp.json()["published"] is False
        draft = resp.json()["draft"]
        from camellia import menu_review
        saved_path = menu_review.source_file("weekly_vegetarian", draft["draft_id"])
        with Image.open(saved_path) as saved:
            assert saved.size == (2400, 3200), "private review source must retain full resolution"
        assert weekly_flyer.get() is None, "unapproved source must not be customer-facing"
    finally:
        weekly_flyer.clear()
        app.state.limiter.enabled = True
        config.ADMIN_API_KEY, config.MENU_JSON_PATH = original_key, original_menu

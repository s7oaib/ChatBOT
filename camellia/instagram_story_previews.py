"""Private, bounded display previews keyed only by an exact Meta Story ID.

No URL, image, username or source metadata in this module is routing authority.
Signed media URLs stay in memory; only a compact JPEG is persisted.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import tempfile
import threading
import time
import warnings
from io import BytesIO
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

from PIL import Image, ImageOps

from camellia.config import config
from camellia.menu_images import ALLOWED_TYPES, validate_image

MAX_BYTES = 4 * 1024 * 1024
MAX_PREVIEW_BYTES = 2 * 1024 * 1024
MAX_FILES = 128
MAX_PIXELS = 16_000_000
_LOCK = threading.Lock()
_CDN_DOMAINS = ("fbcdn.net", "cdninstagram.com", "fbsbx.com")


def _root() -> Path:
    root = Path(config.MENU_JSON_PATH).resolve().parent / "instagram_story_previews"
    if root.is_symlink():
        raise ValueError("Unsafe preview directory")
    return root


def _path(story_id: str) -> Path:
    return _root() / (hashlib.sha256(str(story_id).encode("utf-8")).hexdigest() + ".jpg")


def _trusted_url(value):
    try:
        parsed = urlsplit(value) if isinstance(value, str) and len(value) <= 8192 else None
        host = (parsed.hostname or "").lower() if parsed else ""
        if (not parsed or parsed.scheme != "https" or parsed.username or parsed.password
                or parsed.port not in (None, 443) or parsed.fragment
                or not any(host == domain or host.endswith("." + domain) for domain in _CDN_DOMAINS)):
            return None
        return parsed
    except ValueError:
        return None


def preview_candidate(payload):
    """Only authenticated Meta Story fields may be passed by our callers."""
    if not isinstance(payload, dict):
        return None
    keys = ("thumbnail_url",) if str(payload.get("media_type") or "").upper() == "VIDEO" else ("thumbnail_url", "media_url", "url")
    return next((payload[key] for key in keys if _trusted_url(payload.get(key))), None)


def _public_address(host):
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("Unsafe media destination")
    addresses.sort(key=lambda item: item[0] != socket.AF_INET)
    return addresses[0][4][0]


def _download(url):
    """Pin a validated public IP while retaining TLS hostname verification.

    No proxies, credentials, automatic redirects or URL logging. Every redirect
    gets the same host/IP checks. Connect/read timeouts are three seconds;
    body reads also have an eight-second deadline and a hard byte limit.
    """
    deadline = time.monotonic() + 8
    for _ in range(3):
        parsed = _trusted_url(url)
        if not parsed or time.monotonic() >= deadline:
            raise ValueError("Unavailable preview")
        address = _public_address(parsed.hostname)
        connection = http.client.HTTPSConnection(parsed.hostname, timeout=3)
        try:
            raw_socket = socket.create_connection((address, 443), timeout=3)
            try:
                connection.sock = ssl.create_default_context().wrap_socket(raw_socket, server_hostname=parsed.hostname)
            except Exception:
                raw_socket.close()
                raise
            connection.sock.settimeout(3)
            target = (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
            connection.request("GET", target, headers={"User-Agent": "Camellia-Story-Preview"})
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location:
                    raise ValueError("Unavailable preview")
                url = urljoin(url, location)
                continue
            mime = (response.getheader("Content-Type") or "").split(";", 1)[0].lower()
            if response.status != 200 or mime not in ALLOWED_TYPES:
                raise ValueError("Unavailable image preview")
            length = response.getheader("Content-Length")
            if length and (int(length) < 0 or int(length) > MAX_BYTES):
                raise ValueError("Oversized preview")
            data = bytearray()
            while True:
                if time.monotonic() >= deadline:
                    raise ValueError("Preview read deadline")
                chunk = response.read(min(65536, MAX_BYTES + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > MAX_BYTES:
                    raise ValueError("Oversized preview")
            return bytes(data), mime
        finally:
            connection.close()
    raise ValueError("Too many preview redirects")


def _thumbnail(data, mime):
    if not data or len(data) > MAX_BYTES:
        raise ValueError("Oversized or empty preview")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        validate_image(data, mime)
        with Image.open(BytesIO(data)) as opened:
            if opened.width * opened.height > MAX_PIXELS:
                raise ValueError("Oversized preview dimensions")
            image = ImageOps.exif_transpose(opened).convert("RGB")
            image.thumbnail((640, 640), Image.Resampling.LANCZOS)
            output = BytesIO()
            image.save(output, format="JPEG", quality=85, optimize=True)
    if output.tell() > MAX_PREVIEW_BYTES:
        raise ValueError("Oversized cached preview")
    return output.getvalue()


def cached_path(story_id):
    try:
        path = _path(story_id)
        return path if path.is_file() and not path.is_symlink() and path.resolve().parent == _root().resolve() else None
    except Exception:
        return None


def delete_preview(story_id):
    try:
        with _LOCK:
            _path(story_id).unlink(missing_ok=True)
    except Exception:
        pass  # A filesystem failure must never undo Story deletion/processing.


def cleanup(database):
    """Remove only our orphan/expired files, using existing lifecycle state."""
    try:
        root = _root()
        if not root.is_dir():
            return
        with database.get_connection() as connection:
            rows = connection.execute("SELECT story_media_id, story_status FROM instagram_story_sources").fetchall()
        current = {_path(row["story_media_id"]).name for row in rows if str(row["story_status"] or "").upper() not in {"EXPIRED", "REMOVED"}}
        with _LOCK:
            for path in root.iterdir():
                if re.fullmatch(r"[a-f0-9]{64}\.jpg", path.name) and path.name not in current:
                    path.unlink(missing_ok=True)
    except Exception:
        pass


def cache_preview(story_id, url, database):
    """Best effort only; never return/log signed URLs or affect a reply."""
    temporary = None
    try:
        if not story_id or not _trusted_url(url) or not database.get_instagram_story_source(story_id):
            return False
        cleanup(database)
        if cached_path(story_id):
            return True
        root = _root()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if len(list(root.glob("*.jpg"))) >= MAX_FILES:
            return False
        image = _thumbnail(*_download(url))
        with _LOCK:
            record = database.get_instagram_story_source(story_id)
            if not record or str(record.get("story_status") or "").upper() in {"EXPIRED", "REMOVED"}:
                return False
            if len(list(root.glob("*.jpg"))) >= MAX_FILES and not cached_path(story_id):
                return False
            with tempfile.NamedTemporaryFile(dir=root, suffix=".tmp", delete=False) as output:
                temporary = output.name
                output.write(image)
            os.replace(temporary, _path(story_id))
            temporary = None
        return True
    except Exception:
        return False
    finally:
        if temporary:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass


def display_metadata(payload, previous=None):
    """Keep only explicitly named Meta source/original fields, never owner/sender."""
    try:
        old = json.loads((previous or {}).get("last_event_metadata") or "{}").get("display") or {}
    except (ValueError, AttributeError):
        old = {}
    if not isinstance(old, dict):
        old = {}
    candidates = {**old, **{key: value for key, value in payload.items() if value is not None}}
    result = {}
    for prefix in ("source", "original"):
        account = payload.get(prefix + "_account")
        account = account if isinstance(account, dict) else {}
        username = payload.get(prefix + "_username", account.get("username", old.get(prefix + "_username")))
        if isinstance(username, str) and re.fullmatch(r"@?[A-Za-z0-9._]{1,30}", username):
            result[prefix + "_username"] = username.lstrip("@")
        for field, value in ((prefix + "_account_id", payload.get(prefix + "_account_id", account.get("id", old.get(prefix + "_account_id")))),
                             (prefix + "_media_id", candidates.get(prefix + "_media_id"))):
            if value is not None and re.fullmatch(r"[0-9]{1,256}", str(value)):
                result[field] = str(value)
    kind = str(candidates.get("media_type") or "").upper()
    if kind in {"IMAGE", "VIDEO"}:
        result["media_type"] = kind
    for field in ("permalink", "source_permalink", "original_permalink"):
        try:
            parsed = urlsplit(str(candidates.get(field) or ""))
            if parsed.scheme == "https" and parsed.hostname in {"www.instagram.com", "instagram.com"} and not parsed.username and not parsed.password and parsed.path:
                result[field] = f"https://{parsed.hostname}{parsed.path}"[:2000]
        except ValueError:
            pass
    return result


def enrich(source, database):
    result = dict(source)
    metadata = display_metadata({}, source)
    result.update({key: value for key, value in metadata.items() if key != "media_type"})
    result["media_type"] = source.get("media_type") or metadata.get("media_type")
    result["preview_url"] = None
    if str(source.get("story_status") or "").upper() not in {"EXPIRED", "REMOVED"} and cached_path(source["story_media_id"]):
        result["preview_url"] = f"/admin/instagram-story-sources/{quote(str(source['story_media_id']), safe='')}/preview"
    identity = database.get_instagram_customer_identity(source.get("last_sender_id")) if source.get("last_sender_id") else None
    result["replier_username"] = (identity or {}).get("customer_username")
    return result

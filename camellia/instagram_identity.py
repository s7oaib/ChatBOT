"""Best-effort Instagram customer identity for authenticated operations."""

from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

import httpx

from camellia.config import config
from camellia.monitoring import get_logger

log = get_logger("camellia.instagram_identity")


@dataclass(frozen=True)
class InstagramIdentity:
    external_id: str
    username: Optional[str] = None
    display_name: Optional[str] = None
    profile_pic: Optional[str] = None

    def as_record(self) -> dict:
        return {
            "customer_external_id": self.external_id,
            "customer_username": self.username,
            "customer_display_name": self.display_name,
            "customer_profile_pic": self.profile_pic,
        }


def resolve_instagram_identity(sender_id: str) -> InstagramIdentity:
    """Return the sender ID immediately and enrich it with username if allowed."""
    external_id = str(sender_id or "").strip()
    if not external_id:
        return InstagramIdentity("")

    token = str(config.INSTAGRAM_ACCESS_TOKEN or "").strip()
    username = None
    display_name = None
    profile_pic = None
    if token:
        host = "graph.instagram.com" if token.startswith("IGAA") else "graph.facebook.com"
        url = f"https://{host}/{config.INSTAGRAM_API_VERSION}/{quote(external_id, safe='')}"
        try:
            with httpx.Client(timeout=5.0) as client:
                response = client.get(
                    url,
                    params={"fields": "username", "access_token": token},
                )
            payload = response.json() if response.status_code == 200 else {}
            candidate = payload.get("username") if isinstance(payload, dict) else None
            if isinstance(candidate, str):
                candidate = candidate.strip().lstrip("@")
                username = candidate or None
            if isinstance(payload, dict):
                display_name = str(payload.get("name") or "").strip() or None
                picture = str(payload.get("profile_pic") or "").strip()
                profile_pic = picture if picture.startswith("https://") and "access_token" not in picture.lower() else None
        except Exception:
            # Do not log the raw ID, username, token, provider response, or
            # exception. The sender ID remains a complete operational identity.
            log.info("Instagram username lookup unavailable; retaining sender identity.")

    return InstagramIdentity(external_id=external_id, username=username, display_name=display_name, profile_pic=profile_pic)

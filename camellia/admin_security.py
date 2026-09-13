"""Admin authentication primitives shared by the portal and its tests.

This module deliberately contains no browser/UI code and never logs credential
material. Passwords and recovery codes are verified with Argon2id; session and
CSRF bearer values are stored only as hashes.
"""

import base64
import hashlib
import hmac
import secrets
import struct
import time
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError


PASSWORD_HASHER = PasswordHasher()
_DUMMY_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$YKztZ1fXCD0xDncpTVd1mQ$"
    "pejmZN6iNFNveFYYgh3cRHsltV4ZkbryBt8NSbl2/ds"
)


def digest(value: str) -> str:
    """Return a stable non-reversible identifier for audit/throttle storage."""
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def normalize_username(value: str) -> str:
    return str(value or "").strip().casefold()


def verify_password(candidate: str, configured_hash: str) -> bool:
    """Verify an Argon2id hash, treating malformed configuration as failure."""
    stored = configured_hash or _DUMMY_PASSWORD_HASH
    if configured_hash and not configured_hash.startswith("$argon2id$"):
        return False
    try:
        return bool(PASSWORD_HASHER.verify(stored, str(candidate or "")))
    except (InvalidHashError, VerificationError, VerifyMismatchError, ValueError):
        return False


def hash_secret(value: str) -> str:
    return PASSWORD_HASHER.hash(str(value))


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(value: str) -> str:
    return digest(value)


def verify_totp(secret: str, code: str, now: Optional[float] = None, window: int = 1) -> bool:
    """Verify a six-digit RFC 6238 TOTP with a small clock-skew window."""
    normalized = "".join(str(secret or "").split()).upper()
    candidate = str(code or "").strip()
    if not normalized or len(candidate) != 6 or not candidate.isdigit():
        return False
    try:
        padded = normalized + "=" * (-len(normalized) % 8)
        key = base64.b32decode(padded, casefold=True)
    except (ValueError, base64.binascii.Error):
        return False
    counter = int((time.time() if now is None else now) // 30)
    for offset in range(-max(0, int(window)), max(0, int(window)) + 1):
        digest_value = hmac.new(key, struct.pack(">Q", counter + offset), hashlib.sha1).digest()
        start = digest_value[-1] & 0x0F
        number = struct.unpack(">I", digest_value[start:start + 4])[0] & 0x7FFFFFFF
        expected = str(number % 1_000_000).zfill(6)
        if hmac.compare_digest(expected, candidate):
            return True
    return False


def generate_recovery_code() -> str:
    return secrets.token_urlsafe(12)

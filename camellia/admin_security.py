"""Admin authentication primitives shared by the portal and its tests.

This module deliberately contains no browser/UI code and never logs credential
material. Passwords are verified with Argon2id; session and CSRF bearer values
are stored only as hashes.
"""

import hashlib
import secrets

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


def hash_password(value: str) -> str:
    return PASSWORD_HASHER.hash(str(value))


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(value: str) -> str:
    return digest(value)

"""
FR-WAL-03  Fernet encryption of per-user XRPL seeds.

Used only by services/xrpl_service.py — nowhere else. The key comes from
XRPL_ENCRYPTION_KEY (env), never from the database, so a DB dump alone does not
expose usable seeds. Each ciphertext is tagged with KEY_ID so a future key
rotation can tell which key sealed which row.
"""
from __future__ import annotations

import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


class KeyConfigError(RuntimeError):
    """XRPL_ENCRYPTION_KEY is missing or not a valid Fernet key."""


def _fernet() -> Fernet:
    try:
        return Fernet(settings.xrpl_encryption_key.encode())
    except (ValueError, TypeError) as exc:
        raise KeyConfigError(
            "XRPL_ENCRYPTION_KEY must be a url-safe base64 32-byte Fernet key "
            "(generate with Fernet.generate_key())."
        ) from exc


def key_id() -> str:
    """Stable, non-reversible fingerprint of the active key (stored beside each ciphertext)."""
    return hashlib.sha256(settings.xrpl_encryption_key.encode()).hexdigest()[:16]


def encrypt_seed(seed: str) -> str:
    return _fernet().encrypt(seed.encode()).decode()


def decrypt_seed(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        # Never include the token or key in the message.
        raise KeyConfigError("Seed could not be decrypted with the configured key.") from exc

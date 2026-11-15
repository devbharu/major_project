"""Encrypt GitHub tokens at rest (Fernet / AES-128-CBC + HMAC). Key = TOKEN_ENCRYPTION_KEY, else derived from SECRET_KEY."""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from .config import settings

PREFIX = "enc:"


def _fernet() -> Fernet:
    seed = (settings.token_encryption_key or settings.secret_key).encode()
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"warren-token-v1:" + seed).digest()))


def encrypt(plain: str) -> str:
    return PREFIX + _fernet().encrypt(plain.encode()).decode() if plain else ""


def decrypt(stored: str) -> str:
    if not stored:
        return ""
    if not stored.startswith(PREFIX):
        return stored      # legacy plaintext row; re-encrypted on next login
    try:
        return _fernet().decrypt(stored[len(PREFIX):].encode()).decode()
    except InvalidToken:
        return ""          # key changed: the user has to sign in again

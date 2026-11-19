"""Security primitives: JWT, password hashing (bcrypt), Fernet encryption.

Nothing in here ever logs secrets. Tokens are signed with JWT_SECRET from
settings. Service-account material (storage_state blobs, connector tokens)
is encrypted with Fernet using FERNET_KEY.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from cryptography.fernet import Fernet, InvalidToken
from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import get_settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

ALGORITHM = "HS256"
ACCESS = "access"
REFRESH = "refresh"


# --- Passwords (bcrypt) ---

def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return pwd_context.verify(plain_password, hashed_password)
    except ValueError:
        return False


# --- JWT ---

def _create_token(subject: str | int, token_type: Literal["access", "refresh"], expires: timedelta) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(subject),
        "type": token_type,
        "iat": now,
        "exp": now + expires,
        "jti": secrets.token_hex(8),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=ALGORITHM)


def create_access_token(subject: str | int) -> str:
    settings = get_settings()
    return _create_token(subject, ACCESS, timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES))


def create_refresh_token(subject: str | int) -> str:
    settings = get_settings()
    return _create_token(subject, REFRESH, timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS))


def create_token_pair(subject: str | int) -> tuple[str, str]:
    return create_access_token(subject), create_refresh_token(subject)


def decode_token(token: str) -> dict[str, Any] | None:
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[ALGORITHM])
    except JWTError:
        return None
    if payload.get("type") not in (ACCESS, REFRESH):
        return None
    return payload


# --- Fernet encryption at rest ---

def _fernet() -> Fernet:
    settings = get_settings()
    if not settings.FERNET_KEY:
        raise RuntimeError("FERNET_KEY is not configured")
    return Fernet(settings.FERNET_KEY.encode() if isinstance(settings.FERNET_KEY, str) else settings.FERNET_KEY)


def encrypt_bytes(plaintext: bytes) -> bytes:
    return _fernet().encrypt(plaintext)


def decrypt_bytes(ciphertext: bytes) -> bytes:
    try:
        return _fernet().decrypt(ciphertext)
    except InvalidToken as exc:
        raise ValueError("Cannot decrypt payload: invalid FERNET_KEY or corrupted data") from exc


def encrypt_str(plaintext: str) -> bytes:
    return encrypt_bytes(plaintext.encode("utf-8"))


def decrypt_str(ciphertext: bytes) -> str:
    return decrypt_bytes(ciphertext).decode("utf-8")
